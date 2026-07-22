from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from grok_helper.auth import require_admin
from grok_helper.logger import logger
from grok_helper.paths import data_path, project_root


REGISTER_ROOT = data_path("register")
TASKS_DIR = REGISTER_ROOT / "tasks"
DB_PATH = REGISTER_ROOT / "console.db"
REPO_ROOT = project_root()
SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
_DEFAULT_PYTHON = Path(sys.executable)
SOURCE_VENV_PYTHON = Path(os.getenv("GROK_REGISTER_PYTHON", str(_DEFAULT_PYTHON))).expanduser()
SOURCE_PYTHON = SOURCE_VENV_PYTHON
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1")))
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))


def _env_seconds(name: str, default: int, minimum: int = 30) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, value)


STALL_TIMEOUT_SECONDS = _env_seconds("GROK_REGISTER_CONSOLE_STALL_TIMEOUT", 300)

PROJECT_FILES = ("DrissionPage_example.py", "email_register.py", "sso_to_cpa.py", "grok2api_push.py")
PROJECT_DIRS = ("turnstilePatch",)

STATUS_QUEUED = "queued"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

db_lock = threading.RLock()


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    count: int = Field(50, ge=1, le=5000)
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_provider: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_email: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    api_endpoint: str | None = None
    api_token: str | None = None
    api_append: bool | None = None
    notes: str = ""


class SystemSettings(BaseModel):
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_provider: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_email: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str | list[str] = ""
    temp_mail_site_password: str = ""
    api_endpoint: str = ""
    api_token: str = ""
    api_append: bool = True
    # 验证码等信超时（秒）：注册流程轮询临时邮箱等待 OTP 的最长时间
    otp_wait_timeout: int = Field(90, ge=30, le=600)
    # grok2api（chenyme/grok2api，Go 版）三池推送
    grok2api_enabled: bool = False
    grok2api_base_url: str = ""
    grok2api_username: str = ""
    grok2api_password: str = ""
    grok2api_push_build: bool = True
    grok2api_push_web: bool = True
    grok2api_push_console: bool = True
    # CPA（cli-proxy-api）xai auth 导出开关
    cpa_enabled: bool = True


@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[Any]
    log_handle: Any


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _task_is_stalled(last_log_at: str | None) -> bool:
    activity_at = _parse_timestamp(last_log_at)
    if activity_at is None:
        return False
    return (datetime.now() - activity_at).total_seconds() >= STALL_TIMEOUT_SECONDS


