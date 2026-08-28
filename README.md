# 网盘分享链接 → TMDB → Telegram 频道推送工具

把 **115 网盘分享链接** 或 **ed2k 链接** 发给一个 Telegram Bot，Bot 会：

1. 通过 `p115client` 读取分享内的文件列表（ed2k 则纯本地解析链接）
2. 用 `guessit` 解析文件名，聚合出标题/年份/季集/画质
3. 调 **TMDB 官方 API** 匹配元数据（海报、评分、概览、演职员、集数）
4. 渲染成卡片，**推送到指定 Telegram 频道**，并做去重与缓存

> 已接入 **115 网盘 + ed2k** 两种来源（`BaseShareProvider` 接口抽象，可扩展夸克/阿里等）。115 与 ed2k 卡片可分流推送到不同频道（`TG_CHAT_ID_115` / `TG_CHAT_ID_ED2K`）。

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
- **推荐文件化**：改填 `PAN115_COOKIE_FILE` 指向 cookie 文件路径（一整行 cookie 字符串）。容器挂载该文件后**更新内容无需重启**——巡检器每轮热加载；cookie 失效时还会私信 admin 告警（24h 节流）。`PAN115_COOKIE` 环境变量优先于文件。

### 6. 代理（国内必需）
- TG Bot API 与 TMDB API 在国内需走代理；填 `PROXY_URL`（如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080`）。
- **115 默认不走代理**（走代理易触发风控），如确实需要可设 `PAN115_USE_PROXY=true`（当前版本 115 代理尚未完全接线，默认直连）。
- 进程启动时会**自动清除** docker-compose 注入的 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 等环境变量（防止 115 意外走代理触发风控）；代理只认 `PROXY_URL` 显式配置。

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
| `TG_CHAT_ID` | 推送频道（@username 或 -100xxx），未分流时的回退目标 |
| `TG_CHAT_ID_115` | 115 网盘链接推送频道（可选，回退 `TG_CHAT_ID`） |
| `TG_CHAT_ID_ED2K` | ed2k 链接推送频道（可选，回退 `TG_CHAT_ID`） |
| `TG_ADMIN_IDS` | 管理员用户 ID（逗号分隔） |
| `TMDB_API_KEY` | TMDB v3 API Key |
| `TMDB_LANGUAGE` | TMDB 语言，默认 `zh-CN` |
| `PAN115_COOKIE` | 115 cookie（**可选**，留空走匿名读取） |
| `PAN115_COOKIE_FILE` | 115 cookie 文件路径（可选，热加载 + 失效告警；`PAN115_COOKIE` 优先） |
| `PAN115_USE_PROXY` | 115 是否走代理，默认 `false` |
| `PROXY_URL` | TG/TMDB 代理地址 |
| `INSPECT_ENABLED` | 是否启用分享失效巡检，默认 `true` |
| `INSPECT_INTERVAL_HOURS` | 巡检间隔（小时），默认 `6` |
| `INSPECT_NOTIFY` | 失效撤卡后是否私信 admin 告警，默认 `true` |
| `SHARE_WATCH_ENABLED` | 是否启用目录监控自动建分享，默认 `true` |
| `SHARE_WATCH_INTERVAL_MINUTES` | 目录扫描间隔（分钟），默认 `10` |
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
| 发送 ed2k 链接 `ed2k://\|file\|...\|/` | 当作 ed2k 资源处理（文件名含 SxxExx 判定为剧集） |
| 消息正文含 `访问码：xxxx` / `提取码:xxxx` / `密码：xxxx` | 链接未带密码时自动提取访问码（URL 自带则不覆盖） |
| 一条消息含多个链接 | 自动批处理：按集数排序逐个推送，实时进度 + 最终汇总 |
| `/115 <链接> [访问码]` | 显式触发 |
| `/edit <链接>` | 预览编辑模式：追加推荐语 / 切换 💎 精品标记后推送（跳过去重，可重推） |
| `/cancel` | 取消当前编辑会话 |
| `/dir add <网盘路径>` | 登记目录监控（如 `/dir add /媒体/新剧`），新子目录自动建永久分享推送 |
| `/dir list` / `/dir del <路径>` | 查看 / 移除监控目录 |
| `/share` | 立即扫描一轮监控目录（不等定时） |
| `/inspect [数量]` | 手动巡检一轮已推送分享（失效撤卡，默认 50 条，详见下节） |
| `/refresh <tmdb_id>` | 清除该 TMDB 缓存，下次重新拉取（剧集更新集数时用） |
| `/status` | 查看配置与 115 健康状态 |
| `/mon` | 频道监控管理（详见下节） |
| `/help` | 帮助 |

