# MediaPush 架构设计

> 版本：v1.0
> 日期：2026-07-23
> 状态：已实现（阶段 0–6 全部完成，178 项测试通过）

---

## 1. 项目定位

### 1.1 核心能力
定时扫描 115 网盘分享 → 解析文件名 → 匹配 TMDB 元数据 → 去重入库 → 推送 Telegram 频道。

### 1.2 扩展能力（本期新增）
**Web 管理后台**：在线配置、日志查看、统计看板、手动触发/停止任务、TMDB 缓存管理。

### 1.3 非目标（本期不做）
- 多网盘源（夸克/阿里/百度）
- 多推送渠道（微信/飞书/邮件）
- 用户订阅与关键词过滤
- 多用户系统

> 这些方向通过"源 / 推送器"抽象接口预留扩展点，但本期不实现。

---

## 2. 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 运行时 | Python 3.11+ | asyncio |
| Web 框架 | FastAPI + Uvicorn | ASGI，自带 OpenAPI |
| 模板 | Jinja2 | 服务端渲染管理后台 |
| TG Bot | python-telegram-bot v20+ | 异步，`concurrent_updates(True)` |
| 调度 | APScheduler AsyncIOScheduler | 与 uvicorn 同 loop |
| ORM | SQLAlchemy 2.0（async） | 便于将来切换 PG |
| 数据库 | SQLite | 单机零运维 |
| 115 客户端 | p115client（最新版） | 容错导入 |
| HTTP | httpx | TMDB API |
| 配置 | pydantic-settings + DB | 环境变量 + 持久化 |
| 日志 | logging + structlog 风格 | 结构化 |

---

## 3. 架构总览

### 3.1 单进程多组件