def ensure_dirs() -> None:
    REGISTER_ROOT.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_lock, closing(get_conn()) as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, closing(get_conn()) as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, closing(get_conn()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, closing(get_conn()) as conn:
        conn.execute(query, params)
        conn.commit()


def init_db() -> None:
    ensure_dirs()
    with db_lock, closing(get_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                current_round INTEGER NOT NULL DEFAULT 0,
                current_phase TEXT,
                last_email TEXT,
                last_error TEXT,
                last_log_at TEXT,
                notes TEXT,
                config_json TEXT NOT NULL,
                task_dir TEXT NOT NULL,
                console_path TEXT NOT NULL,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER
            );
            """
        )


def load_source_defaults() -> dict[str, Any]:
    config_path = SOURCE_PROJECT / "config.json"
    if config_path.exists():
        try:
            base = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[register] 读取 config.json 失败: {exc}")
            base = {}
    else:
        example_path = SOURCE_PROJECT / "config.example.json"
        if example_path.exists():
            try:
                base = json.loads(example_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"[register] 读取 config.example.json 失败: {exc}")
                base = {}
        else:
            base = {
                "run": {"count": 50},
                "proxy": "",
                "browser_proxy": "",
                "temp_mail_provider": "",
                "temp_mail_api_base": "",
                "temp_mail_admin_email": "",
                "temp_mail_admin_password": "",
                "temp_mail_domain": "",
                "temp_mail_site_password": "",
                "api": {"endpoint": "", "token": "", "append": True},
            }

    env_count = os.getenv("GROK_REGISTER_DEFAULT_RUN_COUNT", "").strip()
    if env_count:
        try:
            base.setdefault("run", {})["count"] = max(1, int(env_count))
        except ValueError:
            pass

    env_otp_wait = os.getenv("GROK_REGISTER_DEFAULT_OTP_WAIT_TIMEOUT", "").strip()
    if env_otp_wait:
        try:
            base["otp_wait_timeout"] = max(30, min(600, int(env_otp_wait)))
        except ValueError:
            pass

    env_map = {
        "proxy": "GROK_REGISTER_DEFAULT_PROXY",
        "browser_proxy": "GROK_REGISTER_DEFAULT_BROWSER_PROXY",
        "temp_mail_provider": "GROK_REGISTER_DEFAULT_TEMP_MAIL_PROVIDER",
        "temp_mail_api_base": "GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE",
        "temp_mail_admin_email": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_EMAIL",
        "temp_mail_admin_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD",
        "temp_mail_domain": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN",
        "temp_mail_site_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_SITE_PASSWORD",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            base[key] = value

    api_base = dict(base.get("api") or {})
    for key, env_name in {
        "endpoint": "GROK_REGISTER_DEFAULT_API_ENDPOINT",
        "token": "GROK_REGISTER_DEFAULT_API_TOKEN",
    }.items():
        value = os.getenv(env_name)
        if value is not None:
            api_base[key] = value
    append_env = os.getenv("GROK_REGISTER_DEFAULT_API_APPEND")
    if append_env is not None:
        api_base["append"] = append_env.strip().lower() in {"1", "true", "yes", "on"}
    base["api"] = api_base

    # grok2api（Go 版）三池推送默认值，允许通过环境变量预置。
    g2a = dict(base.get("grok2api") or {})
    for key, env_name in {
        "base_url": "GROK_REGISTER_GROK2API_BASE_URL",
        "username": "GROK_REGISTER_GROK2API_USERNAME",
        "password": "GROK_REGISTER_GROK2API_PASSWORD",
    }.items():
        value = os.getenv(env_name)
        if value is not None:
            g2a[key] = value
    for key, env_name in {
        "enabled": "GROK_REGISTER_GROK2API_ENABLED",
        "push_build": "GROK_REGISTER_GROK2API_PUSH_BUILD",
        "push_web": "GROK_REGISTER_GROK2API_PUSH_WEB",
        "push_console": "GROK_REGISTER_GROK2API_PUSH_CONSOLE",
    }.items():
        value = os.getenv(env_name)
        if value is not None:
            g2a[key] = value.strip().lower() in {"1", "true", "yes", "on"}
    if g2a:
        base["grok2api"] = g2a

    # CPA（cli-proxy-api）xai auth 导出开关，允许通过环境变量预置。
    cpa = dict(base.get("cpa") or {})
    cpa_enabled_env = os.getenv("GROK_REGISTER_CPA_EXPORT_ENABLED")
    if cpa_enabled_env is not None:
        cpa["enabled"] = cpa_enabled_env.strip().lower() in {"1", "true", "yes", "on"}
    if cpa:
        base["cpa"] = cpa
    return base


def read_settings() -> dict[str, Any]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", ("system",))
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_domain_list(values: list[Any]) -> list[str]:
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def write_settings(settings: SystemSettings) -> dict[str, Any]:
    data = settings.model_dump()
    # Preserve existing sensitive values when frontend sends empty strings
    sensitive_keys = ("temp_mail_admin_password", "temp_mail_site_password", "api_token", "grok2api_password")
    existing = read_settings()
    for key in sensitive_keys:
        if not data.get(key) and existing.get(key):
            data[key] = existing[key]
    temp_mail_domain = data.get("temp_mail_domain")
    if isinstance(temp_mail_domain, list):
        data["temp_mail_domain"] = _clean_domain_list(temp_mail_domain)
    elif isinstance(temp_mail_domain, str):
        data["temp_mail_domain"] = temp_mail_domain.strip()
    execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("system", json.dumps(data, ensure_ascii=False), now_iso()),
    )
    return data


def merged_defaults() -> dict[str, Any]:
    base = load_source_defaults()
    saved = read_settings()
    for key in (
        "proxy",
        "browser_proxy",
        "temp_mail_provider",
        "temp_mail_api_base",
        "temp_mail_admin_email",
        "temp_mail_admin_password",
        "temp_mail_site_password",
    ):
        if key in saved:
            base[key] = str(saved.get(key, ""))
    if "temp_mail_domain" in saved:
        saved_domain = saved.get("temp_mail_domain")
        if isinstance(saved_domain, str) and saved_domain.strip():
            base["temp_mail_domain"] = saved_domain.strip()
        elif isinstance(saved_domain, list):
            base["temp_mail_domain"] = _clean_domain_list(saved_domain)

    api_base = dict(base.get("api") or {})
    if "api_endpoint" in saved:
        api_base["endpoint"] = str(saved.get("api_endpoint", ""))
    if "api_token" in saved:
        api_base["token"] = str(saved.get("api_token", ""))
    if "api_append" in saved:
        api_base["append"] = bool(saved.get("api_append", True))
    base["api"] = api_base

    # grok2api（Go 版）三池推送段：config.json 里独立于旧版 api 段。
    g2a = dict(base.get("grok2api") or {})
    g2a.setdefault("enabled", False)
    g2a.setdefault("base_url", "")
    g2a.setdefault("username", "")
    g2a.setdefault("password", "")
    g2a.setdefault("push_build", True)
    g2a.setdefault("push_web", True)
    g2a.setdefault("push_console", True)
    if "grok2api_enabled" in saved:
        g2a["enabled"] = bool(saved.get("grok2api_enabled", False))
    if "grok2api_base_url" in saved:
        g2a["base_url"] = str(saved.get("grok2api_base_url", ""))
    if "grok2api_username" in saved:
        g2a["username"] = str(saved.get("grok2api_username", ""))
    if "grok2api_password" in saved:
        g2a["password"] = str(saved.get("grok2api_password", ""))
    if "grok2api_push_build" in saved:
        g2a["push_build"] = bool(saved.get("grok2api_push_build", True))
    if "grok2api_push_web" in saved:
        g2a["push_web"] = bool(saved.get("grok2api_push_web", True))
    if "grok2api_push_console" in saved:
        g2a["push_console"] = bool(saved.get("grok2api_push_console", True))
    base["grok2api"] = g2a

    # CPA（cli-proxy-api）auth 导出段：注册成功后把 sso 换成 xai auth json。
    cpa = dict(base.get("cpa") or {})
    cpa.setdefault("enabled", True)
    if "cpa_enabled" in saved:
        cpa["enabled"] = bool(saved.get("cpa_enabled", True))
    base["cpa"] = cpa

    # 验证码等信超时（秒）：控制注册脚本轮询临时邮箱等待 OTP 的最长时间。
    otp_wait = base.get("otp_wait_timeout")
    if not isinstance(otp_wait, int):
        otp_wait = 90
    if "otp_wait_timeout" in saved:
        try:
            otp_wait = int(saved.get("otp_wait_timeout", 90))
        except (TypeError, ValueError):
            otp_wait = 90
    base["otp_wait_timeout"] = max(30, min(600, otp_wait))
    return base


def build_task_config_from_defaults(defaults: dict[str, Any], payload: TaskCreate) -> dict[str, Any]:
    api_defaults = dict(defaults.get("api") or {})
    return {
        "run": {"count": int(payload.count)},
        "proxy": defaults.get("proxy", "") if payload.proxy is None else payload.proxy.strip(),
        "browser_proxy": defaults.get("browser_proxy", "") if payload.browser_proxy is None else payload.browser_proxy.strip(),
        "temp_mail_provider": defaults.get("temp_mail_provider", "") if payload.temp_mail_provider is None else payload.temp_mail_provider.strip(),
        "temp_mail_api_base": defaults.get("temp_mail_api_base", "") if payload.temp_mail_api_base is None else payload.temp_mail_api_base.strip(),
        "temp_mail_admin_email": defaults.get("temp_mail_admin_email", "") if payload.temp_mail_admin_email is None else payload.temp_mail_admin_email.strip(),
        "temp_mail_admin_password": defaults.get("temp_mail_admin_password", "") if payload.temp_mail_admin_password is None else payload.temp_mail_admin_password.strip(),
        "temp_mail_domain": defaults.get("temp_mail_domain", "")
        if payload.temp_mail_domain is None
        else payload.temp_mail_domain.strip()
        if isinstance(payload.temp_mail_domain, str)
        else payload.temp_mail_domain,
        "temp_mail_site_password": defaults.get("temp_mail_site_password", "") if payload.temp_mail_site_password is None else payload.temp_mail_site_password.strip(),
        "api": {
            "endpoint": api_defaults.get("endpoint", "") if payload.api_endpoint is None else payload.api_endpoint.strip(),
            "token": api_defaults.get("token", "") if payload.api_token is None else payload.api_token.strip(),
            "append": api_defaults.get("append", True) if payload.api_append is None else bool(payload.api_append),
        },
        # grok2api 三池推送为系统级配置，不支持按任务覆盖，直接沿用系统默认值。
        "grok2api": dict(defaults.get("grok2api") or {}),
        # CPA auth 导出同为系统级配置，不支持按任务覆盖。
        "cpa": dict(defaults.get("cpa") or {}),
        # 验证码等信超时（秒）为系统级配置，不支持按任务覆盖。
        "otp_wait_timeout": int(defaults.get("otp_wait_timeout", 90)),
    }


def build_task_config(payload: TaskCreate) -> dict[str, Any]:
    return build_task_config_from_defaults(merged_defaults(), payload)


def _mask_sensitive_config(config: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive fields in task config before returning to frontend."""
    masked = dict(config)
    for key in ("api_token", "temp_mail_admin_password", "temp_mail_site_password"):
        if key in masked and masked[key]:
            val = str(masked[key])
            masked[key] = val[:2] + "***" if len(val) > 2 else "***"
    for key in ("proxy", "browser_proxy"):
        if key in masked and masked[key]:
            masked[key] = _mask_proxy(masked[key])
    if "api" in masked and isinstance(masked["api"], dict):
        api = dict(masked["api"])
        if api.get("token"):
            val = str(api["token"])
            api["token"] = val[:2] + "***" if len(val) > 2 else "***"
        masked["api"] = api
    if "grok2api" in masked and isinstance(masked["grok2api"], dict):
        g2a = dict(masked["grok2api"])
        if g2a.get("password"):
            val = str(g2a["password"])
            g2a["password"] = val[:2] + "***" if len(val) > 2 else "***"
        masked["grok2api"] = g2a
    return masked


def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
    config = json.loads(row["config_json"])
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "target_count": int(row["target_count"]),
        "completed_count": int(row["completed_count"]),
        "failed_count": int(row["failed_count"]),
        "current_round": int(row["current_round"]),
        "current_phase": row["current_phase"] or "",
        "last_email": row["last_email"] or "",
        "last_error": row["last_error"] or "",
        "last_log_at": row["last_log_at"] or "",
        "notes": row["notes"] or "",
        "config": _mask_sensitive_config(config),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "pid": row["pid"],
    }


