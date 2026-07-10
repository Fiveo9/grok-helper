#!/usr/bin/env python3
"""
SSO cookie → CPA xai auth json 格式（纯 HTTP Device Flow）

既可作为库被 DrissionPage_example.py / register.py 调用，也可独立作为 CLI 使用。

CLI 用法:
  # 单个 / 批量 SSO，写出多个独立 CPA auth 文件
  python3 sso_to_cpa.py --sso sso_list.txt --out-dir ./cpa

  # 单行 sso
  python3 sso_to_cpa.py --sso-cookie 'eyJ...' --email user@example.com --out ./xai-user@example.com.json

库用法:
  from sso_to_cpa import export_cpa_auth_from_sso
  path = export_cpa_auth_from_sso(sso_value, email="a@b.com", output_dir="/some/dir", proxy="")
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
AUTH_KEY = f"{OIDC_ISSUER}::{CLIENT_ID}"
BASE_URL = "https://cli-chat-proxy.grok.com/v1"
TOKEN_ENDPOINT = f"{OIDC_ISSUER}/oauth2/token"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)

# 运行期可配置的全局状态（由 configure() 设置）。
HTTP_PROXY = ""
SSL_CONTEXT = None
VERIFY_TLS = True


def configure(proxy: str = "", verify_tls: bool = True) -> None:
    """设置代理与 TLS 校验；库调用方在换取前先调用一次即可。"""
    global HTTP_PROXY, SSL_CONTEXT, VERIFY_TLS
    HTTP_PROXY = str(proxy or "").strip()
    VERIFY_TLS = bool(verify_tls)
    SSL_CONTEXT = None if verify_tls else ssl._create_unverified_context()


def build_proxy_config(proxy: str = "") -> tuple[dict | None, urllib.request.OpenerDirector | None]:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None, None
    proxies = {"http": proxy, "https": proxy}
    handlers = [urllib.request.ProxyHandler(proxies)]
    if SSL_CONTEXT is not None:
        handlers.append(urllib.request.HTTPSHandler(context=SSL_CONTEXT))
    opener = urllib.request.build_opener(*handlers)
    return proxies, opener


def http_urlopen(req: urllib.request.Request, timeout: int = 15):
    if HTTP_PROXY:
        _, opener = build_proxy_config(HTTP_PROXY)
        return opener.open(req, timeout=timeout)
    kwargs = {"timeout": timeout}
    if SSL_CONTEXT is not None:
        kwargs["context"] = SSL_CONTEXT
    return urllib.request.urlopen(req, **kwargs)


def request_kwargs(extra: dict | None = None) -> dict:
    kwargs = dict(extra or {})
    if HTTP_PROXY:
        proxies, _ = build_proxy_config(HTTP_PROXY)
        kwargs["proxies"] = proxies
    if not VERIFY_TLS:
        kwargs["verify"] = False
    return kwargs


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_ns(ts: float | None = None) -> str:
    """2026-07-10T01:00:00.000000000Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def rfc3339_seconds(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename_part(value: str) -> str:
    value = str(value or "").strip()
    return "".join(c if c.isalnum() or c in "._@+-" else "_" for c in value)


def request_device_code() -> dict | None:
    data = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPES}).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with http_urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ❌ device/code HTTP {e.code}: {e.read().decode()[:200]}")
        return None


def poll_token(device_code: str, interval: int, expires_in: int, timeout: int = 60) -> dict | None:
    deadline = time.time() + min(expires_in, timeout)
    while time.time() < deadline:
        time.sleep(interval)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with http_urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            print(f"  ❌ token: {error}")
            return None
    print("  ❌ 轮询超时")
    return None


