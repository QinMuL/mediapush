# MediaPush — 网盘/本地媒体 → TMDB → Telegram 频道自动化推送

把 **115 网盘分享链接**、**ed2k 链接** 或 **本地媒体文件** 交给一个 Telegram Bot，它自动完成：元数据匹配、卡片渲染、频道推送、失效巡检、跨云归档的全流程自动化。

> 已接入 **115 网盘 + ed2k** 两种分享来源（`BaseShareProvider` 接口抽象，可扩展夸克/阿里等）。115 与 ed2k 卡片可分流推送到不同频道（`TG_CHAT_ID_115` / `TG_CHAT_ID_ED2K`）。

---

## 目录

- [核心功能](#核心功能)
- [安装部署](#安装部署)
- [使用教程](#使用教程)
- [配置变量详解](#配置变量详解)
- [日志体系](#日志体系)
- [健康检查与自愈](#健康检查与自愈)
- [数据文件一览](#数据文件一览)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [开发](#开发)
- [CI/CD](#cicd)

---

## 核心功能

项目有**两条并行链路**，通过 DI 容器编排，各链路可独立开关：

### 链路 A：网盘分享自动化

```
用户/Bot → 115分享链接/ed2k → 解析 → TMDB匹配 → 卡片推送 → 去重缓存 → 巡检撤卡
网盘目录监控 → 自动建永久分享 → 推卡片 → 归档到 /已分享
频道监控 → 捕获ed2k → 推卡片
```

- **手动推送**：发一条 115 分享链接 / 裸码 / ed2k 链接，Bot 读取文件列表 → TMDB 匹配 → 推送带海报的卡片
- **批量推送**：一条消息多个链接自动批处理（按集数排序、实时进度、最终汇总）
- **编辑推送**（`/edit`）：预览卡片 → 追加推荐语 / 精品标记 → 确认推送（跳过去重，可重推补档）
- **目录监控**（`/dir`）：监控自己网盘目录，新子目录自动创建**永久分享**并推送，推送成功后归档
- **频道监控**（`/mon`）：用 Telegram 用户账号监控指定频道，自动捕获 ed2k 链接推送
- **失效巡检**（`/inspect`）：定期检查已推送分享，失效自动**撤卡**（删除频道死链卡片）并告警

### 链路 B：统一媒体流水线（方案二整合）

**一个服务（`app/pipeline/service.py`）+ 两个目录跑完全链**，替代原四段服务与 A/B/C 三目录：

```
目录A（下载落地）→ 稳定检测 → TMDB高置信匹配 + ffprobe探测 → 重命名 → 移入B
目录B（资源库）  → MD4分块哈希 → 记JSONL账本 → 转分享卡片 → 推TG频道
                → CloudDrive2 gRPC跨云复制 → 115网盘 → 删源（视频+伴行）
```

| 项目 | 说明 |
|---|---|
| 总开关 | `PIPELINE_ENABLED`（替代原 4 个 `*_ENABLED`） |
| 目录 | `PIPELINE_INPUT_DIR`（A）+ `PIPELINE_LIBRARY_DIR`（B，兼 CD2 上传源） |
| 稳定判定 | 仅 A 侧一处（原 A/B/C 三处 → 全链等待 ~4 分钟 → ~30 秒） |
| 上传 | 串行约束保留（一个 CopyFile 任务一时刻），重启自动从 CD2 恢复追踪 |
| JSONL | `data/ed2k_results.jsonl` 保留为追加式审计账本（重启按存在性重建） |

**模拟模式（DRY-RUN）**：三个阶段开关各管一段（默认 true）。模拟去重只记**内存**，改 `.env` 后 `/reload` 切实际，所有条目（含模拟过的）立即正常处理，无需重启。

**注意模拟的"深度"并不相同**——不是所有模拟都只出日志：

| 开关 | 管哪段 | 模拟时真实发生 |
|---|---|---|
| `PIPELINE_RENAME_DRY_RUN` | ① A→B 重命名 | 调 TMDB API 匹配，只出"拟移动"日志 |
| `PIPELINE_PUSH_DRY_RUN` | ② B→卡片 | **真哈希 + 真写 JSONL 账本**，不推频道 |
| `PIPELINE_UPLOAD_DRY_RUN` | ③ B→115 | **真列 115 目录查重**，不提交上传任务 |
| `SHARE_NORMALIZE_DRY_RUN` | 分享目录标准化（旁路功能） | 只出"拟重命名"日志 |

#### 从旧四段链路迁移

1. **合并目录**：原目录C 的待上传内容移入 B（流水线会自动对账哈希）；docker-compose 卷映射去掉 C
2. **改 .env**：删除 `LOCAL_MEDIA_* / ED2K_* / ED2K_PUSH_* / CD2_ENABLED` 等旧键，换 `PIPELINE_*`（见上表与 .env.example）；`CD2_UPLOAD_SRC` 改为 B 的 CD2 路径
3. **重启容器**：启动日志若检测到旧四段开关仍为 true 会提示迁移
4. **灰度**：先三段全 dry 观察日志，再逐段切实际（每段 /reload 即时生效）
5. 进行中的退避/上传状态属瞬态数据，迁移后从头开始（已推送去重在 cache.db 不受影响）

> **/reload 热加载前提**：docker-compose 需挂载项目目录（`- .:/app/deploy:ro`，
> 新版 compose 已内置）。`env_file:` 注入的是容器创建时的快照——不挂载时
> 改宿主机 `.env` 后 `/reload` 无法感知，只能 `docker compose up -d` 重建生效
> （旧部署 `/reload` 会如实提示未找到 .env）。

### 命令菜单总览（17 个）

| 命令 | 功能 |
|---|---|
| `/start` | 开始使用（快速上手引导） |
| `/help` | 完整用法说明 |
| `/status` | 运行状态一览（概览/健康/频道/常驻任务/流水线 4 阶段） |
| `/115 <链接> [访问码]` | 显式推送（裸链接消息自动识别） |
| `/edit <链接>` | 预览编辑模式：追加推荐语/精品标记后推送 |
| `/cancel` | 取消当前编辑或登录会话 |
| `/refresh <tmdb_id>` | 清除该 TMDB 缓存重拉（剧集更新集数时用） |
| `/loglevel <级别>` | 运行时调整控制台日志级别（文件恒为 DEBUG） |
| `/reload` | 重读 .env 热加载配置（无需重启） |
| `/cookie [串]` | 查看/设置 115 cookie（写文件+热更新+探活） |
| `/reset` | 一键清空业务数据（缓存/去重/状态/日志，保留配置） |
| `/mon` | 频道监控管理（login/add/del/target/batch/filter） |
| `/inspect [数量]` | 手动巡检一轮已推送分享（失效撤卡） |
| `/dir add\|del\|list` | 目录监控登记（新子目录自动建永久分享） |
| `/share` | 立即扫描一轮监控目录 |
| `/ed2k_status` | ed2k 生成+推送详情（pending 队列/进度/退避） |
| `/upload_status` | CD2 上传详情（进度/退避/卡死告警） |

---

## 安装部署

### 方式一：Docker Compose（推荐）

**1. 准备文件**

```bash
git clone https://github.com/qinmul/mediapush.git   # 或直接下载 docker-compose.yml + .env.example
cd mediapush
cp .env.example .env
```

**2. 编辑 `docker-compose.yml`**（按需调整）

```yaml
services:
  mediapush:
    image: ghcr.io/qinmul/mediapush:latest   # CI 自动发布镜像
    build: .                                  # 本地构建时使用
    container_name: mediapush
    restart: always
    env_file:
      - .env
    environment:
      TZ: Asia/Shanghai
    network_mode: host          # host 网络：直连宿主机 loopback 代理
    volumes:
      - ./data:/app/data        # 数据持久化（DB/日志/cookie/session）
      - /vol1/1000/media:/media  # 本地媒体流水线目录映射（链路 B 用）
    healthcheck:
      test: ["CMD", "python3", "-c", "import os,time; exit(0 if time.time()-os.getmtime('/tmp/.heartbeat')<120 else 1)"]
      interval: 30s
      timeout: 10s
      start_period: 15s
      retries: 3
```

**3. 填写 `.env`**（见[配置变量详解](#配置变量详解)，最少填 5 项：`TG_BOT_TOKEN` / `TG_CHAT_ID` / `TG_ADMIN_IDS` / `TMDB_API_KEY` / `PROXY_URL`）

**4. 启动**

```bash
docker compose up -d --build   # 本地构建
# 或使用已发布镜像
docker compose pull && docker compose up -d
```

**5. 验证**

```bash
docker compose logs -f                    # 实时日志，看到 "启动 Telegram Bot" 即成功
docker inspect mediapush --format '{{.State.Health.Status}}'   # healthy
```

在 Telegram 里给 Bot 发 `/start`，收到欢迎语即部署完成。

### 方式二：直接拉取镜像

```bash
mkdir mediapush && cd mediapush
# 下载 .env.example 为 .env 并填写（或从仓库获取 docker-compose.yml）
docker pull ghcr.io/qinmul/mediapush:latest
docker run -d --name mediapush --restart always \
  --env-file .env -e TZ=Asia/Shanghai \
  -v $(pwd)/data:/app/data \
  ghcr.io/qinmul/mediapush:latest
```

### 方式三：本地开发运行

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # 填写配置
python -m app.main
```

### 前置准备清单

部署前需要准备：

| 项目 | 获取方式 | 填入变量 |
|---|---|---|
| Telegram Bot Token | [@BotFather](https://t.me/BotFather) → `/newbot` | `TG_BOT_TOKEN` |
| 推送频道 | 新建/复用频道，**Bot 加为管理员**（需发送消息权限） | `TG_CHAT_ID`（公开 `@username`，私有 `-100xxx`） |
| 管理员用户 ID | [@userinfobot](https://t.me/userinfobot) | `TG_ADMIN_IDS`（逗号分隔） |
| TMDB API Key | <https://www.themoviedb.org/settings/api>（免费） | `TMDB_API_KEY`（v3） |
| 代理（国内必需） | TG/TMDB API 需走代理 | `PROXY_URL` |
| 115 Cookie（可选） | 浏览器 F12 → 复制 Cookie 头 | `PAN115_COOKIE_FILE`（推荐文件方式） |
| Telethon 凭证（可选） | <https://my.telegram.org> | `TG_API_ID` / `TG_API_HASH` |

**关于 115 Cookie**：读取分享内容走 115 匿名 web 接口，**无需 cookie**。仅以下功能需要：`/status` 账号健康检查、目录监控建分享（`/dir`）、巡检撤卡。推荐用 `PAN115_COOKIE_FILE` 文件方式——更新文件内容无需重启（巡检每轮热加载），失效自动告警 admin，还支持在 Bot 里直接 `/cookie <串>` 更新。

---

## 使用教程

以下操作均在 Telegram 中用**管理员账号**进行。

### 入门：推送第一个链接

1. 给 Bot 发送 `https://115.com/s/xxxx?password=yyyy`
2. Bot 回复 "⏳ 正在读取…"
3. 读取完成回复 `✅ 已推送 · 文件 N · 🎬 标题 (年份)`，频道收到带海报的卡片

其他发送方式：

| 发送内容 | 处理方式 |
|---|---|
| 8+ 字符裸码 | 当作分享码处理 |
| `ed2k://\|file\|片名.mkv\|大小\|hash\|/` | 当作 ed2k 资源（文件名含 SxxExx 判定为剧集） |
| 正文含 `访问码：xxxx` / `提取码:xxxx` / `密码：xxxx` | 链接未带密码时自动提取访问码 |
| 一条消息多个链接 | 自动批处理：按集数排序逐个推送，实时进度+最终汇总 |

**卡片内容**：标题/年份、🆔 TMDB ID、评分、类型、地区（国旗 emoji）、导演/主演、体积、画质 8 维分析（💎 精品标记 / 📝 推荐语可经 `/edit` 追加）、季集（或电影时长）、首播/上映、概览、文件清单（可展开）、分享链接或 ed2k 资源（明文 `code` 模块）、📚 TMDB 详情按钮。

**去重**：同一分享码重复发送提示"已推送过"；同一链接 60 秒内重复发送提示"正在处理中"。TMDB 元数据缓存 24 小时。

### 编辑推送（/edit）

```
/edit https://115.com/s/xxxx?password=yyyy
```

Bot 回复卡片预览 + 按钮：`✏️ 追加画质`（输入自定义推荐语）/ `💎 精品:开/关` / `✅ 确认推送` / `❌ 取消`。确认后跳过持久化去重直接推送——适合补档访问码、更新卡片。

### 目录监控：自动建永久分享（/dir + /share）

监控**自己网盘**的指定目录（需 115 cookie）：

```
/dir add /媒体/新剧     # 登记（路径即时校验）
/dir list              # 查看已监控目录
/dir del /媒体/新剧     # 移除
/share                 # 立即扫描一轮（不等定时）
```

- 默认每 10 分钟后台扫一轮（`SHARE_WATCH_INTERVAL_MINUTES`）
- 每个新子目录 = 一个永久分享 = 一张卡片（一部剧/一部电影）
- 推送成功后自动移入 `/已分享` 归档目录（`SHARE_ARCHIVE_DIR`，可关）
- 失败自动指数退避重试；违规资源标记 blocked（365 天不再重试）
- 每轮结果明细私信 admin（静默轮不打扰）

### 分享失效巡检（/inspect）

已推送的 115 分享会因分享者取消/违规/审核而失效，频道留死链影响体验：

- 默认每 6 小时巡检一轮，每轮查最久未检查的 50 条
- 失效自动**撤卡**（删除频道消息）+ 标记 dead + 私信 admin 汇总
- 缺/错访问码判定为**存活**（提示 `/edit` 补档，不撤卡）
- 手动触发：`/inspect 20`（巡检 20 条并回汇总）
- ed2k 不巡检（磁力链无失效概念）

### 频道监控（/mon）

用自己的 Telegram 账号（Telethon）实时监控公开频道，捕获 ed2k 链接推送：

```
/mon login              # 交互式登录：发手机号 → 验证码 → 两步密码（敏感消息自动删除）
/mon add @频道           # 添加监控频道（t.me 链接 / chat_id 亦可）
/mon del @频道           # 移除
/mon target <频道ID>     # 设置推送目标（默认 TG_CHAT_ID_ED2K）
/mon batch <秒>          # 聚合窗口（0=实时逐条）
/mon filter +关键词       # 白名单：仅推送命中关键词的链接
/mon filter -关键词       # 黑名单：丢弃命中关键词的链接
/mon filter del 关键词    # 删除规则
/mon                     # 查看监控状态
```

登录态保存 `data/monitor.session`（随数据卷持久化），切勿泄露。建议使用小号。

### 媒体流水线（A→B 资源库→频道+115）

1. **准备目录**：`.env` 填容器内路径，`docker-compose.yml` 做宿主机→容器映射（B 须同时挂进 CloudDrive2）：

   ```yaml
   volumes:
     - ./data:/app/data
     - /nas/media/A:/media/A    # 目录A：下载落地（手动投喂也放这里）
     - /nas/media/B:/media/B    # 目录B：资源库（重命名+哈希+待上传，CD2 上传源）
   ```

   `.env` 对应填 `PIPELINE_INPUT_DIR=/media/A`、`PIPELINE_LIBRARY_DIR=/media/B`、`CD2_UPLOAD_SRC=<B 在 CD2 里的路径>`。

2. **先模拟观察**：三个阶段 `PIPELINE_*_DRY_RUN` 默认 true，日志出"拟命名/将推送/将上传"结果
3. **逐步放开**：确认命名质量后逐阶段改 `false`（重命名 → 推卡片 → 上传；每步 `/reload` 即时生效）
4. **监控进度**：`/status` 看三阶段队列/退避/传输中，`/ed2k_status` `/upload_status` 看两侧详情

**命名规范**（高置信度硬门槛）：片名 (年份) - 画质标签 {tmdb-ID}.ext；必须同时匹配 TMDB 片名+年份（剧集额外 SxxExx）才放行；质量标签从 ffprobe 实时探测；文件名末尾 `{tmdb-<id>}` 保证 Emby/飞牛 100% 刮削；低置信进入指数退避（1h→2h→…→24h），TMDB 补全后自动命中。

**CD2 上传**：通过 CloudDrive2 gRPC 跨云复制（服务端 CopyFile，非流式上传）；串行提交防挤占带宽；完成自动删源；115 秒传命中时秒完成。

### 运维命令

```
/status                    # 运行状态总览：概览/健康/频道/常驻任务/流水线
/reload                    # 改 .env 后热加载（间隔/开关/cookie 等，连接层提示需重启）
/loglevel DEBUG            # 临时切控制台日志级别排障
/cookie <串>               # 更新 115 cookie（写文件+探活+即时生效）
/reset                     # 查看将清空/保留的内容
/reset 确认                 # 执行清空：TMDB缓存/推送去重/分享登记/流水线状态/JSONL/日志
                           # 保留：.env、cookie 文件、频道监控、目录监控配置；服务自动重启
```

---

## 配置变量详解

### 必填（5 项）

| 变量 | 说明 | 示例 |
|---|---|---|
| `TG_BOT_TOKEN` | Bot token（@BotFather 获取） | `123456:ABC-xxx` |
| `TG_CHAT_ID` | 推送频道（@username 或 -100xxx），未分流时的回退目标 | `@mychannel` |
| `TG_ADMIN_IDS` | 管理员用户 ID（逗号分隔） | `123456789` |
| `TMDB_API_KEY` | TMDB v3 API Key | `abc123...` |
| `PROXY_URL` | TG/TMDB 代理（国内必需） | `http://127.0.0.1:7890` |

### Telegram

| 变量 | 默认 | 说明 |
|---|---|---|
| `TG_CHAT_ID_115` | 空（回退 `TG_CHAT_ID`） | 115 网盘链接推送频道 |
| `TG_CHAT_ID_ED2K` | 空（回退 `TG_CHAT_ID`） | ed2k 链接推送频道 |

### TMDB

| 变量 | 默认 | 说明 |
|---|---|---|
| `TMDB_LANGUAGE` | `zh-CN` | TMDB 元数据语言 |

### 115 网盘

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAN115_COOKIE` | 空 | cookie 直配（**可选**，读取分享走匿名）。直配优先于文件；直配后 `/cookie` 命令不可用 |
| `PAN115_COOKIE_FILE` | 空 | cookie 文件路径（一整行字符串）。推荐：热加载 + 失效告警 + `/cookie` 更新 |
| `PAN115_USE_PROXY` | `false` | 115 是否走代理（走代理易触发风控，保持默认） |
| `PAN115_REQUEST_INTERVAL` | `1` | 115 请求稳态间隔秒。全局限速令牌桶：margin 限速时自动翻倍降速，恢复后自动回落 |

### 分享失效巡检

| 变量 | 默认 | 说明 |
|---|---|---|
| `INSPECT_ENABLED` | `true` | 是否启用巡检 |
| `INSPECT_INTERVAL_HOURS` | `6` | 巡检间隔（小时） |
| `INSPECT_NOTIFY` | `true` | 失效撤卡后私信 admin 汇总 |
| `INSPECT_NOTIFY_CODE` | `true` | 发现缺/失访问码时私信提醒补档 |
| `INSPECT_ERROR_ALERT_ROUNDS` | `2` | 连续 N 轮全部失败才告警（疑似 IP 被限） |
| `COOKIE_ALERT` | `true` | 115 cookie 失效告警及恢复通知（24h 节流） |

### 目录监控（自动建永久分享，需 115 cookie）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHARE_WATCH_ENABLED` | `true` | 是否启用目录监控 |
| `SHARE_WATCH_INTERVAL_MINUTES` | `10` | 后台扫描间隔（分钟） |
| `SHARE_WATCH_NOTIFY` | `true` | 每轮结果明细私信 admin（静默轮不打扰） |
| `SHARE_ARCHIVE_DIR` | `/已分享` | 推送成功后归档目录（须在监控目录之外；留空=不移动） |

### 频道监控（Telethon 用户账号）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TG_API_ID` / `TG_API_HASH` | 空 | my.telegram.org 申请 |
| `MONITOR_ENABLED` | `true` | 是否启用频道监控 |
| `MONITOR_SESSION` | `./data/monitor.session` | 登录态文件路径 |
| `MONITOR_DB_PATH` | `./data/monitor.db` | 监控配置存储 |
| `MONITOR_BATCH_SECONDS` | `0` | 默认聚合窗口秒（0=实时；可被 `/mon batch` 覆盖） |
| `MONITOR_NOTIFY` | `true` | 断连/重连等运行事件私信 admin |

### 本地媒体流水线（链路 B）

#### 统一媒体流水线（含 CD2 上传连接）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_ENABLED` | `false` | 总开关（替代原 4 个 `*_ENABLED`） |
| `PIPELINE_INPUT_DIR` | 空 | 目录 A：下载落地（递归监控 + 稳定判定；手动投喂也放这里） |
| `PIPELINE_LIBRARY_DIR` | 空 | 目录 B：资源库（重命名+哈希+待上传；须挂在 CD2，勿手动投放） |
| `PIPELINE_RENAME_DRY_RUN` | `true` | ① 模拟：只出"拟移动"日志（仍真调 TMDB） |
| `PIPELINE_PUSH_DRY_RUN` | `true` | ② 模拟：真哈希+记 JSONL，不推频道 |
| `PIPELINE_UPLOAD_DRY_RUN` | `true` | ③ 模拟：真查重 115 目标，不提交任务 |
| `PIPELINE_INTERVAL_SECONDS` | `10` | 轮询周期秒（哈希/推送/上传同轮进行） |
| `PIPELINE_STABLE_ROUNDS` | `3` | A 侧稳定判定轮数（全链唯一一处，约 30s） |
| `PIPELINE_BATCH_MAX` | `5` | 单轮最多重命名文件数（防打爆 IO/TMDB） |
| `PIPELINE_STUCK_DAYS` | `7` | 各阶段失败卡死告警阈值天 |
| `PIPELINE_MIN_SIZE_MB` | `10` | 体积守门（MB）；设 0 关闭 |
| `PIPELINE_MIN_AGE_MINUTES` | `5` | mtime 静默年龄守门（分）：最近有写入不动，防下载器停顿骗过快照稳定判定 |
| `PIPELINE_REPORT_ADMIN` | `true` | 有动作轮次汇总+明细私信 admin（旧键 `CD2_REPORT_ADMIN` 兼容） |
| `PIPELINE_CLEAN_ENABLED` | `false` | 元数据清洗（L1 保守档：广告标签/垃圾章节/广告轨，remux 零重编码） |
| `PIPELINE_CLEAN_DRY_RUN` | `true` | 清洗模拟：只检测报告不动文件（轮汇总"🧹 检测到垃圾"组） |
| `CD2_ADDRESS` | `192.168.1.202:19798` | CloudDrive2 gRPC 地址 |
| `CD2_TOKEN` | 空 | CD2 API 令牌（推荐，Web UI → 设置 → API 令牌创建） |
| `CD2_USERNAME` / `CD2_PASSWORD` | 空 | 或账号密码（token 优先） |
| `CD2_UPLOAD_SRC` | 空 | 目录 B 在 CD2 里的路径（本地磁盘挂载后） |
| `CD2_UPLOAD_DST` | 空 | 115 在 CD2 里的目标目录 |

### 日志与存储

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 控制台日志级别（文件恒为 DEBUG 全量；`/loglevel` 运行时可调） |
| `LOG_COLOR` | `true` | 控制台彩色日志 |
| `LOG_FILE` | `./data/logs/mediapush.log` | 核心日志文件（系统内容，排除媒体流水线） |
| `LOG_MEDIA_FILE` | `./data/logs/media.log` | 媒体流水线日志文件（只记 `app.pipeline.*`，与核心日志互不重复） |
| `LOG_MAX_BYTES` | `5242880`（5MB） | 单日志文件轮转阈值（达到即刻轮转 + gzip 压缩） |
| `LOG_RETENTION_DAYS` | `7` | 轮转归档保留天数（按文件 mtime 到期即删） |
| `DB_PATH` | `./data/cache.db` | 业务数据库（TMDB 缓存 + 推送去重 + 分享登记） |
| `STATE_DB_PATH` | `./data/state.db` | 服务统一状态存储（pipeline/share_watch 等一行一服务） |

**热加载说明**：改 `.env` 后发 `/reload` 即时生效的字段包括各类间隔、通知开关、DRY_RUN 开关、日志级别、cookie 文件等（见 `config.py` 的 `HOT_RELOAD_FIELDS`）；`TG_BOT_TOKEN`、`TG_CHAT_ID`、`PROXY_URL`、`TMDB_API_KEY`、目录路径等连接层变更需重启容器。

---

## 日志体系

### 双通道分级

| 通道 | 级别 | 内容 | 用途 |
|------|------|------|------|
| 控制台（`docker compose logs`） | INFO（`LOG_LEVEL` 可调） | 故事线：收到链接 → 读取 → TMDB 命中 → 推送结果 | 日常观察 |
| 文件（`./data/logs/`） | DEBUG（全量） | 上述 + DEBUG 细节 + 第三方库 WARNING | 排障定位 |

### 双文件分流

| 文件 | 内容 |
|------|------|
| `mediapush.log` | 核心系统日志（Bot/TMDB/115/巡检/监控），**不含**媒体流水线 |
| `media.log` | 统一媒体流水线（`app.pipeline.*`：重命名/哈希/推送/上传），**只含**流水线 |

两文件互不重复，按 logger 名自动分流；控制台两份内容都输出。

### 轮转与保留

- **按固定字节轮转**：单文件达到 `LOG_MAX_BYTES`（默认 5MB）即刻轮转，旧文件 gzip 压缩成 `.N.gz`
- **7 天保留**：归档按 mtime 到期即删（`LOG_RETENTION_DAYS`，默认 7 天），启动和每次轮转都执行清理
- Docker stdout 日志另由 compose `logging` 限制（10MB × 3 份）

```
data/logs/
├── mediapush.log            # 当前核心日志
├── mediapush.log.1.gz       # 轮转归档（gzip，最多保留 7 天）
├── media.log                # 当前流水线日志
└── media.log.1.gz
```

排障示例：

```bash
tail -100 data/logs/mediapush.log                    # Bot 问题看核心日志
grep -E "探活|polling|推送" data/logs/mediapush.log
tail -100 data/logs/media.log                         # 流水线问题看媒体日志
zcat data/logs/media.log.1.gz | grep "重命名"          # 查历史归档
```

处理链路带 `trace_id`：并发批处理/巡检/监控交错时，grep `[tid=xxx]` 一步拉出全链路日志。敏感信息（cookie/访问码/密码）自动脱敏后才落盘。

---

## 健康检查与自愈

容器配有 Docker HEALTHCHECK + 应用级自愈探活，覆盖四类故障场景，**无需人工重启**：

| 故障场景 | 检测方式 | 恢复方式 | 耗时 |
|---------|---------|---------|------|
| 进程崩溃 | Docker 检测 PID 退出 | `restart: always` 自动重启 | 即时 |
| Bot 卡死（事件循环冻结） | 心跳文件停止更新 → HEALTHCHECK 标记 `unhealthy` | 监控告警（需外部 autoheal 容器才自动重启） | ~2min |
| PTB polling 静默死亡 | 心跳任务每 90s 检查内部 polling task 状态 | 进程退出 → Docker 自动重启 | ~90s |
| 代理长期断联 | 每 90s 调 TG API 探活，连续 3 次失败 → `os._exit(1)` | 进程退出 → Docker 自动重启 | ~4.5min |

| 组件 | 文件 | 说明 |
|------|------|------|
| 心跳写入 | `app/telegram/bot.py` | `post_init` 启动任务，每 30s `touch /tmp/.heartbeat` |
| 自愈探活 | `app/telegram/bot.py` | 每 90s 双重探活：polling task 活性 + `bot.get_me()` 网络连通 |
| Docker 检查 | `docker-compose.yml` | 每 30s 检查心跳文件 mtime 是否在 120s 内 |

```bash
docker inspect mediapush --format '{{.State.Health.Status}}'
# healthy / unhealthy / starting
```

---

## 数据文件一览

`./data` 卷持久化的全部文件：

| 文件 | 类型 | 说明 |
|---|---|---|
| `cache.db` | 业务数据库 | TMDB 缓存（24h TTL）+ 推送去重（pushed_shares）+ 分享登记（shared_items）+ 监控目录配置（share_dirs） |
| `state.db` | 状态数据库 | 服务统一状态：一行一服务（pipeline 为主），含 failures/completed/pushed |
| `monitor.db` | 监控数据库 | 频道监控配置（监控频道/过滤规则/去重/消息水位） |
| `monitor.session` | 登录态 | Telethon 用户账号登录态（**切勿泄露**） |
| `115cookie.txt` | 凭证 | 115 cookie 文件（`PAN115_COOKIE_FILE`，热加载） |
| `ed2k_results.jsonl` | 数据 | ed2k 哈希结果（流水线 ②→③ 的传递媒介） |
| `logs/` | 日志 | 双文件日志 + gzip 归档（保留 7 天） |

`/reset 确认` 会清空：cache.db 业务表（保留 share_dirs 配置）、state.db、monitor.db 之外的全部数据文件、全部日志。保留：.env、cookie 文件、monitor.db、share_dirs。

---

## 目录结构

```
app/
├── main.py              # 入口：加载配置 → 设置 NO_PROXY → 构建容器 → run_polling
├── config.py            # .env 加载与校验（含热加载字段清单 HOT_RELOAD_FIELDS）
├── logging_config.py    # 双通道双文件日志（彩色控制台 + 按字节轮转 + 7 天保留 + 脱敏）
├── core/
│   ├── container.py     # DI 容器（懒加载各服务，条件启用）
│   ├── processor.py     # 编排：读取→解析→TMDB→推送→去重
│   ├── share_watcher.py # 目录监控→建永久分享→推卡片（违规 blocked + 失败退避）
│   ├── link_parser.py   # 115 链接/裸码/ed2k 解析 + 正文访问码提取
│   └── rate_limiter.py  # AdaptiveLimiter 自适应令牌桶（115 margin 信号反馈）
├── providers/
│   ├── base.py          # BaseShareProvider + ShareFile（网盘抽象接口）
│   ├── ed2k.py          # ed2k Provider（纯字符串解析，无网络请求）
│   ├── exceptions.py    # Pan115Error（独立，p115client 装坏也可导入）
│   └── pan115.py        # 115 封装（share_snap 预检 + margin/快照渐进重试）
├── parser/
│   └── media_parser.py  # guessit + 噪音清洗 + 分享聚合 + {tmdb-XXX} 标签提取
├── matching.py          # 标题归一化/匹配唯一实现（全/半角标点折叠，全项目复用）
├── tmdb/
│   └── client.py        # TMDB API（搜索带年回退/跨语种别名兜底/详情 translations+AKA/缓存）
├── pipeline/            # 统一媒体流水线（方案二：A→重命名→B→哈希/推卡片→CD2上传115）
│   └── service.py       # PipelineService：单服务单轮询跑完全链（含 CD2 gRPC 层）
├── media/
│   ├── namer.py         # 命名引擎：TMDB 高置信匹配 + 统一命名模板
│   └── probe.py         # ffprobe 探测：分辨率/编码/帧率/比特率/HDR/DV
├── ed2k/
│   └── hasher.py        # MD4 分块哈希（pycryptodome C 实现，兼容 OpenSSL 3）
├── cd2/                 # CloudDrive2 gRPC 生成代码（含 protobuf 兼容性预加载）
├── telegram/
│   ├── bot.py           # PTB Application（代理 + 心跳自愈 + 服务启停编排）
│   ├── handlers/        # 命令处理器（按领域拆分：push/status/admin/edit_flow/...）
│   ├── edit_session.py  # /edit 编辑会话状态（推荐语/精品标记）
│   ├── inspector.py     # 分享失效巡检（撤卡/告警 + cookie 热加载）
│   ├── pusher.py        # 卡片渲染 + 推送（返回消息引用供撤卡）
│   └── notifier.py      # admin 私信通知 + 进度条渲染
├── monitor/             # 频道监控（Telethon 用户账号）
│   ├── store.py         # 监控配置持久化（monitor.db）
│   ├── watcher.py       # ed2k 提取/验证/过滤/渲染（纯函数）
│   ├── channel_monitor.py  # Telethon 封装 + 实时事件 + 补扫 + 推送
│   └── login.py         # 登录 CLI（备选；推荐 Bot 内 /mon login）
└── db/
    ├── cache.py         # aiosqlite 业务库（tmdb_cache/pushed_shares/shared_items/share_dirs）
    └── state.py         # StateStore 统一状态库（state.db，一行一服务 + 旧 JSON 迁移）
tests/                   # 测试套件（pipeline/service_base/handlers/watchdog/...，pytest -q 全绿）
```

---

## 常见问题

- **Bot 不响应**：检查 `TG_ADMIN_IDS` 是否填了你的用户 ID；检查 `PROXY_URL` 是否可达；看 `docker compose logs`。
- **115 读取失败**：访问码错误（确认链接带 `?password=`，或正文有"访问码：xxxx"）；分享已失效；或匿名接口被限流（margin，稍后自动重试）。读取分享**不需要 cookie**。
- **巡检全是"缺访问码"**：旧卡片（当时未存档访问码），分享本身有效。`/edit <链接>` 重推一次即存档。
- **TMDB 匹配不到**：新剧可能 TMDB 还没收录，程序会指数退避自动重试；跨语种问题检查 `PROXY_URL` 可达性。
- **频道收不到**：确认 Bot 是频道管理员且有发送权限；确认 `TG_CHAT_ID` 正确（分流时检查 `TG_CHAT_ID_115` / `TG_CHAT_ID_ED2K`）。
- **cookie 失效告警**：仅影响 `/status` 健康检查和目录监控，匿名读取分享不受影响。更新：改 cookie 文件内容（自动热加载）或 `/cookie <串>`。
- **目录监控没反应**：需已配置 115 cookie；`/dir list` 确认已登记；`/share` 手动触发看报错。
- **媒体流水线不工作**：确认 `PIPELINE_ENABLED=true` 且各阶段 `*_DRY_RUN=false`；确认 A/B 目录在 `.env` 和 `docker-compose.yml` 都配了且不嵌套；B 已挂进 CD2 且 `CD2_UPLOAD_SRC` 正确；`/status` 看三阶段状态。
- **CD2 上传卡住**：`/upload_status` 看退避详情。CD2 传输中显示 0% 属正常（单文件不报字节进度，完成才更新）；CD2 里手动取消 + 移出 B 目录即可停止。
- **违规资源一直刷屏**：`/dir del` + `/dir add` 重新添加监控目录可清除 blocked 记录。
- **想全部重来**：`/reset 确认` 一键清空业务数据（配置保留，服务自动重启）。

---

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff

PYTHONPATH=. pytest -q     # 测试
ruff check .               # 静态检查
```

### 关键设计约束

- **p115client**：懒导入（装坏不拖垮 bot）；`tool.share_iterdir_walk` 第三位置传访问码；margin 风控返回 `{"margin": N}` → 等 N 秒渐进重试；errno 4100009/4100010/`share_state=7` = 失效，4100012/4100008 = 访问码问题**不是死链**。
- **Telegram**：`concurrent_updates(True)`（防长 handler 阻塞队列）；TG 走代理；推送串行 + 2s 限速防 flood。
- **代理分发**：TG + TMDB 走代理，115 默认直连（`NO_PROXY=115.com,.115.com`，`main.py` 设置）。
- **模拟模式（DRY-RUN）状态隔离**：模拟结果只记内存（CD2 `_dry_done` / ed2k 推送 offset 不落盘），切实际模式后资源正常处理不被跳过。
- **状态存储**：`data/state.db` 单表（service/payload/updated_at），一行一服务；旧 JSON 状态文件首次加载自动迁移并改名 `.migrated`。
- **扩展新网盘**：继承 `BaseShareProvider` 实现 `list_share`/`check_health`，在 `link_parser` 注册解析，在 `container` 注册实例，上层零改动。

---

## CI/CD

推送到 `main` 即自动发布，无需本地构建镜像：

- **自动打 tag**：读取最新 `vX.Y.Z` git tag，patch+1（首次 `v0.1.0`）
- **自动镜像构建**：构建并推送 GHCR，标签 `vX.Y.Z` / `X.Y.Z` / `latest`
- **镜像地址**：`ghcr.io/qinmul/mediapush:latest`
- **升 minor/major**：Actions → Release → Run workflow，选 `minor`/`major`
- **并发控制**：同一时间只允许一个 Release 工作流运行，防止重复 tag

NAS 升级：`docker compose pull && docker compose up -d`。

---

## 致谢

部分工程经验借鉴自开源项目 [P115-Share](https://github.com/ListeningLTG/P115-Share)（分享链接处理工具）：margin 限速识别与渐进重试、快照生成中退避、访问码正文提取、处理中去重、cookie 文件热加载与失效告警、分享失效定期巡检 + 撤卡、TMDB 年份/别名优先匹配。