def _tail_read(path: Path, max_bytes: int = 512 * 1024) -> str:
    """Read the tail of a file efficiently, avoiding loading the entire file."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="replace")
    with path.open("rb") as f:
        f.seek(-max_bytes, 2)
        raw = f.read()
    # Skip partial first line
    nl = raw.find(b"\n")
    if nl != -1:
        raw = raw[nl + 1:]
    return raw.decode("utf-8", errors="replace")


def read_log_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    text = _tail_read(path)
    return text.splitlines()[-limit:]


def parse_console_state(console_path: Path) -> dict[str, Any]:
    state = {
        "completed_count": 0,
        "failed_count": 0,
        "current_round": 0,
        "current_phase": "",
        "last_email": "",
        "last_error": "",
        "last_log_at": now_iso(),
    }
    if not console_path.exists():
        return state

    lines = _tail_read(console_path, max_bytes=2 * 1024 * 1024).splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if m := re.search(r"开始第\s*(\d+)\s*轮注册", line):
            state["current_round"] = int(m.group(1))
            state["current_phase"] = "starting_round"
        if m := re.search(r"注册成功\s*\|\s*email=([^|\s]+)", line):
            state["completed_count"] += 1
            state["last_email"] = m.group(1)
            state["current_phase"] = "success"
        if m := re.search(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)", line):
            state["failed_count"] += 1
            state["last_error"] = m.group(2).strip()
            if state["current_phase"] != "pushed_to_api":
                state["current_phase"] = "error"
        if m := re.search(r"临时邮箱创建成功:\s*([^\s]+)", line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "mailbox_created"
        if m := re.search(r"已填写邮箱并点击注册:\s*([^\s]+)", line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "email_submitted"
        if "提取到验证码" in line:
            state["current_phase"] = "otp_received"
        if "最终注册页" in line:
            state["current_phase"] = "profile_page"
        if "Turnstile 响应已同步" in line:
            state["current_phase"] = "turnstile_solved"
        if "已填写注册资料并点击完成注册" in line:
            state["current_phase"] = "submitting_profile"
        if re.search(r"SSO token 已推送到 API|已推送到 API", line):
            state["current_phase"] = "pushed_to_api"
    return state


def task_row(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def delete_task_files(row: sqlite3.Row | dict[str, Any]) -> None:
    root = TASKS_DIR.resolve()
    task_dir = Path(row["task_dir"]).resolve()
    try:
        task_dir.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Task directory is outside TASKS_DIR") from exc
    if task_dir == root:
        raise HTTPException(status_code=400, detail="Refusing to delete TASKS_DIR")
    if task_dir.exists() and task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)


def copy_source_to_task_dir(task_dir: Path, task_config: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    for file_name in PROJECT_FILES:
        shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
    for dir_name in PROJECT_DIRS:
        src = SOURCE_PROJECT / dir_name
        dst = task_dir / dir_name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "sso").mkdir(exist_ok=True)
    (task_dir / "config.json").write_text(
        json.dumps(task_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def package_cpa_auths(task_dir: Path) -> Path | None:
    """把任务目录里 DrissionPage_example.py 导出的 CPA auth json 打成 tar.gz。

    单账号 json 由注册脚本写入 `<task_dir>/sso/cpa_auths/xai-*.json`。
    任务结束时统一打包，方便下载/迁移/导入 CPA。无 json 时返回 None，
    任何失败都吞掉，不影响任务状态收尾。
    """
    try:
        auth_dir = task_dir / "sso" / "cpa_auths"
        if not auth_dir.is_dir():
            return None
        auth_files = sorted(auth_dir.glob("xai-*.json"))
        if not auth_files:
            return None
        archive_path = task_dir / "cpa_xai_auth_import.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            for auth_file in auth_files:
                tar.add(auth_file, arcname=auth_file.name)
        logger.info(f"[register] 已打包 {len(auth_files)} 个 CPA auth: {archive_path}")
        return archive_path
    except Exception as exc:
        logger.error(f"[register] 打包 CPA auth 失败: {exc}")
        return None


def _mask_proxy(proxy_url: str) -> str:
    try:
        parsed = urlparse(proxy_url)
        if not parsed.scheme or not parsed.netloc:
            return proxy_url
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except (ValueError, TypeError):
        return "***"


def _request_with_optional_proxy(
    url: str,
    proxy_url: str = "",
    method: str = "GET",
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    return requests.request(method, url, timeout=timeout, headers=headers, proxies=proxies, allow_redirects=True)


def _build_health_item(
    key: str,
    label: str,
    ok: bool,
    summary: str,
    detail: str,
    target: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "summary": summary,
        "detail": detail,
        "target": target,
        "checked_at": now_iso(),
    }


def run_health_checks() -> dict[str, Any]:
    defaults = merged_defaults()
    items: list[dict[str, Any]] = []

    browser_proxy = str(defaults.get("browser_proxy", "") or "").strip()
    request_proxy = str(defaults.get("proxy", "") or "").strip()
    api_conf = dict(defaults.get("api") or {})
    api_endpoint = str(api_conf.get("endpoint", "") or "").strip()
    temp_mail_api_base = str(defaults.get("temp_mail_api_base", "") or "").strip()

    warp_target = browser_proxy or request_proxy
    if not warp_target:
        items.append(_build_health_item("warp", "WARP / Proxy", False, "未配置代理出口", "当前系统默认配置里没有 `browser_proxy` 或 `proxy`，无法检查前置网络出口。", "-"))
    else:
        try:
            response = _request_with_optional_proxy("https://www.cloudflare.com/cdn-cgi/trace", proxy_url=warp_target, timeout=20)
            body = response.text
            ip_match = re.search(r"(?m)^ip=(.+)$", body)
            loc_match = re.search(r"(?m)^loc=(.+)$", body)
            warp_match = re.search(r"(?m)^warp=(.+)$", body)
            ip = ip_match.group(1).strip() if ip_match else "unknown"
            loc = loc_match.group(1).strip() if loc_match else "unknown"
            warp_state = warp_match.group(1).strip() if warp_match else "unknown"
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    response.status_code == 200,
                    f"HTTP {response.status_code} | IP {ip} | LOC {loc}",
                    f"通过代理 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 成功，warp={warp_state}。",
                    _mask_proxy(warp_target),
                )
            )
        except Exception as exc:
            items.append(_build_health_item("warp", "WARP / Proxy", False, "代理出口不可达", f"通过 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 失败：{exc}", _mask_proxy(warp_target)))

    if not api_endpoint:
        items.append(_build_health_item("grok2api", "grok2api Admin Sink", False, "未配置 token sink", "当前系统默认配置里没有 `api.endpoint`，注册成功后不会自动入池。", "-"))
    else:
        try:
            response = _request_with_optional_proxy(api_endpoint, timeout=15)
            ok = response.status_code in {200, 401, 403, 405}
            items.append(_build_health_item("grok2api", "grok2api Admin Sink", ok, f"HTTP {response.status_code}", "防封版管理接口已可达。即使返回 401/403，也说明服务本身在线，只是需要正确的管理口令。", api_endpoint))
        except Exception as exc:
            items.append(_build_health_item("grok2api", "grok2api Admin Sink", False, "接口不可达", f"访问 `{api_endpoint}` 失败：{exc}", api_endpoint))

    if not temp_mail_api_base:
        items.append(_build_health_item("temp_mail", "Temp Mail API", False, "未配置临时邮箱 API", "当前系统默认配置里没有 `temp_mail_api_base`，注册流程会在创建邮箱阶段直接失败。", "-"))
    else:
        try:
            response = _request_with_optional_proxy(temp_mail_api_base, proxy_url=request_proxy, timeout=15)
            items.append(_build_health_item("temp_mail", "Temp Mail API", response.status_code < 500, f"HTTP {response.status_code}", "接口地址可达。这里只做基础连通性检查，不会真的创建邮箱地址。", temp_mail_api_base))
        except Exception as exc:
            items.append(_build_health_item("temp_mail", "Temp Mail API", False, "接口不可达", f"访问 `{temp_mail_api_base}` 失败：{exc}", temp_mail_api_base))

    xai_proxy = browser_proxy or request_proxy
    try:
        response = _request_with_optional_proxy(
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            proxy_url=xai_proxy,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        ok = response.status_code in {200, 301, 302, 303, 307, 308, 500}
        detail = f"使用 `{_mask_proxy(xai_proxy)}` 访问注册页返回 HTTP {response.status_code}。" if xai_proxy else f"直连访问注册页返回 HTTP {response.status_code}。"
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        if response.status_code == 500:
            detail += " Cloudflare 可能返回了挑战页面，通常不影响实际注册。"
        items.append(_build_health_item("xai", "x.ai Sign-up", ok, f"HTTP {response.status_code}", detail, "https://accounts.x.ai/sign-up?redirect=grok-com"))
    except Exception as exc:
        items.append(_build_health_item("xai", "x.ai Sign-up", False, "注册页不可达", f"访问 `x.ai` 注册页失败：{exc}", "https://accounts.x.ai/sign-up?redirect=grok-com"))

    return {"items": items, "checked_at": now_iso()}


class TaskSupervisor:
    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            managed_processes = list(self._processes.values())
            thread = self._thread

        for managed in managed_processes:
            self._terminate_process(managed)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=SUPERVISOR_INTERVAL + 1)
        for managed in managed_processes:
            exit_code = self._close_managed(managed)
            self._mark_stopped_for_shutdown(managed, exit_code)

        with self._lock:
            self._processes.clear()

    def stop_task(self, task_id: int) -> None:
        with self._lock:
            managed = self._processes.get(task_id)
        if not managed:
            row = task_row(task_id)
            if row["status"] in (STATUS_QUEUED, STATUS_STARTING):
                message = "Task stopped before launch." if row["status"] == STATUS_QUEUED else "Task stopped while starting."
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, last_error = ?, current_phase = ?, pid = NULL
                    WHERE id = ?
                    """,
                    (STATUS_STOPPED, now_iso(), message, STATUS_STOPPED, task_id),
                )
                return
            raise HTTPException(status_code=409, detail=f"Task is not stoppable in status '{row['status']}'")

        execute_no_return(
            "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
            (STATUS_STOPPING, "Stopping task...", STATUS_STOPPING, task_id),
        )
        self._terminate_process(managed)

    def _running_count(self) -> int:
        with self._lock:
            return len(self._processes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_running()
                self._launch_queued()
            except Exception as exc:
                logger.error(f"[register] supervisor 循环异常: {exc}")
            self._stop.wait(SUPERVISOR_INTERVAL)

    def _launch_queued(self) -> None:
        slots = MAX_CONCURRENT_TASKS - self._running_count()
        if slots <= 0:
            return
        # Atomically claim queued tasks by setting status to 'starting'
        # This prevents race condition with stop_task() changing status concurrently
        with db_lock, closing(get_conn()) as conn:
            claimed = conn.execute(
                "SELECT id FROM tasks WHERE status = ? ORDER BY id ASC LIMIT ?",
                (STATUS_QUEUED, slots),
            ).fetchall()
            claimed_ids = [int(row["id"]) for row in claimed]
            if not claimed_ids:
                return
            placeholders = ",".join("?" for _ in claimed_ids)
            conn.execute(
                f"UPDATE tasks SET status = ? WHERE id IN ({placeholders}) AND status = ?",
                (STATUS_STARTING, *claimed_ids, STATUS_QUEUED),
            )
            conn.commit()
            starting = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) AND status = ? ORDER BY id ASC",
                (*claimed_ids, STATUS_STARTING),
            ).fetchall()
        for row in starting:
            if self._stop.is_set():
                # Revert unprocessed starting tasks back to queued
                execute_no_return(
                    "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
                    (STATUS_QUEUED, int(row["id"]), STATUS_STARTING),
                )
                return
            self._start_task(row)

    def _start_task(self, row: sqlite3.Row) -> None:
        task_id = int(row["id"])
        # Verify task is still in 'starting' status (could have been stopped concurrently)
        current = fetch_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
        if not current or current["status"] != STATUS_STARTING:
            return
        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        try:
            task_config = json.loads(row["config_json"])
            copy_source_to_task_dir(task_dir, task_config)

            output_path = task_dir / "sso" / f"task_{task_id}.txt"
            command = [
                str(SOURCE_PYTHON),
                str(task_dir / "DrissionPage_example.py"),
                "--count",
                str(int(row["target_count"])),
                "--output",
                str(output_path),
            ]
            log_handle = console_path.open("a", encoding="utf-8")
            popen_kwargs: dict[str, Any] = {
                "cwd": task_dir,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        except Exception as exc:
            self._mark_start_failed(task_id, exc)
            raise

        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except Exception as exc:
            log_handle.close()
            self._mark_start_failed(task_id, exc)
            raise

        with self._lock:
            self._processes[task_id] = ManagedProcess(
                task_id=task_id,
                process=process,
                log_handle=log_handle,
            )
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, pid = ?, started_at = ?, current_phase = ?, last_log_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, process.pid, now_iso(), "process_started", now_iso(), task_id),
        )

    def _refresh_running(self) -> None:
        finished: list[int] = []
        closed: set[int] = set()
        with self._lock:
            managed_items = list(self._processes.items())
        for task_id, managed in managed_items:
            try:
                row = task_row(task_id)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                self._terminate_process(managed)
                self._close_managed(managed)
                with self._lock:
                    self._processes.pop(task_id, None)
                logger.warning(f"[register] 移除缺失任务进程: id={task_id}")
                continue
            console_path = Path(row["console_path"])
            try:
                parsed = parse_console_state(console_path)
            except Exception as exc:
                parsed = {
                    "completed_count": int(row["completed_count"] or 0),
                    "failed_count": int(row["failed_count"] or 0),
                    "current_round": int(row["current_round"] or 0),
                    "current_phase": row["current_phase"] or "log_parse_failed",
                    "last_email": row["last_email"] or "",
                    "last_error": f"日志解析失败: {exc}",
                    "last_log_at": now_iso(),
                }
            execute_no_return(
                """
                UPDATE tasks
                SET completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"],
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            exit_code = managed.process.poll()
            if exit_code is None:
                current = fetch_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                if (current and current["status"] == STATUS_STOPPING) or not _task_is_stalled(parsed["last_log_at"]):
                    continue

                timeout_error = f"任务连续 {STALL_TIMEOUT_SECONDS} 秒无日志进展，已自动终止"
                self._terminate_process(managed)
                exit_code = self._close_managed(managed)
                closed.add(task_id)
                stalled_status = STATUS_PARTIAL if parsed["completed_count"] > 0 else STATUS_FAILED
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, exit_code = ?,
                        completed_count = ?, failed_count = ?, current_round = ?,
                        current_phase = ?, last_email = ?, last_error = ?,
                        last_log_at = ?, pid = NULL
                    WHERE id = ?
                    """,
                    (
                        stalled_status,
                        now_iso(),
                        exit_code,
                        parsed["completed_count"],
                        parsed["failed_count"],
                        parsed["current_round"],
                        "stalled_timeout",
                        parsed["last_email"],
                        timeout_error,
                        parsed["last_log_at"],
                        task_id,
                    ),
                )
                finished.append(task_id)
                continue

            final_status = STATUS_FAILED
            if row["status"] == STATUS_STOPPING or exit_code in (-15, -9):
                final_status = STATUS_STOPPED
            elif parsed["completed_count"] >= int(row["target_count"]) and exit_code == 0:
                final_status = STATUS_COMPLETED
            elif parsed["completed_count"] > 0:
                final_status = STATUS_PARTIAL
            execute_no_return(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, exit_code = ?,
                    completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?, pid = NULL
                WHERE id = ?
                """,
                (
                    final_status,
                    now_iso(),
                    exit_code,
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"] or final_status,
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            finished.append(task_id)

        for task_id in finished:
            with self._lock:
                managed = self._processes.pop(task_id, None)
            if managed and task_id not in closed:
                self._close_managed(managed)
            try:
                row = task_row(task_id)
                package_cpa_auths(Path(row["task_dir"]))
            except HTTPException:
                pass

    def _terminate_process(self, managed: ManagedProcess) -> None:
        if managed.process.poll() is not None:
            return
        try:
            if os.name == "nt":
                # Windows cannot reliably terminate a whole descendant tree here;
                # start new process group and terminate the parent process.
                managed.process.terminate()
            else:
                os.killpg(managed.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            managed.process.terminate()

    def _close_managed(self, managed: ManagedProcess) -> int | None:
        exit_code: int | None = None
        try:
            exit_code = managed.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                managed.process.kill()
                exit_code = managed.process.wait(timeout=5)
            except OSError:
                pass
        if managed.log_handle:
            managed.log_handle.close()
        return exit_code

    def _mark_stopped_for_shutdown(self, managed: ManagedProcess, exit_code: int | None) -> None:
        try:
            task_row(managed.task_id)
        except HTTPException:
            return
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, finished_at = ?, exit_code = ?, current_phase = ?,
                last_error = ?, pid = NULL
            WHERE id = ? AND status IN (?, ?)
            """,
            (
                STATUS_STOPPED,
                now_iso(),
                exit_code,
                STATUS_STOPPED,
                "Task stopped during application shutdown.",
                managed.task_id,
                STATUS_RUNNING,
                STATUS_STOPPING,
            ),
        )

    def _mark_start_failed(self, task_id: int, exc: Exception) -> None:
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, finished_at = ?, exit_code = NULL, current_phase = ?,
                last_error = ?, pid = NULL
            WHERE id = ?
            """,
            (STATUS_FAILED, now_iso(), "start_failed", str(exc), task_id),
        )


supervisor = TaskSupervisor()


def start_register_supervisor() -> None:
    init_db()
    _cleanup_orphaned_tasks()
    supervisor.start()


def _cleanup_orphaned_tasks() -> None:
    """Mark running/stopping tasks as failed on startup (server restarted)."""
    try:
        rows = fetch_all(
            "SELECT id, name FROM tasks WHERE status IN (?, ?, ?)",
            (STATUS_STARTING, STATUS_RUNNING, STATUS_STOPPING),
        )
        if rows:
            execute_no_return(
                "UPDATE tasks SET status = ?, last_error = ?, finished_at = ?, current_phase = ?, pid = NULL WHERE status IN (?, ?, ?)",
                (STATUS_FAILED, "服务重启，任务被中断", now_iso(), "orphaned_after_restart", STATUS_STARTING, STATUS_RUNNING, STATUS_STOPPING),
            )
            for row in rows:
                logger.warning(f"[register] 清理孤儿任务: id={row['id']} name={row['name']}")
    except Exception as exc:
        logger.error(f"[register] 清理孤儿任务失败: {exc}")


def stop_register_supervisor() -> None:
    supervisor.stop()


router = APIRouter(prefix="/admin/register", tags=["Register Admin"], dependencies=[Depends(require_admin)])


def _mask_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive fields in settings before returning to frontend."""
    masked = dict(data)
    for key in ("temp_mail_admin_password", "temp_mail_site_password", "api_token", "grok2api_password"):
        if key in masked and masked[key]:
            val = str(masked[key])
            masked[key] = val[:2] + "***" if len(val) > 2 else "***"
    for key in ("proxy", "browser_proxy"):
        if key in masked and masked[key]:
            masked[key] = _mask_proxy(masked[key])
    return masked