处理流程：Bot 先回复"⏳ 正在读取…"，读取完成后回复 `✅ 已推送 · 文件 N · 🎬 标题 (年份)`，同时频道收到带海报的卡片。

**卡片包含**：标题/年份、🆔 TMDB ID、TMDB 评分、类型、地区（国旗 emoji）、导演/主创、主演、体积、画质 8 维分析（💎 精品标记 / 📝 推荐语可经 `/edit` 追加）、季集（或电影时长）、首播/上映、概览、文件清单（可展开）、115 分享链接或 ed2k 资源（明文 `code` 模块）、📚 TMDB 详情按钮（卡片下方 inline button）。

**去重与缓存**：
- 同一分享码重复发送提示"已推送过，跳过"；同一链接 60 秒内重复发送提示"正在处理中"（防并发双推）。
- TMDB 元数据缓存统一 24 小时（过期自动清除重拉）。
- `/edit` 重推会跳过持久化去重（用于补档访问码、更新卡片）。

### 目录监控：自动建永久分享（/dir + /share）

监控**自己网盘**的指定目录（需 115 cookie，创建分享接口不支持匿名）：

- **登记**：`/dir add /媒体/新剧`（路径即时校验，防拼写错误）；`/dir list` 查看、`/dir del` 移除。
- **扫描**：默认每 10 分钟（`SHARE_WATCH_INTERVAL_MINUTES`）后台扫一轮；`/share` 随时手动触发。启动 1 分钟后先跑一轮。
- **粒度**：监控目录下每个**新子目录** = 一个永久分享 = 一张卡片（一部剧/一部电影），契合卡片模板的文件清单结构。
- **建分享**：`share_send` 创建 + `share_duration=-1` 设**永久**（P115-Share 同款配方）；margin 限速自动等待重试。
- **推送**：完全复用手动推送卡片管线（TMDB 匹配/海报/画质/分流频道），推送串行 + 2s 限速防 flood。
- **去重**：子目录 fid 持久化（shared_items），已分享的不再重复；建分享/推送失败不标记，下轮自动重试（宁重不漏）。
- **闭环**：推送后访问码/消息引用自动存档 → 失效巡检器照常撤卡死链。

### 分享失效巡检（/inspect）

已推送的 115 分享会因分享者取消/违规/审核而失效，频道里留死链影响体验。巡检器（`app/telegram/inspector.py`）定期体检已推送卡片：

- **周期**：默认每 6 小时一轮（`INSPECT_INTERVAL_HOURS`），每轮查最久未检查的 50 条；启动 2 分钟后先跑一轮。
- **判定**：调 115 `share_snap` 接口检查状态——`share_state=7` 或 errno 4100009/4100010 判定失效。
- **撤卡**：失效卡片自动**删除频道消息**（撤卡），标记 `dead` 不再重复巡检；admin 收到汇总私信（`INSPECT_NOTIFY`）。
- **语义区分**（实测 errno）：
  - `4100012` 请输入访问码 / `4100008` 访问码错误 → **分享还活着**，计为存活，明细提示 `/edit` 重推可补档，不撤卡。
  - 快照生成中 / 审核中 → 待定，下轮再看；网络异常 → 下轮自动重试。
