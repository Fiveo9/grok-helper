# Grok Helper

批量 Grok 账号注册服务，基于 FastAPI + DrissionPage 构建。

## 功能特性

- 🚀 批量自动注册 Grok 账号
- 🌐 Web 管理控制台
- 📧 支持多种临时邮箱服务（CloudMail、DuckMail、Mail.tm）
- 🔄 自动推送 SSO Token 到 API
- 🛡️ 内置 Turnstile 验证码绕过
- 📊 任务状态实时监控
- 🐳 Docker 一键部署

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/grok-helper.git
cd grok-helper
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，至少修改 GROK_HELPER_ADMIN_PASSWORD 和必要的注册配置
```

### 3. 启动服务

```bash
docker compose up -d --build
```

### 4. 访问管理后台

使用 Docker Compose 启动时，默认宿主端口为 `8001`：

```bash
http://localhost:8001/admin/register
```

默认访问地址为：

```bash
http://localhost:8001/admin/register
```

浏览器会显示内置管理登录页，默认用户名为 `.env` 里的 `GROK_HELPER_ADMIN_USERNAME`，密码为 `GROK_HELPER_ADMIN_PASSWORD`。登录成功后前端会为管理 API 请求附带 HTTP Basic 凭据。

## GitHub 手动构建

项目内置了手动触发的 GitHub Actions workflow，可在 GitHub 页面构建 Docker 镜像：

1. 打开仓库的 `Actions` 页面。
2. 选择 `Manual Docker Build`。
3. 点击 `Run workflow`。
4. 按需填写：
   - `image_name`: 镜像名称，默认 `grok-helper`
   - `image_tag`: 镜像标签，默认 `latest`
   - `push_to_ghcr`: 填 `true` 或 `yes` 时推送到 GitHub Container Registry
5. 构建完成后，在 workflow run 的 `Artifacts` 区域下载 Docker 镜像 `.tar` 文件。

下载后可在本地导入：

```bash
docker load -i grok-helper-latest-docker-image.tar
```

## 配置说明

### 必填配置

| 变量 | 说明 |
|------|------|
| `GROK_HELPER_ADMIN_PASSWORD` | 管理控制台 HTTP Basic 密码 |
| `GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE` | 临时邮箱 API 地址 |
| `GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_EMAIL` | 邮箱管理员账号 |
| `GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD` | 邮箱管理员密码 |
| `GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN` | 邮箱域名 |
| `GROK_REGISTER_DEFAULT_API_ENDPOINT` | SSO Token 推送地址 |
| `GROK_REGISTER_DEFAULT_API_TOKEN` | API 认证 Token |

### 可选配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GROK_HELPER_ADMIN_USERNAME` | 管理控制台 HTTP Basic 用户名 | admin |
| `GROK_REGISTER_DEFAULT_RUN_COUNT` | 每次任务注册数量 | 50 |
| `GROK_REGISTER_DEFAULT_PROXY` | API 请求代理 | 空 |
| `GROK_REGISTER_DEFAULT_BROWSER_PROXY` | 浏览器代理 | 空 |
| `GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS` | 最大并发任务数 | 1 |
| `GROK_REGISTER_CPA_EXPORT_ENABLED` | 注册成功后把 sso 换成 CPA xai auth json | true |
| `GROK_REGISTER_CPA_AUTH_DIR` | CPA auth 目录（容器内路径），设置后自动写入 `xai-*.json` | 空 |

### CPA auth 自动导出

注册成功后，会用 sso cookie 走 xAI 的 OAuth Device Flow 换取 `access_token` / `refresh_token`，
生成 CPA（cli-proxy-api）可直接导入的 `xai-<email>.json`：

- 始终写一份到任务目录 `data/register/tasks/task_<id>/sso/cpa_auths/`；
- 若设置了 `GROK_REGISTER_CPA_AUTH_DIR`，再写一份到该目录（供 CPA 直接识别）；
- 任务结束时把该任务的所有 json 打包成 `cpa_xai_auth_import.tar.gz`，方便下载/迁移。

要让 CPA 自动识别，在 `docker-compose.yml` 里放开 CPA auth 目录挂载并设置环境变量：

```yaml
environment:
  GROK_REGISTER_CPA_EXPORT_ENABLED: "true"
  GROK_REGISTER_CPA_AUTH_DIR: /app/cpa-auths
volumes:
  - /opt/cli-proxy-api-official/auths:/app/cpa-auths
```

> Device Flow 需要逐个账号轮询换取，会给每轮注册增加数秒开销。若不需要实时写入，
> 可设 `GROK_REGISTER_CPA_EXPORT_ENABLED=false`，改用 CLI 事后批量转换：
> `python sso_to_cpa.py --sso data/register/tasks/task_<id>/sso/task_<id>.txt --out-dir ./cpa`

## 项目结构

```
grok-helper/
├── grok_helper/             # 核心模块
│   ├── register.py         # 任务管理与 API
│   ├── logger.py           # 日志配置
│   └── paths.py            # 路径配置
├── app/statics/            # 前端静态文件
├── email_register.py       # 临时邮箱注册逻辑
├── DrissionPage_example.py # 浏览器自动化注册
├── sso_to_cpa.py           # sso → CPA xai auth json（库 + CLI）
├── turnstilePatch/         # Turnstile 绕过脚本
├── main.py                 # FastAPI 入口
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/admin/register/meta` | 获取控制台元信息 |
| `GET` | `/admin/register/health` | 获取注册链路健康检查 |
| `GET` | `/admin/register/settings` | 获取系统配置 |
| `POST` | `/admin/register/settings` | 更新系统配置 |
| `POST` | `/admin/register/tasks` | 创建注册任务 |
| `GET` | `/admin/register/tasks` | 获取任务列表 |
| `GET` | `/admin/register/tasks/{id}` | 获取任务详情 |
| `GET` | `/admin/register/tasks/{id}/logs` | 获取任务日志 |
| `POST` | `/admin/register/tasks/{id}/stop` | 停止任务 |
| `DELETE` | `/admin/register/tasks/{id}` | 删除任务 |

除 `/health` 和管理登录/页面壳外，`/admin/register/*` API 都需要 HTTP Basic 认证。

## 技术栈

- **后端**: FastAPI + Granian
- **浏览器自动化**: DrissionPage (Chromium)
- **临时邮箱**: CloudMail / DuckMail / Mail.tm
- **数据库**: SQLite
- **前端**: 原生 HTML/CSS/JS

## License

MIT License