def sso_to_token(sso_cookie: str) -> dict | None:
    """SSO cookie → token dict (access/refresh/expires_in)"""
    s = requests.Session()
    s.cookies.set("sso", sso_cookie, domain=".x.ai")

    try:
        r = s.get(
            "https://accounts.x.ai/",
            **request_kwargs({"impersonate": "chrome", "timeout": 15}),
        )
    except Exception as e:
        print(f"  ❌ 网络错误: {e}")
        return None
    if "sign-in" in r.url or "sign-up" in r.url:
        print("  ❌ sso 无效")
        return None
    print("  ✅ sso 有效")

    print("  🔑 Device Flow...")
    dc = request_device_code()
    if not dc:
        return None
    print(f"  📋 user_code: {dc.get('user_code')}")

    try:
        s.get(
            dc["verification_uri_complete"],
            **request_kwargs({"impersonate": "chrome", "timeout": 15}),
        )
        r = s.post(
            f"{OIDC_ISSUER}/oauth2/device/verify",
            data={"user_code": dc["user_code"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            **request_kwargs(
                {"impersonate": "chrome", "timeout": 15, "allow_redirects": True}
            ),
        )
        if "consent" not in r.url:
            print(f"  ❌ verify 失败: {r.url}")
            return None
    except Exception as e:
        print(f"  ❌ verify 异常: {e}")
        return None

    try:
        r = s.post(
            f"{OIDC_ISSUER}/oauth2/device/approve",
            data={
                "user_code": dc["user_code"],
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            **request_kwargs(
                {"impersonate": "chrome", "timeout": 15, "allow_redirects": True}
            ),
        )
        if "done" not in r.url:
            print(f"  ❌ approve 失败: {r.url}")
            return None
        print("  ✅ 授权确认")
    except Exception as e:
        print(f"  ❌ approve 异常: {e}")
        return None

    token = poll_token(
        dc["device_code"],
        dc.get("interval", 5),
        dc.get("expires_in", 1800),
    )
    if not token:
        return None
    print(
        f"  ✅ access_token (expires_in={token.get('expires_in')}s)"
        + (" + refresh_token" if token.get("refresh_token") else "")
    )
    return token


def token_to_auth_entry(token: dict, sso_cookie: str, email: str = "") -> tuple[str, dict]:
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = decode_jwt_payload(access)

    user_id = payload.get("sub") or payload.get("principal_id") or ""
    expires_in = int(token.get("expires_in") or 21600)
    if "exp" in payload:
        expires_at = rfc3339_seconds(float(payload["exp"]))
    else:
        expires_at = rfc3339_seconds(time.time() + expires_in)

    entry = {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access,
        "refresh_token": refresh,
        "token_type": token.get("token_type") or "Bearer",
        "expires_in": expires_in,
        "expired": expires_at,
        "last_refresh": rfc3339_seconds(),
        "email": email or "",
        "sub": user_id,
        "base_url": BASE_URL,
        "token_endpoint": TOKEN_ENDPOINT,
        "redirect_uri": REDIRECT_URI,
        "disabled": False,
        "headers": {
            "x-grok-client-version": "0.2.93",
            "x-xai-token-auth": "xai-grok-cli",
            "x-authenticateresponse": "authenticate-response",
            "x-grok-client-identifier": "grok-shell",
            "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
        },
        "id_token": sso_cookie,
    }
    return user_id, entry


def write_auth_json(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_auth_json(path: Path, entry_key: str, entry: dict) -> None:
    """
    合并写入。CPA 常用独立文件；保留 --merge 仅用于批量检查。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing[entry_key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalize_sso_cookie(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("sso="):
        token = token[4:].strip()
    return token


def export_cpa_auth_from_sso(
    sso_value: str,
    email: str = "",
    output_dir: str = "",
    proxy: str = "",
    verify_tls: bool = True,
) -> Path | None:
    """
    高层封装：把单个 sso cookie 换取为 CPA xai auth json，写入 output_dir。
    返回写入的文件路径；失败或未配置 output_dir 时返回 None，且不抛异常，
    以免影响注册主流程。
    """
    output_dir = str(output_dir or "").strip()
    if not output_dir:
        return None

    sso = normalize_sso_cookie(sso_value)
    if not sso:
        return None

    configure(proxy=proxy, verify_tls=verify_tls)
    try:
        token = sso_to_token(sso)
        if not token:
            print("  ❌ CPA 导出失败：未能换取 access_token")
            return None
        uid, entry = token_to_auth_entry(token, sso_cookie=sso, email=email)
        file_id = safe_filename_part(email) if email else safe_filename_part(uid)
        if not file_id:
            file_id = secrets.token_hex(4)
        path = Path(output_dir) / f"xai-{file_id}.json"
        write_auth_json(path, entry)
        print(f"  💾 CPA auth 已导出: {path}")
        return path
    except Exception as e:
        print(f"  ❌ CPA 导出异常: {e}")
        return None


def load_sso_list(path: str | None, single: str | None, email: str = "") -> list[dict]:
    if single:
        return [{"email": email.strip(), "sso": normalize_sso_cookie(single)}]
    if not path:
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line_email = ""
        # 兼容 邮箱----密码----sso
        if "----" in line:
            parts = line.split("----")
            line_email = parts[0].strip()
            line = parts[-1].strip()
        out.append({"email": line_email, "sso": normalize_sso_cookie(line)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SSO cookie → CPA xai auth json (纯 HTTP)")
    ap.add_argument("--sso", metavar="FILE", help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）")
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument("--out", default=None, help="输出 CPA auth json 路径（单账号或 --merge）")
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 xai-{email}.json",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 email 或 sub",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("https_proxy") or os.environ.get("http_proxy") or "",
        help="HTTP/HTTPS 代理，例如 http://127.0.0.1:7897（默认读取环境变量）",
    )
    ap.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="跳过 HTTPS 证书校验，仅在代理证书链异常时临时使用",
    )
    args = ap.parse_args()

    configure(proxy=args.proxy, verify_tls=not args.insecure_skip_verify)

    if HTTP_PROXY:
        print(f"🌐 代理: {HTTP_PROXY}")
    if args.insecure_skip_verify:
        print("⚠️  已跳过 HTTPS 证书校验")

    records = load_sso_list(args.sso, args.sso_cookie, email=args.email)
    if not records:
        ap.error("需要 --sso 或 --sso-cookie")

    if len(records) > 1 and not args.out_dir and not args.merge:
        # 默认批量写目录
        args.out_dir = args.out_dir or "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")

    if args.out is None and args.out_dir is None and len(records) == 1:
        record_email = args.email.strip() or records[0].get("email", "")
        name = f"xai-{safe_filename_part(record_email)}.json" if record_email else "xai-auth.json"
        args.out = name

    print(f"🚀 SSO → CPA auth json: {len(records)} 个, delay={args.delay}s")
    ok = 0
    fail = 0

    for i, record in enumerate(records, 1):
        sso = record["sso"]
        email = args.email.strip() or record.get("email", "")
        print(f"\n{'=' * 60}\n[{i}/{len(records)}] {email or '...'}\n{'=' * 60}")
        try:
            token = sso_to_token(sso)
            if not token:
                fail += 1
                print(f"  ❌ [{i}] 失败")
                continue
            uid, entry = token_to_auth_entry(token, sso_cookie=sso, email=email)
            entry_key = email or uid or secrets.token_hex(4)

            if args.out_dir:
                file_id = safe_filename_part(email) if email else safe_filename_part(uid)
                p = Path(args.out_dir) / f"xai-{file_id}.json"
                write_auth_json(p, entry)
                print(f"  💾 {p}")
            if args.out:
                if args.merge or len(records) > 1:
                    merge_auth_json(Path(args.out), entry_key, entry)
                    print(f"  💾 merge → {args.out}")
                else:
                    write_auth_json(Path(args.out), entry)
                    print(f"  💾 {args.out}")

            ok += 1
            print(f"  ✅ [{i}] 完成 sub={uid[:12]}...")
        except Exception as e:
            fail += 1
            print(f"  ❌ [{i}] 异常: {e}")

        if args.delay > 0 and i < len(records):
            time.sleep(args.delay)

    print(f"\n{'=' * 60}\n📊 完成: {ok}/{len(records)} 成功, {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