- **手动**：`/inspect 20` 立即巡检 20 条并回汇总（存活/失效撤卡/待定/缺访问码明细）。
- ed2k 不巡检（磁力链无失效概念）；cookie 文件热加载与失效告警也挂在该循环。

### 频道监控（/mon）

用你自己的 Telegram 账号（Telethon）实时监控公开频道，捕获新消息中的 ed2k 链接（`ed2k://|file|...|/` 标准格式），经格式验证与关键词过滤后推送到目标频道。推送走与手动推送完全相同的卡片管线：TMDB 自动匹配 → 海报/背景图卡片 + 画质分析 + 📚 TMDB 详情按钮（与手动推送模板一致）；TMDB 未匹配到时回退纯文本中继（来源频道 + 时间戳 + `<code>` 明文链接块），链接不丢失。

| 命令 | 说明 |
|---|---|
| `/mon` | 查看监控状态（服务/频道/目标/窗口/规则） |
| `/mon login [手机号]` | 交互式登录监控账号：对话中发送手机号 → 验证码 → 两步密码（5 分钟有效，`/cancel` 可中止，敏感消息自动删除） |
| `/mon add @频道` | 添加监控频道（t.me 链接 / chat_id 亦可，账号自动加入频道） |
| `/mon del @频道` | 移除监控频道 |
| `/mon target <频道ID>` | 设置推送目标（默认 `TG_CHAT_ID_ED2K`，回退 `TG_CHAT_ID`；Bot 需为该频道管理员） |
| `/mon batch <秒>` | 聚合窗口：同频道 N 秒内的链接缓冲后逐条推卡片（0=实时；窗口用于合并突发与去重） |
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
├── main.py              # 入口：加载配置 → 清代理环境变量 → 构建容器 → run_polling
├── config.py            # .env / 环境变量加载与校验（含 cookie 文件读取）
├── core/
│   ├── container.py     # DI 容器（懒加载各服务）
│   ├── processor.py     # 编排：读取→解析→TMDB→推送→去重
│   └── share_watcher.py # 目录监控→建永久分享→推卡片（/dir 管理）
│   └── link_parser.py   # 115 链接/裸码/ed2k 解析 + 正文访问码提取
├── providers/
│   ├── base.py          # BaseShareProvider + ShareFile
│   ├── ed2k.py          # ed2k Provider（纯字符串解析，无网络请求）
│   ├── exceptions.py    # Pan115Error（独立，p115client 装坏也可导入）
│   └── pan115.py        # 115 封装（share_snap 预检 + margin/快照渐进重试）
├── parser/
│   └── media_parser.py  # guessit + 噪音清洗 + 分享聚合
├── tmdb/
│   └── client.py        # TMDB API（搜索带年回退/年份-标题-类型优先匹配/详情/缓存）
├── telegram/
│   ├── bot.py           # PTB Application（concurrent_updates + 代理 + 心跳 + 巡检挂载）
│   ├── handlers.py      # 命令 + 裸链接处理 + 批处理聚合 + 处理中去重 + /mon 管理
│   ├── edit_session.py  # /edit 编辑会话状态（推荐语/精品标记）
│   ├── inspector.py     # 分享失效巡检（撤卡/告警 + cookie 热加载）
│   └── pusher.py        # 卡片渲染 + 推送（返回消息引用供撤卡）
├── monitor/             # 频道监控（Telethon 用户账号）
│   ├── store.py         # 监控配置持久化（频道/规则/去重，monitor.db）
│   ├── watcher.py       # ed2k 提取/验证/过滤/渲染（纯函数）
│   ├── service.py       # Telethon 封装 + 实时事件 + 补扫 + 推送
│   └── login.py         # 登录 CLI（备选；推荐 Bot 内 /mon login）
└── db/
    └── cache.py         # aiosqlite：tmdb_cache + pushed_shares（巡检字段 + 自动迁移）
