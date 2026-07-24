# MediaPush

115 网盘分享 → TMDB 元数据匹配 → Telegram 频道推送，带 Web 管理后台。

定时扫描监控的 115 分享链接 → 解析文件名（季/集/画质/音频）→ 匹配 TMDB 元数据 → 去重入库 → 渲染卡片推送 Telegram 频道。全部业务配置通过 Web 后台管理、热生效。

## 功能

- **定时扫描**：APScheduler 定时扫描监控的 115 分享，全量/增量扫描自动交替（默认 2 小时全量校准一次）。
- **文件名解析**：季/集（跨度）/年份/画质/音频/ed2k 链接，噪音词前置清理。
- **TMDB 匹配**：搜索带年份无结果回退不带年份；整季文件夹从季 `episode_count` 补集数；连载中缓存 3 天 / 完结 30 天。
- **去重入库**：批量 200 对/批查重，避免 SQLite 表达式树深度超限。
- **Telegram 推送**：海报走 `send_photo + caption`，无海报走 `send_message`；HTML 转义；完整版 ≤4096 / 紧凑版 ≤1024 字符。
- **Web 管理后台**：仪表盘、配置管理（热生效）、任务触发/停止、日志查看、分享列表、TMDB 缓存管理。
- **加固**：115/TMDB 调用指数退避重试（永久错误不重试）；rate-limit 关键词监控告警；watchdog 监控 pipeline 停滞与 bot 重建。

## 架构

单进程多组件共享一个 asyncio loop：Uvicorn（FastAPI）+ Telegram Bot（python-telegram-bot）+ APScheduler。

```
┌─────────────────────────────────────────────────────┐
│   Uvicorn(FastAPI)  +  PTB Bot  +  APScheduler       │
│                       (单 asyncio loop)              │
│                          ▼                            │
│                   Container (依赖注入)                │
│        ┌──────────────┬──────────────┬──────────┐    │
│        ▼              ▼              ▼          ▼    │
│   Pan115Service   TmdbService    Pipeline   Watchdog │
│        └──────────────┴──────────────┘          │    │
│                      ▼                              │
│               SQLite (SQLAlchemy 2.0 async)         │
└─────────────────────────────────────────────────────┘
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 部署

支持两种方式：**Docker Compose**（默认）与 **systemd 直装**。

### 方式一：Docker Compose（默认）

```bash
docker compose up -d --build
```

- 端口 `8088:8088`，数据持久化在 `./data/`（DB + 日志）。
- 容器内通过 `host.docker.internal` 访问宿主机代理（compose 已配 `extra_hosts`）。
- 首次启动会自动生成管理员密码并打印到日志：
  ```bash
  docker compose logs mediapush | grep "管理员密码"
  ```

### 方式二：systemd 直装

适合偏好系统直装、需直接访问宿主机本机代理（`127.0.0.1`）的场景。

```bash
# 在仓库根目录执行（需 sudo）
sudo bash deploy/install.sh
```

`install.sh` 会：
1. 在 `/opt/mediapush`（可用第一个参数覆盖）创建安装目录、拷贝代码
2. 创建 `mediapush` 系统用户与 venv，安装 `requirements.txt` 依赖
3. 渲染并安装 `mediapush.service` 到 systemd，enable + start

常用运维命令：
```bash
sudo systemctl status mediapush        # 状态
sudo systemctl restart mediapush       # 重启（改 log_level 后生效）
sudo journalctl -u mediapush -f        # 实时日志（也见 /opt/mediapush/data/mediapush.log）
sudo journalctl -u mediapush | grep "管理员密码"   # 首次密码
sudo bash deploy/install.sh --uninstall        # 卸载（保留 data/）
```

直装场景在 Web 后台「配置管理」页把代理地址填 `127.0.0.1:<port>`（宿主机本机代理）。

## 配置体系

**Web 是配置的唯一入口**。除让进程能启动的最小集外，所有业务配置在 Web 后台维护、持久化到 DB。

### 启动配置（非敏感，写 docker-compose / systemd）

| 项 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/mediapush.db` | 数据库 |
| `WEB_HOST` | `0.0.0.0` | 监听地址 |
| `WEB_PORT` | `8088` | 监听端口 |