```
┌─────────────────────────────────────────────────────────────┐
│                     MediaPush 进程（单 asyncio loop）         │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │  Uvicorn     │   │  PTB Bot     │   │  APScheduler   │  │
│  │  (FastAPI)   │   │  polling     │   │  AsyncIO       │  │
│  └──────┬───────┘   └──────┬───────┘   └───────┬────────┘  │
│         │                  │                   │           │
│         └──────────────────┼───────────────────┘           │
│                            ▼                               │
│                   ┌─────────────────┐                      │
│                   │   Container     │  依赖注入，共享实例   │
│                   │  (服务注册表)    │                      │
│                   └────────┬────────┘                      │
│          ┌─────────────────┼─────────────────┐             │
│          ▼                 ▼                 ▼             │
│   ┌────────────┐   ┌────────────┐   ┌────────────┐        │
│   │ Pan115Svc  │   │  TmdbSvc   │   │ Pipeline   │        │
│   └────────────┘   └────────────┘   └────────────┘        │
│          ┌─────────────────┼─────────────────┐             │
│          ▼                 ▼                 ▼             │
│   ┌────────────┐   ┌────────────┐   ┌────────────┐        │
│   │  Parser    │   │ Repository │   │  Watchdog  │        │
│   └────────────┘   └────────────┘   └────────────┘        │
│                            │                               │
│                            ▼                               │
│                   ┌─────────────────┐                      │
│                   │   SQLite (DB)   │                      │
│                   └─────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 启动顺序
1. 加载配置（env → DB 覆盖）
2. 初始化 DB engine / 建表
3. Container 实例化各服务（Pan115 / Tmdb / Repository / Pipeline）
4. 启动 Scheduler（注册 pipeline / full_scan / health / watchdog 任务）
5. 启动 PTB Bot polling（`concurrent_updates(True)`）
6. Uvicorn serve FastAPI（主循环阻塞在此）

### 3.3 关闭顺序（lifecycle）
> 旧项目教训：`reset_all` 必须先于 `close_db`，确保关闭流程可访问 DB。

1. Scheduler.shutdown(wait=False)
2. Bot.stop() / Bot.shutdown()
3. 各服务 reset_all（关闭 p115client 连接等）
4. Repository flush
5. DB engine dispose

---

## 4. 目录结构

```
mediapush/
├── app/
│   ├── __init__.py
│   ├── main.py                 # 进程入口：组装 Container + 启动 uvicorn
│   ├── core/
│   │   ├── config.py           # Settings（pydantic）+ 配置持久化读写
│   │   ├── container.py        # 依赖注入容器，服务实例化与 reset
│   │   ├── lifecycle.py        # 启动/关闭编排
│   │   └── logging.py          # 日志配置
│   ├── db/
│   │   ├── base.py             # AsyncEngine / async_sessionmaker
│   │   ├── models.py           # ORM 模型
│   │   └── repository.py       # 数据访问层（批量去重等）
│   ├── pan115/
│   │   ├── client.py           # p115client 封装（容错导入、relogin）
│   │   └── service.py          # 分享扫描：share_iterdir_walk
│   ├── tmdb/
│   │   ├── client.py           # TMDB API（httpx）
│   │   ├── cache.py            # 缓存（过期策略 / refresh）
│   │   └── service.py          # 搜索、季集数补充
│   ├── parser/
│   │   └── filename.py         # 文件名解析（季/集/画质/音频/ed2k）
│   ├── pipeline/
│   │   ├── pipeline.py         # 流水线编排（可取消，FIRST_COMPLETED）
│   │   └── context.py          # 运行态（_stop_event / is_running / 进度）
│   ├── scheduler/
│   │   ├── scheduler.py        # AsyncIOScheduler 封装
│   │   └── watchdog.py         # 看门狗（永久性故障冷却）
│   ├── telegram/
│   │   ├── bot.py              # PTB Application 构建
│   │   ├── handlers.py         # 命令处理（Pan115Error 容错导入）
│   │   └── pusher.py           # 推送卡片渲染
│   ├── web/
│   │   ├── app.py              # FastAPI 工厂
│   │   ├── auth.py             # 管理后台登录（简单 session）
│   │   ├── deps.py             # 依赖注入（Container / 认证）
│   │   ├── routes/
│   │   │   ├── dashboard.py    # 仪表盘统计
│   │   │   ├── config.py       # 配置管理
│   │   │   ├── tasks.py        # 任务历史 / 手动触发 / 停止
│   │   │   ├── logs.py         # 日志查看
│   │   │   ├── shares.py       # 分享列表
│   │   │   └── tmdb_cache.py   # TMDB 缓存管理
│   │   ├── templates/          # Jinja2 模板
│   │   └── static/             # css/js
│   └── api/                    # 内部 REST（供前端/外部调用，可选）
│       └── schemas.py
├── tests/
├── docs/
│   └── ARCHITECTURE.md
├── pyproject.toml
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .gitignore
└── README.md
```

---

## 5. 模块职责

### 5.1 core/config
- `Settings`（pydantic-settings）：从 `.env` / 环境变量加载启动配置（DB 路径、TG token、监听端口、管理员密码等）。
- 运行时可调配置（调度间隔、cookie、TMDB key、全量扫描周期等）持久化在 DB `app_config` 表，Web 可改、热生效。
- 配置变更通过 `Container` 通知相关组件（scheduler.update_interval / pan115.reset）。

### 5.2 core/container
- 依赖注入容器，持有所有服务单例。
- 提供 `reset_pan115()`（async，更新 cookie 后重建 client）。
- 暴露公共接口，**禁止其他层访问服务私有属性**（旧项目约束）。

### 5.3 core/lifecycle
- 编排启动与关闭顺序（见 3.2 / 3.3）。
- 信号处理（SIGTERM/SIGINT）→ 优雅关闭。

### 5.4 db
- `models.py`：ORM 模型（见第 6 节）。
- `repository.py`：数据访问。**去重查询批量 200 对/批**，避免 SQLite 表达式树深度超限（旧项目约束）。
- 使用 async session。

### 5.5 pan115
- `client.py`：封装 p115client。
  - **tool 模块函数逐个容错导入**，避免新版移除/重命名导致整服务崩溃。
  - `fs_files_iter` 优先，回退 `iter_fs_files`。
  - `user_info(uid=client.user_id)`、`fs_info`（非 `fs_info_app`）、`share_info`（非 `share_info_app`）。
  - `login_another_app(replace=True)` 返回新 P115Client 实例，需重建 client 维持 webapi 端点。
  - 405 错误不重试（永久错误）。
- `service.py`：分享扫描。
  - `share_iterdir_walk(receive_code=...)`，**第三位置参数传访问码**，`app='web'`（android 端已失效）。
  - **share_snap 已废弃（405），统一用 share_iterdir_walk。**
  - 内存缓存键含 `password`，避免同 share_code 不同访问码命中错误缓存。

### 5.6 tmdb
- `client.py`：httpx 调 TMDB API。
- `cache.py`：
  - **连载中剧集缓存 3 天，已完结 30 天**（旧项目教训：30 天缓存导致集数不更新）。
  - upsert 时刷新缓存时间戳。
- `service.py`：
  - 搜索带年份无结果时**回退不带年份**重搜。
  - **整季文件夹（如 S01）从 TMDB 季 `episode_count` 补集数**，跳过 season 0 特别篇，取第一个正剧季。
  - `total_episodes` 用 `media_data['number_of_episodes']`（TMDB），回退 `media_data['total_episodes']`（文件名）。

### 5.7 parser
- 文件名解析：季、集（跨度）、年份、画质、音频、ed2k 链接。
- **噪音词前置清理**（声道/帧率/色深）。
- 音频标签 `\bAAC\d*(?:\.\d+)?\b`（支持 AAC2.0/DTS5.1）。
- 清理非正式画质标记（HQ/HD/FINE）。
- **ed2k 正则非贪婪匹配到 `|/`**：`ed2k://.*?\|/`。
- `extract_season_episode` 第三返回值是"集跨度"，`ep_end = ep + ep_span - 1`。

