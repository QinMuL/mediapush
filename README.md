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

### 7. 频道监控账号（可选）
- 功能：用你自己的 Telegram 账号实时监控指定公开频道，自动捕获其中的 ed2k 链接并推送到推送频道。
- 到 <https://my.telegram.org> → API development tools 申请 `api_id` / `api_hash`，填 `TG_API_ID` / `TG_API_HASH`。
- 启动后直接向 Bot 发送 `/mon login`，在对话中依次发送手机号（国内 11 位自动补 +86）→ 验证码 → 两步验证密码（如已设置）即完成登录，无需 SSH 进容器；登录后监控自动启动。
- 备选：项目目录运行 `python -m app.monitor.login` CLI 登录（交互方式相同）。
- 登录态保存为 `data/monitor.session`（随数据卷持久化），切勿泄露（等同账号登录态）。
- 建议使用小号：监控账号需加入被监控频道，频繁操作存在限流风险。

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
| `TG_API_ID` / `TG_API_HASH` | 频道监控用户账号凭证（my.telegram.org 申请，可选） |
| `MONITOR_ENABLED` | 是否启用频道监控，默认 `true` |
| `MONITOR_SESSION` | Telethon session 路径，默认 `./data/monitor.session` |
| `MONITOR_DB_PATH` | 监控配置存储，默认 `./data/monitor.db` |
| `MONITOR_BATCH_SECONDS` | 默认聚合窗口秒数（0=实时逐条），可被 `/mon batch` 覆盖 |

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
| `/mon` | 频道监控管理（详见下节） |
| `/help` | 帮助 |

处理流程：Bot 先回复"⏳ 正在读取…"，读取完成后回复 `✅ 已推送 · 文件 N · 🎬 标题 (年份)`，同时频道收到带海报的卡片。

**卡片包含**：标题/年份、TMDB 评分、类型、国家、导演/主创、主演、画质/HDR/来源、季集（或电影时长）、首播/上映、概览、115 分享链接（含访问码）、TMDB 详情链接。

**去重**：同一分享码重复发送会提示"已推送过，跳过"。TMDB 元数据走缓存（连载中剧集 3 天、已完结 30 天）。

### 频道监控（/mon）

用你自己的 Telegram 账号（Telethon）实时监控公开频道，捕获新消息中的 ed2k 链接（`ed2k://|file|...|/` 标准格式），经格式验证与关键词过滤后推送到目标频道。推送格式包含来源频道、北京时间戳与 `<code>` 明文链接块。

| 命令 | 说明 |
|---|---|
| `/mon` | 查看监控状态（服务/频道/目标/窗口/规则） |
| `/mon login [手机号]` | 交互式登录监控账号：对话中发送手机号 → 验证码 → 两步密码（5 分钟有效，`/cancel` 可中止，敏感消息自动删除） |
| `/mon add @频道` | 添加监控频道（t.me 链接 / chat_id 亦可，账号自动加入频道） |
| `/mon del @频道` | 移除监控频道 |
| `/mon target <频道ID>` | 设置推送目标（默认 `TG_CHAT_ID_ED2K`，回退 `TG_CHAT_ID`；Bot 需为该频道管理员） |
| `/mon batch <秒>` | 聚合窗口：同频道 N 秒内的链接合并为一条推送（0=实时逐条） |
| `/mon filter` | 查看关键词过滤规则 |
| `/mon filter +关键词` | 仅推送文件名命中关键词的链接（include 白名单） |
| `/mon filter -关键词` | 丢弃文件名命中关键词的链接（exclude 黑名单） |
| `/mon filter del 关键词` | 删除规则 |

**可靠性机制**：
- 去重：md5(链接) 持久化 30 天，重复链接不再推送；推送失败不标记，链接再次出现自动重试。
- 补扫：重启后按频道消息水位（last_msg_id）回溯最多 100 条，停机期间漏掉的消息不丢失。
- 限流：推送串行 + 2s 间隔 + flood control 自动等待 + 3 次退避重试。
- 断连：Telethon 自动重连；事件处理异常不影响 Bot 主链路。
- 频道监控与 Bot 主推送相互独立，监控故障不会影响手动推送功能。

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
│   ├── handlers.py      # 命令 + 裸链接处理 + /mon 频道监控管理
│   └── pusher.py        # 卡片渲染 + 推送
├── monitor/             # 频道监控（Telethon 用户账号）
│   ├── store.py         # 监控配置持久化（频道/规则/去重，monitor.db）
│   ├── watcher.py       # ed2k 提取/验证/过滤/渲染（纯函数）
│   ├── service.py       # Telethon 封装 + 实时事件 + 补扫 + 推送
│   └── login.py         # 登录 CLI（备选；推荐 Bot 内 /mon login）
└── db/
    └── cache.py         # aiosqlite：tmdb_cache + pushed_shares
tests/                   # 单测（parser/link_parser/tmdb/pusher/cache/processor/monitor）
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