@router.get("/meta")
def api_meta() -> dict[str, Any]:
    return {
        "defaults": _mask_settings(merged_defaults()),
        "settings": _mask_settings(read_settings()),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@router.get("/health")
def api_health() -> dict[str, Any]:
    return run_health_checks()


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return {"settings": _mask_settings(read_settings()), "defaults": _mask_settings(merged_defaults())}


@router.post("/settings")
def save_settings(payload: SystemSettings) -> dict[str, Any]:
    saved = write_settings(payload)
    return {"settings": _mask_settings(saved), "defaults": _mask_settings(merged_defaults())}


@router.get("/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(5, ge=1, le=100),
) -> dict[str, Any]:
    offset = (page - 1) * page_size
    total_row = fetch_one("SELECT COUNT(*) AS cnt FROM tasks")
    total = int(total_row["cnt"]) if total_row else 0
    rows = fetch_all(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    return {
        "tasks": [serialize_task(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.post("/tasks")
def create_task(payload: TaskCreate) -> dict[str, Any]:
    if not SOURCE_PROJECT.exists():
        raise HTTPException(status_code=500, detail=f"Source project not found: {SOURCE_PROJECT}")
    if not SOURCE_VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"Python not found: {SOURCE_VENV_PYTHON}")
    task_config = build_task_config(payload)
    created_at = now_iso()
    task_id = execute(
        """
        INSERT INTO tasks (
            name, status, target_count, notes, config_json, task_dir, console_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            STATUS_QUEUED,
            payload.count,
            payload.notes.strip(),
            json.dumps(task_config, ensure_ascii=False),
            str(TASKS_DIR / "pending"),
            str(TASKS_DIR / "pending.log"),
            created_at,
        ),
    )
    task_dir = TASKS_DIR / f"task_{task_id}"
    console_path = task_dir / "console.log"
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        execute_no_return(
            "UPDATE tasks SET task_dir = ?, console_path = ? WHERE id = ?",
            (str(task_dir), str(console_path), task_id),
        )
    except Exception:
        execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
        if task_dir.exists():
            delete_task_files({"task_dir": str(task_dir)})
        raise
    return {"task": serialize_task(task_row(task_id))}


@router.get("/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return {"task": serialize_task(task_row(task_id))}


@router.get("/tasks/{task_id}/logs")
def get_task_logs(task_id: int, limit: int = Query(200, ge=20, le=1000)) -> dict[str, Any]:
    row = task_row(task_id)
    return {"lines": read_log_lines(Path(row["console_path"]), limit=limit)}


@router.post("/tasks/{task_id}/stop")
def stop_task(task_id: int) -> dict[str, Any]:
    supervisor.stop_task(task_id)
    return {"ok": True}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, Any]:
    row = task_row(task_id)
    if row["status"] in {STATUS_RUNNING, STATUS_STOPPING, STATUS_STARTING}:
        raise HTTPException(status_code=409, detail="Task is still running")
    delete_task_files(row)
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}