### 5.8 pipeline
- 编排：扫描新分享 → 解析 → TMDB 匹配 → 去重入库 → 推送。
- **取消机制**：`asyncio.wait(tasks, return_when=FIRST_COMPLETED)` 竞速 gather_task 与 stop_task，`_stop_event` 在 `_fetch_one` 入口检查，停止时取消剩余任务，1-2 秒内中断。
- 运行态 `context`：`is_running`、`_stop_event`、进度、当前任务 ID。
- 全量扫描 vs 增量扫描由 `full_scan_interval_runs` 控制。

### 5.9 scheduler
- AsyncIOScheduler，与 uvicorn 同 loop。
- **`max_instances=1` + `coalesce=True`**，防止并发执行与任务积压风暴。
- **`misfire_grace_time = max(60, interval*60)`**，防止高频调度丢任务。
- **跳过执行（is_running）时也要更新 `_last_pipeline_execution`**，防止 watchdog 误报超时。
- **间隔联动自适应**：`schedule_interval` 变化时自动重算并持久化：
  - `full_scan_interval_runs = max(1, round(120/interval))`（目标 2 小时全量校准）
  - `pan115_health_interval = min(max(interval*60, 180), 900)`（跟随间隔，clamp [180s, 900s]）
  - `update_interval` 返回 `(full_scan, health)` 供展示
  - 用户单独改某项后，只要不再改间隔就不会被联动覆盖
