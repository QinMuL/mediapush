# 网盘分享链接 → TMDB → Telegram 频道推送工具

把 **115 网盘分享链接** 发给一个 Telegram Bot，Bot 会：

1. 通过 `p115client` 读取分享内的文件列表
2. 用 `guessit` 解析文件名，聚合出标题/年份/季集/画质
3. 调 **TMDB 官方 API** 匹配元数据（海报、评分、概览、演职员、集数）
4. 渲染成卡片，**推送到指定 Telegram 频道**，并做去重与缓存

> 当前仅接入 **115 网盘**，但抽象了 `BaseShareProvider` 接口，后续可扩展夸克/阿里等。

---

## 一、前置准备

需要准备 5 样东西，全部填入 `.env`。

### 1. Telegram Bot Token
- 找 [@BotFather](https://t.me/BotFather) → `/newbot` → 拿到 token，填 `TG_BOT_TOKEN`。

### 2. 推送目标频道
- 新建或复用一个频道，**把 Bot 加为频道管理员**（需"发送消息"权限）。
- 频道标识填 `TG_CHAT_ID`：公开频道用 `@username`，私有频道用 `-100xxxxxxxxxx`（数字 ID，可用 [@userinfobot](https://t.me/userinfobot) 或把频道转发给该 bot 获取）。

### 3. 管理员用户 ID
- 谁可以用这个 Bot 推送？把你的 Telegram 数字用户 ID 填 `TG_ADMIN_IDS`（多个用逗号）。从 [@userinfobot](https://t.me/userinfobot) 获取。

### 4. TMDB API Key
- 到 <https://www.themoviedb.org/settings/api> 申请（选 Developer，免费），拿到 **API Key (v3 auth)**，填 `TMDB_API_KEY`。

### 5. 115 Cookie（可选）
- **读取分享内容走 115 的匿名 web 接口，无需 cookie**——访问码就是门禁。因此这一项可留空。
- 仅当想用 `/status` 验证自有账号有效性时才填：浏览器登录 [115 网盘网页版](https://115.com)，F12 → Network → 任一请求 → 复制 `Cookie` 头，形如 `UID=xxx;CID=xxx;SEID=xxx;KID=xxx;`，填 `PAN115_COOKIE`。

### 6. 代理（国内必需）
- TG Bot API 与 TMDB API 在国内需走代理；填 `PROXY_URL`（如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`）。
- **115 默认不走代理**（走代理易触发风控），如确实需要可设 `PAN115_USE_PROXY=true`（当前版本 115 代理尚未完全接线，默认直连）。

---

## 二、配置

复制示例配置并填写：

```bash
cp .env.example .env
# 编辑 .env 填入上述 5 项
```

`.env` 关键项：

| 变量 | 说明 |
|---|---|
| `TG_BOT_TOKEN` | Bot token |
| `TG_CHAT_ID` | 推送频道（@username 或 -100xxx） |
| `TG_ADMIN_IDS` | 管理员用户 ID（逗号分隔） |
| `TMDB_API_KEY` | TMDB v3 API Key |
| `TMDB_LANGUAGE` | TMDB 语言，默认 `zh-CN` |
| `PAN115_COOKIE` | 115 cookie（**可选**，留空走匿名读取） |
| `PAN115_USE_PROXY` | 115 是否走代理，默认 `false` |
| `PROXY_URL` | TG/TMDB 代理地址 |
| `LOG_LEVEL` | 控制台日志级别，默认 `INFO`（文件日志恒为 `DEBUG` 全量） |
| `DB_PATH` | SQLite 缓存路径，默认 `./data/cache.db` |
| `LOG_FILE` | 文件日志路径，默认 `./data/logs/mediapush.log`（按天轮转，保留 14 天） |

---

## 三、部署（Docker Compose）

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f          # 实时控制台日志（INFO 故事线）
docker compose logs --since 1h  # 最近 1 小时
```

- 容器通过 `extra_hosts: host.docker.internal:host-gateway` 映射宿主机，`PROXY_URL` 可填 `http://host.docker.internal:7890` 访问宿主机代理。
- `./data` 持久化 SQLite 缓存与去重记录。
- 停止：`docker compose down`。

### 日志体系（双通道分级）

| 通道 | 级别 | 内容 | 用途 |
|------|------|------|------|
| 控制台（`docker compose logs`） | INFO（`LOG_LEVEL` 可调） | 故事线：收到链接 → 读取 → TMDB 命中 → 推送结果 | 日常观察 |
| 文件（`./data/logs/`） | DEBUG（全量） | 上述 + DEBUG 细节 + 第三方库 WARNING | 排障定位 |

文件日志**按天轮转**：每天午夜切分，保留 14 天，过期自动删除。

```
data/logs/
├── mediapush.log              # 当天日志
├── mediapush.log.2026-08-21   # 昨天（自动生成）
├── mediapush.log.2026-08-20
└── ...（最多保留 14 天）
```

排障示例：Bot 不响应时先看当天文件里的探活/polling 日志：

```bash
tail -100 data/logs/mediapush.log
grep -E "探活|polling|推送" data/logs/mediapush.log
```

### 健康检查与自愈（心跳机制）

容器配有 Docker HEALTHCHECK + 应用级自愈探活，覆盖三类故障场景，**无需人工重启**：

| 故障场景 | 检测方式 | 恢复方式 | 耗时 |
|---------|---------|---------|------|
| 进程崩溃 | Docker 检测 PID 退出 | `restart: always` 自动重启 | 即时 |
| Bot 卡死（事件循环冻结） | 心跳文件停止更新 → HEALTHCHECK 标记 `unhealthy` | 监控告警（需外部 autoheal 容器才自动重启） | ~2min |
| PTB polling 静默死亡（任务退出但进程活着） | 心跳任务每 90s 检查内部 polling task 状态，`done` 即强制退出 | 进程退出 → Docker 自动重启 | ~90s |
| 代理长期断联（网络探活失败） | 心跳任务每 90s 调 TG API 探活，连续 3 次失败 → `os._exit(1)` | 进程退出 → Docker 自动重启 | ~4.5min |

**工作原理**：

| 组件 | 文件 | 说明 |
|------|------|------|
| 心跳写入 | `app/telegram/bot.py` | Bot `post_init` 启动异步任务，每 30s `touch /tmp/.heartbeat` |
| 自愈探活 | `app/telegram/bot.py` | 每 90s 双重探活：polling task 活性 + `bot.get_me()` 网络连通 |
| Docker 检查 | `docker-compose.yml` | 每 30s 检查心跳文件 mtime 是否在 120s 内，标记 `unhealthy` |

**关键参数**（`bot.py` 顶部常量 + `docker-compose.yml` healthcheck 段）：

```
bot.py:
  _HEARTBEAT_FILE     = /tmp/.heartbeat   # 心跳文件路径（容器内 /tmp，重启自动清除）
  _HEARTBEAT_INTERVAL = 30s               # 心跳写入间隔
  _PROBE_EVERY        = 3                 # 每 3 个心跳周期（90s）探活一次
  _PROBE_MAX_FAIL     = 3                 # 连续 3 次失败 → os._exit(1)

docker-compose.yml:
  interval     = 30s    # 检查间隔
  timeout      = 10s    # 单次检查超时
  start_period = 15s    # 启动宽限期（此期间失败不计入 retries）
  retries      = 3      # 连续失败次数 → unhealthy
  阈值         = 120s   # 心跳文件 mtime 超过 120s 判定失败
```

**查看健康状态**：

```bash
docker inspect mediapush --format '{{.State.Health.Status}}'
# healthy / unhealthy / starting
```

**调优**：如需更快检测，减小 `_PROBE_MAX_FAIL` 或 `_PROBE_EVERY`；如需减少误报，增大。修改后需重新构建镜像生效。

---

## 四、使用

在 Telegram 里找到你的 Bot，**用管理员账号**操作：

| 操作 | 说明 |
|---|---|
| 直接发送 `https://115.com/s/xxxx?password=yyyy` | 自动识别并处理（最常用） |
| 发送 8+ 字符裸码 | 当作分享码处理 |
| `/115 <链接> [访问码]` | 显式触发 |
| `/refresh <tmdb_id>` | 清除该 TMDB 缓存，下次重新拉取（剧集更新集数时用） |
| `/status` | 查看配置与 115 健康状态 |
| `/help` | 帮助 |

处理流程：Bot 先回复"⏳ 正在读取…"，读取完成后回复 `✅ 已推送 · 文件 N · 🎬 标题 (年份)`，同时频道收到带海报的卡片。

**卡片包含**：标题/年份、TMDB 评分、类型、国家、导演/主创、主演、画质/HDR/来源、季集（或电影时长）、首播/上映、概览、115 分享链接（含访问码）、TMDB 详情链接。

**去重**：同一分享码重复发送会提示"已推送过，跳过"。TMDB 元数据走缓存（连载中剧集 3 天、已完结 30 天）。

---

## 五、目录结构

```
app/
├── main.py              # 入口：加载配置 → 构建容器 → run_polling
├── config.py            # .env / 环境变量加载与校验
├── core/
│   ├── container.py     # DI 容器（懒加载各服务）
│   ├── processor.py     # 编排：读取→解析→TMDB→推送→去重
│   └── link_parser.py   # 115 链接/裸码解析
├── providers/
│   ├── base.py          # BaseShareProvider + ShareFile
│   ├── exceptions.py    # Pan115Error（独立，p115client 装坏也可导入）
│   └── pan115.py        # 115 封装（p115client）
├── parser/
│   └── media_parser.py  # guessit + 噪音清洗 + 分享聚合
├── tmdb/
│   └── client.py        # TMDB API（搜索带年回退/详情/海报/集数 + 缓存）
├── telegram/
│   ├── bot.py           # PTB Application（concurrent_updates + 代理 + 心跳）
│   ├── handlers.py      # 命令 + 裸链接处理
│   └── pusher.py        # 卡片渲染 + 推送
└── db/
    └── cache.py         # aiosqlite：tmdb_cache + pushed_shares
tests/                   # 单测（parser/link_parser/tmdb/pusher/cache/processor）
```

---

## 六、开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff

# 测试
PYTHONPATH=. pytest -q

# 静态检查
ruff check .
```

### 关键设计约束（来自前序项目经验）

- **p115client**：`P115Client(cookies=...)`（复数）；用 `tool.share_iterdir_walk(client, code, receive_code, app='web', async_=True)`，第三位置必须传访问码 `receive_code`；p115client 在方法内懒导入，装坏不拖垮 bot；`Pan115Error` 独立可导入。
- **Telegram**：`concurrent_updates(True)`（否则长 handler 阻塞队列）；handler 经 `bot_data` 注入 container，不访问私有属性；TG 走代理。
- **TMDB**：4xx 不重试 / 429·5xx·超时指数退避重试；带年搜无果回退无年；连载剧缓存 3 天、已完结 30 天，upsert 刷新时间戳。
- **代理分发**：TG + TMDB 走代理，115 默认不走。
- **健康检查**：Bot `post_init` 启动心跳任务（每 30s 写 `/tmp/.heartbeat`），Docker HEALTHCHECK 检查文件新鲜度（120s 阈值），卡死时自动重启。详见部署章节。
- 扩展新网盘：继承 `BaseShareProvider` 实现 `list_share`/`check_health`，在 `link_parser` 注册解析，在 `container` 注册实例，上层零改动。

---

## 七、常见问题

- **Bot 不响应**：检查 `TG_ADMIN_IDS` 是否填了你的用户 ID；检查 `PROXY_URL` 是否可达；看 `docker compose logs`。
- **115 读取失败**：访问码错误（确认链接带 `?password=`）；分享已失效；或匿名接口被限流（稍后重试）。注意读取分享**不需要 cookie**，cookie 仅 `/status` 健康检查用。
- **TMDB 匹配不到**：文件名太乱时，可手动 `/115` 带更规范的链接；或检查 `TMDB_LANGUAGE`。
- **频道收不到**：确认 Bot 已是频道管理员且有发送权限；确认 `TG_CHAT_ID` 正确。

---

## 八、CI/CD（自动版本迭代 + 镜像发布）

推送到 `main` 即自动发布，无需本地构建镜像：

- **自动打 tag**：读取最新 `vX.Y.Z` git tag，patch+1（首次 `v0.1.0`）
- **自动镜像构建**：构建并推送 GHCR，标签 `vX.Y.Z` / `X.Y.Z` / `latest`
- **镜像地址**：`ghcr.io/qinmul/mediapush:latest`
- **升 minor/major**：Actions → Release → Run workflow，选 `minor`/`major`
- **版本溯源**：git tag 为唯一来源，无 VERSION 文件，GITHUB_TOKEN 推 tag 不触发本工作流（无循环）

任意机器直接拉取使用：

```bash
docker pull ghcr.io/qinmul/mediapush:latest
```

