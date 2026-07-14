#!/usr/bin/env python3
"""
向 chenyme/grok2api（Go 版）的三个账号池推送注册好的账号。

grok2api 把账号分成三个独立的池（provider），各自有独立的导入接口：
  - Grok Build   (grok_build)   → OAuth 凭证（access/refresh token），需要 grok-cli:access 权限
  - Grok Web     (grok_web)     → SSO cookie
  - Grok Console (grok_console) → SSO cookie

导入接口都在 `{base_url}/api/admin/v1/accounts/...` 下，且：
  - 需要管理员 Bearer JWT（先 POST /auth/login 换取 tokens.accessToken）；
  - 请求体是 multipart/form-data 文件上传（字段名 files），返回 text/event-stream。

本模块只负责 HTTP 交互，调用方负责组织每个池的账号数据。作为库使用：
  from grok2api_push import login, push_build, push_web, push_console, build_credential_entry
"""
from __future__ import annotations

import json
from typing import Any

import requests

# 与 sso_to_cpa 保持一致的 OAuth client_id（Grok Build 凭证需要）。
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
BASE_URL = "https://cli-chat-proxy.grok.com/v1"


def _proxies(proxy: str = "") -> dict[str, str] | None:
    proxy = str(proxy or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def _dig_access_token(data: Any) -> str:
    """从登录响应里挖出 tokens.accessToken。

    Go 端 `response.Success` 可能直接返回 `{"tokens": {...}}`，也可能包一层
    `{"data": {"tokens": {...}}}`。两种都兼容。
    """
    if not isinstance(data, dict):
        return ""
    candidates = [data, data.get("data")]
    for node in candidates:
        if not isinstance(node, dict):
            continue
        tokens = node.get("tokens")
        if isinstance(tokens, dict):
            token = tokens.get("accessToken") or tokens.get("access_token")
            if token:
                return str(token)
    return ""


def login(base_url: str, username: str, password: str, proxy: str = "", timeout: int = 30) -> str:
    """POST /api/admin/v1/auth/login → 管理员 access JWT。失败抛异常。"""
    base = str(base_url or "").rstrip("/")
    url = f"{base}/api/admin/v1/auth/login"
    resp = requests.post(
        url,
        json={"username": username, "password": password},
        proxies=_proxies(proxy),
        timeout=timeout,
        verify=False,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"登录响应不是 JSON: {resp.text[:200]}") from exc
    token = _dig_access_token(data)
    if not token:
        raise RuntimeError(f"登录响应里找不到 accessToken: {json.dumps(data)[:200]}")
    return token


def build_credential_entry(token: dict, email: str = "", client_id: str = "") -> dict[str, Any]:
    """把 sso_to_cpa.sso_to_token() 换到的 OAuth token 映射成 grok_build 导入条目。

    grok2api 的 grok_build 导入器要求：provider=grok_build、至少有 access_token 或
    refresh_token、token_type 若给必须是 Bearer、过期时间用 expires_at(RFC3339) 或
    JWT 的 exp 或 expires_in。
    """
    import sso_to_cpa

    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = sso_to_cpa.decode_jwt_payload(access)
    user_id = payload.get("sub") or payload.get("principal_id") or ""
    expires_in = int(token.get("expires_in") or 21600)
    if "exp" in payload:
        expires_at = sso_to_cpa.rfc3339_seconds(float(payload["exp"]))
    else:
        import time

        expires_at = sso_to_cpa.rfc3339_seconds(time.time() + expires_in)
    return {
        "provider": "grok_build",
        "client_id": client_id or CLIENT_ID,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": token.get("id_token") or "",
        "token_type": token.get("token_type") or "Bearer",
        "scope": token.get("scope") or "",
        "expires_at": expires_at,
        "expires_in": expires_in,
        "email": email or "",
        "user_id": user_id,
    }


def _import(
    base_url: str,
    jwt: str,
    path_suffix: str,
    filename: str,
    content: str,
    content_type: str,
    proxy: str = "",
    timeout: int = 120,
) -> tuple[bool, str]:
    """通用 multipart 文件上传导入。返回 (ok, 简要信息)。"""
    base = str(base_url or "").rstrip("/")
    url = f"{base}/api/admin/v1/accounts/{path_suffix}"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {jwt}", "Accept": "text/event-stream"},
            files={"files": (filename, content, content_type)},
            proxies=_proxies(proxy),
            timeout=timeout,
            verify=False,
        )
    except Exception as exc:  # noqa: BLE001 - 上层只关心成功与否
        return False, f"请求异常: {exc}"
    body = ""
    try:
        body = str(resp.text or "")
    except Exception:
        body = ""
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {body[:200]}"
    # 响应是 SSE 流；成功时通常含 complete 事件，出错时含 error 事件。
    low = body.lower()
    if '"event":"error"' in low or "event: error" in low:
        return False, f"导入返回错误事件: {body[:200]}"
    return True, f"HTTP 200 | {body[:120]}".strip()