- **`schedule_interval < 3 分钟告警**（流水线耗时可能超间隔 + 115 风控）。
- 启动时 `_validate_interval_on_start` 校验间隔合理性 + 联动参数匹配度，仅告警不覆盖。
- 推荐间隔 5-10 分钟。
- **rate-limit 监控任务**（阶段 6）：每 5 分钟扫描内存日志缓冲，命中 `rate limit / frequent / 风控` 等关键词且过 30 分钟冷却则通过 Telegram 告警管理员（见 5.14）。

### 5.10 watchdog
- 监控 `_last_pipeline_execution`，超时阈值 **`max(interval*3, 30)` 分钟**。
- 重建 bot 失败**统一指数退避**，禁止"网络正常就立即重建"跳过退避。
- **区分永久性故障**（ImportError/ModuleNotFoundError/AttributeError）与瞬时故障：永久性抛 `_PermanentRebuildFailure`，上层 10 分钟冷却并告警管理员，不重试。

### 5.11 telegram
- `bot.py`：构建 PTB Application，**`concurrent_updates(True)`**（PTB v20+ 默认串行，长 handler 会阻塞整个 update 队列导致 TG 交互断联）。
- `handlers.py`：命令（/find /115 /refresh /stop /status /set 等）。
  - **顶部 `Pan115Error` 必须 try/except 容错导入**：pan115_service 顶部硬依赖 p115client，p115client 装坏时整条 import 链崩，连带 `TelegramService.start()` 失败导致 bot 完全不可用；fallback 到 `Exception` 子类保留 except 语义。
- `pusher.py`：推送卡片渲染（标题/季集/画质/TMDB 信息）。

### 5.12 web
- `app.py`：FastAPI 工厂，挂载 routes / static / templates。
- `auth.py`：管理员登录（简单 session/cookie，密码来自配置）。可选 Bearer token。
- 页面见第 7 节。
- **API 层只通过 Container 公共接口访问服务，不访问私有属性。**

### 5.13 core/proxy（代理管理）
- 统一代理配置：从 `app_config` 读取 `proxy_enabled` / `proxy_url` / `proxy_targets`。
- 按目标分发：`tg` / `tmdb` 默认走代理（境外服务必须）；`115` 默认不走（国内服务，走代理易触发风控），可在 Web 勾选开启。
- 注入方式：
  - TMDB：`httpx.AsyncClient(proxy=proxy_url)`。
  - TG：`ApplicationBuilder().proxy(proxy_url).get_updates_proxy(proxy_url)`。
  - 115：p115client 底层 httpx，按 `proxy_targets` 决定是否注入代理。
- 代理配置改后 → `Container.on_config_changed` 触发相关 client 重建（bot / tmdb / pan115）。
- 首次未配置代理 → TG/TMDB 连接失败，Web 引导配置。

### 5.14 core/monitor + core/retry（阶段 6 加固）
- `core/retry.py`：`retry_async(coro_factory, retries, base_delay, max_delay, is_transient, label)` 指数退避重试。
  - `is_transient` 返回 False 的异常（永久错误，如 115 的 405、TMDB 4xx）立即抛出不重试。
  - 应用于：pan115 `share_info` / `iter_share_files`（pre-yield 重试，已产出后不重试避免重复）、tmdb `_get`（429/5xx/超时重试）。
- `core/monitor.py`：`RateLimitMonitor` 扫描 `memory_handler.recent(500)`，命中关键词且过 30 分钟冷却则 `container.send_alert` 告警管理员。告警失败（bot 未运行）不设冷却，下次仍尝试。
- `container.send_alert(text)`：通过配置的 `tg_chat_id` 发送，bot 未运行/未配置 chat_id 返回 False 不抛。
- Web 全局异常处理器：`@app.exception_handler(Exception)` 记录日志并返回友好 500 页（HTML/JSON）。

---

## 6. 数据模型

### 6.1 `share`（分享记录）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| share_code | str | 115 分享码 |
| share_password | str | 访问码 |
| title | str | 分享标题 |
| status | str | **显式转字符串** |
| create_time | str | **显式转字符串** |
| file_count | int | |
| size | int | |
| raw_files | json | 原始文件列表 |
| media_id | FK → media | 关联元数据 |
| pushed | bool | 是否已推送 |
| pushed_at | datetime | |
| created_at | datetime | 入库时间 |
| updated_at | datetime | |

> 唯一约束：(share_code, share_password)

### 6.2 `media`（元数据）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| tmdb_id | int | TMDB ID |
| media_type | str | tv/movie |
| title | str | |
| original_title | str | |
| year | int | |
| season | int | |
| episode_start | int | |
| episode_end | int | ep_end = ep + ep_span - 1 |
| total_episodes | int | number_of_episodes 回退文件名 |
| quality | str | |
| audio | str | |
| overview | text | |
| poster_path | str | |

### 6.3 `tmdb_cache`（TMDB 缓存）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| tmdb_id | int | |
| media_type | str | |
| data | json | 原始 TMDB 响应 |
| ongoing | bool | 是否连载中（决定过期天数） |
| cached_at | datetime | upsert 时刷新 |
| expires_at | datetime | ongoing=3天 / 完结=30天 |

### 6.4 `task_log`（任务记录，Web 后台展示）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| task_type | str | pipeline / full_scan / health / manual |
| status | str | running / success / failed / cancelled |
| started_at | datetime | |
| finished_at | datetime | |
| duration_ms | int | |
| shares_new | int | 新增分享数 |
| shares_pushed | int | 推送数 |
| error | text | 失败原因 |
| trigger | str | scheduler / manual / webhook |

### 6.5 `app_config`（运行时可调配置）
| 字段 | 类型 | 说明 |
|---|---|---|
| key | str PK | schedule_interval / cookie / tmdb_api_key / ... |
| value | text | JSON 编码 |
| updated_at | datetime | |

---

## 7. Web 管理后台

### 7.1 页面
| 路由 | 功能 |
|---|---|
| `/login` | 管理员登录 |
| `/` | 仪表盘：今日推送数、扫描数、最近任务、健康状态 |
| `/config` | 配置管理：代理、调度间隔、cookie、TMDB key、TG 配置，在线改 + 热生效 |
| `/tasks` | 任务历史列表 + 手动触发 pipeline / 停止运行中任务 |
| `/logs` | 日志查看（按级别/关键词过滤，倒序） |
| `/shares` | 已入库分享列表（搜索/筛选/分页） |
| `/tmdb_cache` | TMDB 缓存列表 + 刷新（等价 `/refresh <tmdb_id>`） |

### 7.2 技术要点
- Jinja2 模板 + 少量原生 JS（fetch 调内部 API），无构建步骤。
- 日志查看：读取日志文件 + 内存环形缓冲（最近 N 条）双源。
- 手动触发：调用 `Container.pipeline.run_once()`，与定时任务共享 `is_running` 互斥。
- 配置热生效：写 `app_config` → `Container.on_config_changed(key)` → 通知对应组件。

---

## 8. 关键设计约束（旧项目经验内置）

> 以下为旧项目踩坑总结，新设计必须遵守，避免重蹈覆辙。

**p115client 兼容性**
- 用最新版（>=0.0.9.4.7），不锁旧版；tool 模块函数逐个容错导入。
- `fs_files_iter` 优先回退 `iter_fs_files`；`fs_info`/`share_info`（非 `_app` 后缀）。
- `user_info(uid=client.user_id)` 显式传 uid。
- `login_another_app(replace=True)` 返回 P115Client 实例，relogin 后重建 client。
- `share_iterdir_walk` 传 `receive_code` 第三位置参数，`app='web'`。
- `share_snap` 已废弃（405），用 `share_iterdir_walk`；405 不重试。

**调度器**
- `max_instances=1` + `coalesce=True` + `misfire_grace_time=max(60, interval*60)`。
- 跳过执行时也更新 `_last_pipeline_execution`。
- 间隔联动自适应（full_scan / health）。
- 推荐间隔 5-10 分钟；<3 分钟告警。

**watchdog**
- 超时阈值 `max(interval*3, 30)` 分钟。
- 重建失败统一指数退避，区分永久性故障（10 分钟冷却 + 告警）。

**Telegram**
- `concurrent_updates(True)`。
- handlers 顶部 `Pan115Error` 容错导入。
- pipeline 取消用 `asyncio.wait(FIRST_COMPLETED)`，1-2 秒中断。

**数据/解析**
- `ShareDetail.status`/`create_time` 显式转字符串。
- `total_episodes` 用 TMDB `number_of_episodes` 回退文件名。
- 去重批量 200 对/批。
- TMDB 搜索年份回退；整季文件夹从季 `episode_count` 补集数（跳过 season 0）。
- 文件名噪音词/音频标签/画质标记清理；ed2k 非贪婪 `|/`。
- `extract_season_episode` 第三返回值是集跨度，`ep_end = ep + ep_span - 1`。
- 缓存键含 password；连载中 3 天 / 完结 30 天；upsert 刷新时间戳。

**代理与网络**
- 项目依赖代理访问境外服务（TG Bot API、TMDB API），代理配置走 Web 后台管理、热生效。
- TMDB（httpx）与 TG（PTB `proxy` + `get_updates_proxy`）注入代理；115 默认不走代理，可选。
- 代理改后触发相关 client 重建。
- Docker 容器内代理地址用 `host.docker.internal`（compose 配 `extra_hosts: host.docker.internal:host-gateway`），**不能写 127.0.0.1**（那是容器自身）。
- 首次未配置代理 → TG/TMDB 连接失败，Web 引导配置。

**工程**
- `reset_pan115` 为 async，`await container.reset_pan115()`。
- lifecycle：`reset_all` 先于 `close_db`。
- `TokenBucket` 构造校验 `max_rate > 0`。
- API 层不访问服务私有属性。
- `docker-compose.yml` 仅含 3 项非敏感启动配置，可提交 git；敏感配置全在 Web/DB，不入 git。
- 高频间隔监控 `frequent/rate limit` 关键词。

---

## 9. 配置项

> 原则：除"让 Web 进程能启动"的最小启动集外，所有配置统一在 Web 管理后台维护、持久化在 DB。**Web 是配置的唯一入口**。

### 9.1 启动配置（docker-compose.yml environment，仅最小启动集）
3 项均为非敏感项，直接写在 `docker-compose.yml` 的 `environment` 段，无需 `.env` 文件：
```yaml
environment:
  - DATABASE_URL=sqlite+aiosqlite:////app/data/mediapush.db
  - WEB_HOST=0.0.0.0
  - WEB_PORT=8088