tests/                   # 单测（parser/link_parser/pan115/tmdb/pusher/cache/processor/monitor/inspector/handlers）
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
- **115 响应语义**（实测）：margin 风控返回 `{"margin": N}`（无 state/data，check_response 放行后 `resp["data"]` 抛 KeyError）→ 等 N 秒渐进重试（cap 30s，3 次）；「正在生成文件快照」→ 3s/6s/9s 退避重查；errno 4100009/4100010/`share_state=7` = 失效；errno 4100012/4100008 = 分享存在仅访问码缺失/错误，**不是死链**。
- **Telegram**：`concurrent_updates(True)`（否则长 handler 阻塞队列）；handler 经 `bot_data` 注入 container，不访问私有属性；TG 走代理。
- **TMDB**：4xx 不重试 / 429·5xx·超时指数退避重试；带年搜无果回退无年；`search_best` 打分：年份吻合 > 标题精确（zh > original_title 别名 > 包含）> 评分，显式 media_type 过滤异型候选；元数据缓存统一 24h（upsert 刷新时间戳）。
- **代理分发**：TG + TMDB 走代理，115 默认不走；启动时清除进程级代理环境变量（`main.py`）。
- **推送链路**：`push_share` 返回 `(ok, msg, message_id, chat_id)` 消息引用，`mark_pushed` upsert 存档（provider/password/chat_id/message_id/title）；巡检撤卡靠该引用。批处理串行（全局锁 + 2s 限速）；同一链接处理中 60s 去重防双推。
- **健康检查**：Bot `post_init` 启动心跳任务（每 30s 写 `/tmp/.heartbeat`），Docker HEALTHCHECK 检查文件新鲜度（120s 阈值），卡死时自动重启。详见部署章节。
- 扩展新网盘：继承 `BaseShareProvider` 实现 `list_share`/`check_health`，在 `link_parser` 注册解析，在 `container` 注册实例，上层零改动。

---

## 七、常见问题

- **Bot 不响应**：检查 `TG_ADMIN_IDS` 是否填了你的用户 ID；检查 `PROXY_URL` 是否可达；看 `docker compose logs`。
- **115 读取失败**：访问码错误（确认链接带 `?password=`，或正文有"访问码：xxxx"提示行）；分享已失效；或匿名接口被限流（margin，稍后自动重试）。注意读取分享**不需要 cookie**，cookie 仅 `/status` 健康检查用。
- **巡检全是"缺访问码"**：这些是升级前推送的旧卡片（当时未存档访问码），分享本身有效。想完整校验就 `/edit <链接>` 重推一次，之后新卡片都会存档访问码。
- **巡检报"处理中/待定"**：分享正在生成快照或审核中，下轮巡检会复查；网络异常同理，不判死。
- **TMDB 匹配不到**：文件名太乱时，可手动 `/115` 带更规范的链接；或检查 `TMDB_LANGUAGE`。
- **频道收不到**：确认 Bot 已是频道管理员且有发送权限；确认 `TG_CHAT_ID` 正确（分流时检查 `TG_CHAT_ID_115` / `TG_CHAT_ID_ED2K`）。
- **cookie 失效告警**：仅影响 `/status` 健康检查，匿名读取分享不受影响。更新 cookie：改 `PAN115_COOKIE_FILE` 文件内容（自动热加载）或 `PAN115_COOKIE` 环境变量后重启。
- **目录监控没反应**：需已配置 115 cookie（创建分享要登录态）；`/dir list` 确认目录已登记；`/share` 手动触发看报错；cookie 失效时 `/dir add` 会被拦下。

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

---

## 九、致谢

部分工程经验借鉴自开源项目 [P115-Share](https://github.com/ListeningLTG/P115-Share)（分享链接处理工具）：margin 限速识别与渐进重试、快照生成中退避、访问码正文提取、处理中去重、cookie 文件热加载与失效告警、分享失效定期巡检 + 撤卡、代理环境变量清理、TMDB 年份/别名优先匹配。

