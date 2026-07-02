from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


security = HTTPBasic(auto_error=False)


def _admin_username() -> str:
    return os.getenv("GROK_HELPER_ADMIN_USERNAME", "admin").strip() or "admin"


def _admin_password() -> str:
    return os.getenv("GROK_HELPER_ADMIN_PASSWORD", "").strip()


def admin_password_configured() -> bool:
    return bool(_admin_password())


def verify_admin_credentials(credentials: HTTPBasicCredentials | None) -> bool:
    expected_password = _admin_password()
    if not expected_password or credentials is None:
        return False

    username_ok = secrets.compare_digest(credentials.username, _admin_username())
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    return username_ok and password_ok


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    expected_password = _admin_password()
    if not expected_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password is not configured",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    if not verify_admin_credentials(credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
