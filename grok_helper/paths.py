from __future__ import annotations

import os
from pathlib import Path


_ROOT_DIR = Path(__file__).resolve().parents[1]


def project_root() -> Path:
    """返回独立项目根目录。"""
    return _ROOT_DIR


def _resolve_env_path(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip() or default
    path = Path(raw)
    if not path.is_absolute():
        path = _ROOT_DIR / path
    return path


def data_dir() -> Path:
    return _resolve_env_path("DATA_DIR", "/app/data")


def log_dir() -> Path:
    return _resolve_env_path("LOG_DIR", "/app/logs")


def data_path(*parts: str) -> Path:
    relative_parts = parts
    if relative_parts and relative_parts[0] == "register":
        relative_parts = relative_parts[1:]
    return data_dir().joinpath("register", *relative_parts)


def log_path(*parts: str) -> Path:
    return log_dir().joinpath(*parts)


__all__ = ["data_dir", "log_dir", "data_path", "log_path", "project_root"]