def push_build(base_url: str, jwt: str, entries: list[dict], proxy: str = "", timeout: int = 120) -> tuple[bool, str]:
    """推送 OAuth 凭证到 Grok Build 池。entries 为 build_credential_entry 产物。"""
    entries = [e for e in entries if e]
    if not entries:
        return True, "无 Build 凭证可推送"
    content = json.dumps({"accounts": entries}, ensure_ascii=False)
    return _import(base_url, jwt, "import", "grok_build.json", content, "application/json", proxy, timeout)


def push_web(base_url: str, jwt: str, sso_list: list[str], proxy: str = "", timeout: int = 120) -> tuple[bool, str]:
    """推送 SSO cookie 到 Grok Web 池（纯文本，每行一个 sso）。"""
    tokens = [str(s or "").strip() for s in sso_list if str(s or "").strip()]
    if not tokens:
        return True, "无 Web token 可推送"
    content = "\n".join(tokens) + "\n"
    return _import(base_url, jwt, "web/import", "grok_web.txt", content, "text/plain", proxy, timeout)


def push_console(base_url: str, jwt: str, sso_list: list[str], proxy: str = "", timeout: int = 120) -> tuple[bool, str]:
    """推送 SSO cookie 到 Grok Console 池（纯文本，每行一个 sso）。"""
    tokens = [str(s or "").strip() for s in sso_list if str(s or "").strip()]
    if not tokens:
        return True, "无 Console token 可推送"
    content = "\n".join(tokens) + "\n"
    return _import(base_url, jwt, "console/import", "grok_console.txt", content, "text/plain", proxy, timeout)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def push_records(records: list[dict], config: dict, proxy: str = "") -> dict[str, Any]:
    """高层编排：登录一次，按开关把注册记录推送到 Grok Build / Web / Console。

    config（来自 config.json 的 grok2api 段）字段：
      - enabled:        总开关（调用方已判定，这里再兜底一次）
      - base_url:       grok2api 服务地址，例如 http://grok2api:8080
      - username/password: 管理员账号密码（换取 Bearer JWT）
      - push_build / push_web / push_console: 三个池各自的开关（bool）

    records 每项形如 {"email", "sso", "cpa_token"}。其中 cpa_token 是注册流程里
    export_cpa_auth 复用的 OAuth token（权限校验已通过）；没有它的账号会跳过 Build。

    返回各池结果字典，任何失败都不抛出，只记录。
    """
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result: dict[str, Any] = {"build": None, "web": None, "console": None}

    if not _truthy(config.get("enabled")):
        return result

    base_url = str(config.get("base_url") or "").strip()
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not base_url or not username or not password:
        print("[Warn] grok2api 推送跳过：base_url / username / password 未配置完整")
        return result

    want_build = _truthy(config.get("push_build"))
    want_web = _truthy(config.get("push_web"))
    want_console = _truthy(config.get("push_console"))
    if not (want_build or want_web or want_console):
        print("[Warn] grok2api 推送跳过：Build / Web / Console 三个池开关都未启用")
        return result

    try:
        jwt = login(base_url, username, password, proxy=proxy)
    except Exception as exc:  # noqa: BLE001
        print(f"[Warn] grok2api 登录失败，跳过推送: {exc}")
        return result
    print(f"[*] grok2api 管理员登录成功: {base_url}")

    sso_list = [str(r.get("sso") or "").strip() for r in records if str(r.get("sso") or "").strip()]

    if want_build:
        entries = []
        for r in records:
            token = r.get("cpa_token")
            if isinstance(token, dict) and (token.get("access_token") or token.get("key")):
                try:
                    entries.append(build_credential_entry(token, email=str(r.get("email") or "")))
                except Exception as exc:  # noqa: BLE001
                    print(f"[Warn] 构建 Grok Build 凭证失败（email={r.get('email')}）: {exc}")
        skipped = len(records) - len(entries)
        ok, info = push_build(base_url, jwt, entries, proxy=proxy)
        result["build"] = {"ok": ok, "count": len(entries), "info": info}
        note = f"，{skipped} 个无 grok-cli 权限已跳过" if skipped > 0 else ""
        print(f"[{'*' if ok else 'Warn'}] Grok Build 推送 {len(entries)} 个{note}: {info}")

    if want_web:
        ok, info = push_web(base_url, jwt, sso_list, proxy=proxy)
        result["web"] = {"ok": ok, "count": len(sso_list), "info": info}
        print(f"[{'*' if ok else 'Warn'}] Grok Web 推送 {len(sso_list)} 个: {info}")

    if want_console:
        ok, info = push_console(base_url, jwt, sso_list, proxy=proxy)
        result["console"] = {"ok": ok, "count": len(sso_list), "info": info}
        print(f"[{'*' if ok else 'Warn'}] Grok Console 推送 {len(sso_list)} 个: {info}")

    return result