### 业务配置（Web 后台管理，热生效）

| 项 | 说明 |
|---|---|
| `admin_password` | 管理员密码（首次自动生成，可在配置页改） |
| `tg_bot_token` / `tg_chat_id` | TG Bot Token 与推送频道/群 ID |
| `pan115_cookie` | 115 Cookie（改后触发 pan115 client 重建） |
| `tmdb_api_key` | TMDB API Key |
| `schedule_interval` | 调度间隔（分钟，推荐 5-10；<3 告警） |
| `full_scan_interval_runs` / `pan115_health_interval` | 联动参数（改间隔自动重算） |
| `log_level` | 日志级别（重启生效） |
| `proxy_enabled` / `proxy_url` / `proxy_targets` | 代理：tg/tmdb 默认走，115 默认不走 |
| `monitored_shares` | 监控分享列表（逗号分隔 `code:password` 或 `code`） |

配置页密钥类字段（密码/cookie/token/key）留空提交表示不修改；checkbox `proxy_enabled` 勾选=开启。

## 首次使用

1. 部署后访问 `http://<host>:8088/health` 确认服务起来。
2. 查日志拿到首次生成的管理员密码，登录 `http://<host>:8088/login`。
3. 在「配置管理」填入：TG Bot Token、TG Chat ID、115 Cookie、TMDB API Key、代理（如需）、调度间隔、监控分享列表。
4. 保存后自动热生效（TG 配置后 bot 自动拉起；调度立即按新间隔运行）。
5. 在「任务」页可手动触发 pipeline，或在 Telegram 给 bot 发 `/find` 立即扫描。

## Telegram Bot 命令

| 命令 | 说明 |
|---|---|
| `/start` `/help` | 帮助 |
| `/status` | 服务状态（bot/pipeline/未推送数/配置健康） |
| `/115 <链接或分享码>` | 解析 115 分享并推送（支持 URL+`password=`、URL+尾 token、8+ 字符裸码） |
| `/find` | 立即扫描监控分享列表 |
| `/refresh <tmdb_id>` | 刷新 TMDB 缓存 |
| `/stop` | 停止当前运行中的 pipeline |

## 开发

```bash
pip install -e ".[dev]"      # 或用 .venv
pytest                       # 178 项测试
ruff check .                 # lint
```

目录结构（核心）：
```
app/
├── core/        # container / config / proxy / logging / retry / monitor
├── db/          # base / models / repository
├── parser/      # filename 文件名解析
├── pan115/      # client / service（p115client 封装）
├── tmdb/        # client / cache / service
├── pipeline/    # pipeline / context（可取消）
├── scheduler/   # scheduler / watchdog
├── telegram/    # bot / handlers / pusher
└── web/         # app / auth / routes / templates / static
deploy/          # mediapush.service + install.sh（systemd 直装）
docs/            # ARCHITECTURE.md
```

## 排错

- **TG Bot 无响应**：检查代理是否配置（TG API 走代理）；watchdog 会自动重建 bot，永久故障（token 错误等）10 分钟冷却并告警。
- **115 扫描返回空**：确认分享访问码已随 `monitored_shares` 配置（`code:password`），有密码的分享不传访问码会返回空。
- **115 cookie 失效**：Web 后台「配置管理」更新 cookie，热生效触发 client 重建；pan115 健康检查失败会在日志告警。
- **TMDB 匹配不到**：文件名年份可能是制作/资源年份，会自动回退不带年份重搜；整季文件夹从 TMDB 季集数补充。
- **rate-limit 告警刷屏**：日志命中 `frequent/rate limit/风控` 关键词会告警（30 分钟冷却），建议在配置页把调度间隔调大到 10-15 分钟。
- **`/` 返回 JSON 不是后台页**：后台页在 `/dashboard`，`/` 是健康接口（保留启动期契约）。
