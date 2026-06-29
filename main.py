from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# 先加载 .env，再导入注册模块；注册模块会在导入期读取路径和并发配置。
load_dotenv()

from grok_helper.auth import require_admin
from grok_helper.logger import logger, setup_logging
from grok_helper.register import router as register_router
from grok_helper.register import start_register_supervisor, stop_register_supervisor


setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 注册服务只需要启动任务 supervisor，不再加载 grok2api 的账号、模型和代理目录。
    logger.info("register service startup")
    start_register_supervisor()
    try:
        yield
    finally:
        logger.info("register service shutdown")
        stop_register_supervisor()


def create_app() -> FastAPI:
    app = FastAPI(title="Grok Register", version="0.1.0", lifespan=lifespan)
    app.include_router(register_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # 静态文件服务
    _statics_dir = Path(__file__).resolve().parent / "app" / "statics"
    if _statics_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_statics_dir)), name="static")

    # 管理页面路由
    @app.get("/admin/register", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    async def admin_register_page():
        html_file = _statics_dir / "admin" / "register.html"
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Admin page not found</h1>", status_code=404)

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login_page():
        html_file = _statics_dir / "admin" / "login.html"
        if html_file.exists():
            return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Login page not found</h1>", status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return RedirectResponse(url="/admin/register")

    return app


app = create_app()