```
- **管理员密码**：首次启动自动生成随机密码并打印到日志/控制台，用户登录 Web 后在「配置管理」页修改。
- **日志文件路径**：固定 `/app/data/mediapush.log`；日志级别走 Web 改（重启生效）。
- **数据持久化**：宿主机 `./data` 挂载到容器 `/app/data`，DB 与日志持久化在宿主机。

### 9.2 运行时配置（DB app_config，Web 管理后台统一管理）
全部业务配置在 Web 配置页增删改、热生效：
```
admin_password=...             # 管理员密码（可改）
tg_bot_token=...               # TG Bot Token
tg_chat_id=...                 # 推送频道/群 ID
pan115_cookie=...              # 115 Cookie（改后触发 reset_pan115）
tmdb_api_key=...               # TMDB API Key
schedule_interval=5            # 分钟，5-10 推荐
full_scan_interval_runs=24     # 2 小时全量一次
pan115_health_interval=300     # 秒
log_level=INFO                 # 日志级别（重启生效）
proxy_enabled=true             # 代理开关
proxy_url=http://host.docker.internal:7890  # 代理地址（容器内用 host.docker.internal 访问宿主机）
proxy_targets=tg,tmdb          # 走代理的服务：tg/tmdb/115，默认 tg+tmdb（115 国内默认不走）
```
- 首次启动这些项为空 → Web 后台显示「未配置」引导，用户填写保存即生效。
- `tg_bot_token` / `pan115_cookie` / `tmdb_api_key` 改后触发对应组件重建（bot / pan115 client）。
- `proxy_*` 改后按 `proxy_targets` 触发相关 client 重建（bot / tmdb / pan115）。
- 调度参数改后触发 `scheduler.update_interval` 联动重算 `full_scan_interval_runs` / `pan115_health_interval`。

---

## 10. 部署

支持两种部署方式：**Docker Compose 单容器**（默认）与 **systemd 直装**（贴合偏好系统直装、需直接访问宿主机代理/本机 127.0.0.1 的场景）。

### 10.1 Docker Compose（默认）
单容器部署单进程应用（uvicorn + PTB + scheduler 同 loop 跑在容器内）。
- Dockerfile：基镜像 `python:3.12-slim`，安装依赖，拷贝代码，`CMD uvicorn app.main:app --host 0.0.0.0 --port 8088`。
- docker-compose.yml：3 项启动配置写 `environment`（见 9.1）；volume 挂载 `./data:/app/data` 持久化 DB + 日志；端口映射 `8088:8088`；`extra_hosts: ["host.docker.internal:host-gateway"]` 让容器访问宿主机代理；`restart: unless-stopped`。
- 容器内代理地址用 `host.docker.internal`（**不能写 127.0.0.1**，那是容器自身）。

### 10.2 systemd 直装
- `deploy/mediapush.service`：systemd unit，`ExecStart` 调用 venv 内 uvicorn，`WorkingDirectory` 指向安装目录，`Environment` 注入最小启动集 + `LOG_FILE`。
- `deploy/install.sh`：创建 venv、装 `requirements.txt` 依赖、渲染并安装 systemd unit、enable + start。
- 直装场景代理地址填 `127.0.0.1:<port>`（宿主机本机代理，在 Web 后台配置）。
- `User=mediapush`，安装目录 `/opt/mediapush`（可由 `install.sh` 参数覆盖）。

### 10.3 备份
- 宿主机 `./data/` 目录（db + log）纳入备份。

---

## 11. 实现路线图

> 评审通过后按阶段实现，每阶段可独立验证。**全部阶段已实现完成（2026-07-23）。**

### 阶段 0：脚手架 ✅
- pyproject.toml / requirements / .gitignore / Dockerfile / docker-compose.yml
- 目录骨架 + main.py 空 uvicorn 启动
- DB engine + 建表 + 迁移
- logging 配置

### 阶段 1：核心数据与解析 ✅
- ORM 模型 + repository（含批量去重）
- parser 文件名解析 + 单测
- TMDB client + cache + service + 单测

### 阶段 2：115 与流水线 ✅
- pan115 client 封装（容错导入）+ service（share_iterdir_walk）
- pipeline 编排 + 可取消机制
- 端到端：扫描 → 入库（先不推送）

### 阶段 3：Telegram 推送 ✅
- PTB bot 构建（concurrent_updates）
- handlers（容错导入 Pan115Error）+ pusher 卡片
- 端到端：扫描 → 入库 → 推送

### 阶段 4：调度与看门狗 ✅
- scheduler（联动参数 / max_instances / misfire_grace_time）
- watchdog（指数退避 / 永久故障冷却）
- lifecycle 启停顺序

### 阶段 5：Web 管理后台 ✅
- FastAPI 工厂 + auth + 模板骨架
- 仪表盘 / 配置 / 任务 / 日志 / 分享 / TMDB 缓存
- 配置热生效

### 阶段 6：加固 ✅
- 错误处理与重试策略（`core/retry.py`，pan115/tmdb 指数退避，永久错误不重试）
- 监控指标（rate limit 关键词告警，`core/monitor.py` + scheduler 监控任务）
- 文档与 README（README 重写、ARCHITECTURE 状态更新）
- 部署脚本 / systemd unit（`deploy/mediapush.service` + `deploy/install.sh`）

---

## 12. 决策记录

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 数据库迁移 | `create_all` 建表，单机简单 |
| 2 | TG 推送目标 | 单一频道 |
| 3 | Web 后台认证 | 单管理员密码登录（session/cookie） |
| 4 | 任务互斥 | 手动触发与定时任务共享 `is_running` 互斥 |
| 5 | 日志查看 | 文件读取 + 内存环形缓冲双源，支持实时尾随 |
| 6 | 配置热生效 | cookie / TMDB key / 调度间隔均支持热改；cookie 热改触发 `reset_pan115` |
| 7 | 测试覆盖 | parser / tmdb / repository 必须单测；其他模块尽力补 |
| 8 | 部署方式 | Docker Compose 单容器（默认）+ systemd 直装（`deploy/`）双支持；3 项启动配置写 compose environment / systemd Environment，无 .env |
| 9 | 代理 | TG/TMDB 走代理（必须），115 默认不走；代理配置走 Web 热生效；Docker 用 host.docker.internal，直装用 127.0.0.1 |
| 10 | 错误处理 | `core/retry.py` 指数退避；永久错误（115 405 / TMDB 4xx）不重试；rate-limit 关键词监控告警 |

> 全部阶段已实现完成（阶段 0–6，178 项测试通过）。
