# 云桌面工作台（Cloud Desk）

浏览器里的 Linux 工作台：**真终端（PTY）+ AI 对话 + 文件管理 + 端口预览 / 内置浏览器**。
后端只有一份 `server.py`（FastAPI），前端是原生 HTML/JS，所有第三方资源已本地化，**不需要构建、不需要 npm**。

> 它跑在**谁的机器上，就吃谁的配置**。项目本身不绑定任何云平台。

---

## 一、30 秒跑起来（本机）

需要：Linux / macOS（Windows 建议用 WSL2 或 Docker）、Python 3.9+。

```bash
git clone <你的仓库地址> cloud-desk && cd cloud-desk

python3 -m venv .venv && source .venv/bin/activate   # 可选但推荐
pip install -r requirements.txt

python3 server.py
```

启动后终端会打印一段：

```
==================================================
  首次启动：已生成访问密码
      xxxxxxxx-xxxx
==================================================
```

浏览器打开 <http://localhost:8080>，输入这个密码即可进入。
想自己定密码：`DESK_PASSWORD=你的密码 python3 server.py`

> 密码只保存在本机 `auth.json`（已加入 `.gitignore`），不会随仓库分发。

---

## 二、三种部署方式，怎么选

| 方式 | 适合谁 | 拿到什么 | 代价 |
|---|---|---|---|
| **本机直跑** | 自己一个人用、想快速试 | `localhost:8080`，用本机 CPU/内存 | 关机就没了；外网访问不了 |
| **Docker**（推荐） | 想干净隔离、一键复现 | 同上，环境不污染宿主 | 需要装 Docker |
| **自己的服务器 / VPS** | 想随时随地（含手机）访问 | 公网域名，24 小时在线 | 要自己配 HTTPS + 防火墙；**必须先改密码** |

### 方式 B：Docker 一键

```bash
# compose 里没有默认密码，必须显式给一个（否则会直接启动失败并提示）
DESK_PASSWORD='换成你自己的强密码' docker compose up -d --build
# 访问 http://localhost:8080
```

> 为什么要这样：早期版本在 compose 里写了 `change-me-please`，它被一起推到了公开仓库。
> 现在改成 `${DESK_PASSWORD:?...}`，没设置就报错，从机制上避免"照抄示例口令"。
> 也可以写进同目录的 `.env`（已在 `.gitignore` 里）。

要把「截图浏览器」一起装上（内置 Chromium，能内嵌打开任意网站）：把 `docker-compose.yml` 里的
`WITH_BROWSER: "0"` 改成 `"1"`，再执行 `docker compose up -d --build`（首次构建会多花几分钟）。

不用 compose 的话：

```bash
docker build --build-arg WITH_BROWSER=1 -t cloud-desk .
docker run -d -p 8080:8080 -e DESK_PASSWORD='改掉这个' -v "$PWD/workspace:/workspace" cloud-desk
```

工作区默认挂载在 `./workspace`，你可以把它换成任何宿主机目录。

### 方式 C：部署到自己的服务器

```bash
# 服务器上
git clone <仓库> && cd cloud-desk
pip install -r requirements.txt
DESK_PASSWORD='一个足够长的密码' PORT=8080 python3 server.py
```

**上线前必须做的三件事**（这个应用能在终端执行任意命令，等于把 shell 交出去了）：

1. **改密码**，别用示例里的。
2. **套 HTTPS 反代**（Caddy 最省事）：
   ```caddyfile
   desk.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```
   用 Nginx 的话务必带上 WebSocket 升级头，否则终端会退化成轮询模式：
   ```nginx
   proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection "upgrade";
   ```
3. **绑本机回环**：`server.py` 默认监听 `0.0.0.0`，若走反代可改成只监听 `127.0.0.1`（改 `server.py` 最后一行或加防火墙规则）。
4. 建议用独立系统用户运行，别用 root。

---

## 三、"能不能连上 WorkBuddy，用它的云端配置？"

这是被问得最多的一点，直接说结论：

> **WorkBuddy 的"配置"不是一个可以被外部项目调用的服务，它是一台分给你的沙箱容器（CPU/内存/磁盘/PTY）。**
> 所以没有"填个地址就把别人的云端配置接过来"这回事——配置跟人走，不跟项目走。

想让这个项目跑在 **WorkBuddy 提供的 4 核 / 8 GB 云端容器**里，正确路径只有一条：

**在 WorkBuddy 会话里让 Agent 把项目放进去跑，再发布成公网链接。**

```
① 在 WorkBuddy 新建一个会话（它会给你一台沙箱）
② 把本仓库放进去：git clone <仓库>  （或直接上传 zip 解压）
③ 让 Agent 执行：pip install -r requirements.txt && python3 server.py
④ 让 Agent 用平台的发布能力把 8080 端口发布出去
   → 得到一个 https://xxxx.app.workbuddy.host/?sharecode=xxx 的公网地址
```

发布后你会拿到两样东西：

- **一个公网域名**：手机、电脑都能开，不需要公网 IP；
- **网关的 `sharecode`**：这是平台网关的准入参数。项目前端会自动从当前 URL 读取并附加到请求上，**没有 sharecode 的环境（自己机器/自己的服务器）会自动跳过，不用改代码**。

这也就是当前这个实例的来历。换个人、换台沙箱，就得到他自己的一份。

### 那"模型"怎么接？两条路，自动选

进入「💬 对话」→ 顶部 ⚡ 按钮，可以看到两个通道：

**① 平台通道（免 Key，推荐）**

如果这台机器上装了 `codebuddy` CLI（WorkBuddy 沙箱里默认就有），并且**已经登录过**，
那么对话会直接复用这份登录态 —— 相当于用设备上那份额度，**不需要任何 API Key**。

实现上没做任何逆向：只是把 CLI 当成一个本地模型网关，用子进程调度
`codebuddy -p ... --output-format stream-json`，逐行解析 `text_delta` 得到流式输出。
会话靠 `--session-id` / `--resume` 续接，所以多轮上下文是原生的。

15 个可用模型（`auto` / `deepseek-v4-pro` / `glm-5.3` / `kimi-k3.1-pro` / `minimax-m3.1-flash` …）
由后端从 CLI 的 `--help` 里解析得到，跟着 CLI 升级自动同步。

> 安全默认：聊天面板**禁用全部工具**（`--tools ""`），模型只能说话，不能读写文件或执行命令。
> 想让它能读工作区，部署时设 `DESK_CHAT_TOOLS="Read,Glob,Grep"`（只有只读三件套）。

**② 自有 API（BYO，任何机器都能用）**

| 字段 | 例子 |
|---|---|
| 接口地址 | `https://api.deepseek.com/v1` |
| API Key | `sk-...` |
| 模型 | 点「拉取模型列表」自动填充，或手填 `deepseek-chat` |

只要兼容 **OpenAI 的 `/chat/completions` 协议**就能用：OpenAI、DeepSeek、通义、智谱、
Moonshot、各类自建网关（one-api / new-api）都行。配置存本机 `chat_config.json`（已 gitignore）。

没有平台通道、也没填 Key 时，对话页会退化成"留言板"模式（消息进收件箱，等 Agent 处理）。

### 会话存在哪？

**SQLite**（`data/desk.db`，单文件，WAL 模式）。

- `conversations` 表：`id / title / workspace / model / provider / sid / created_at / updated_at`
- `messages` 表：`conv_id / role / content / model / tool / ts`

不用 jsonl 的原因：多会话要按会话过滤、并发写要防撕裂、还要能排序。
不引 Redis / Postgres 的原因：这是个单机工具，多一个服务就多一个会坏的地方。

备份 = 拷走 `data/desk.db` 一个文件。老版本的 `data/chat.jsonl` 首次启动会自动导成一条"历史消息"会话。

---

## 四、配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `DESK_PASSWORD` | 随机生成 | 访问密码；仅首次启动且无 `auth.json` 时生效 |
| `WORKSPACE_ROOT` | `/workspace` → `~/workspace` | 「对话 → 工作区」和文件树能访问的根目录 |
| `BRIDGE_SECRET` | 自动生成 | 供本地脚本 / Agent 写入对话记录的校验密钥（存 `data/bridge_secret`） |
| `DESK_TMUX_SESSION` | `desk` | 终端会话的 tmux 名；同一名字的会话会被所有访问端共享 |
| `DESK_CHAT_TOOLS` | 空（禁用） | 平台通道允许的工具，如 `Read,Glob,Grep`；留空=纯对话，最安全最快 |
| `DESK_PROXY_ALLOW_PRIVATE` | 空（禁止） | 设为 `1` 才允许 `/proxy` 访问内网地址。**只在纯内网部署时打开**，公网务必留空，否则等于把云主机元数据接口暴露出去 |

---

## 五、登录态与终端会话能保持多久

这是「手机点开就是连接状态」的关键，分两层：

**登录态（免密码）**
- 登录后写入 **30 天有效**的签名 Cookie，浏览器关闭再开仍在
- 勾选「记住我」（默认勾选）会额外存一个 **180 天**令牌在 `localStorage`，Cookie 过期后自动静默续登
- 装成 PWA 后 Cookie 存在独立容器里，不会被「清理浏览器数据」顺手清掉

**终端会话（不丢工作现场）**
- shell 被托管在 **tmux** 里，网页进程只是「附着客户端」
- 关页面、刷新、切后台、**服务重启**、网关换实例，重新进来都还是原来那个终端：工作目录、命令历史、正在跑的任务都在
- 手机和电脑可以同时附着同一个会话
- 想从干净状态开始：`tmux kill-session -t desk`，或调 `POST /api/term/reset`
- **没装 tmux 会自动退回裸 bash**（功能正常，但会话随服务重启丢失）。Docker 镜像已内置

---

## 六、功能与限制（实话）

**界面**
- 移动优先，底部四标签：终端 / 对话 / 文件 / 浏览
- 深色单一强调色（赤陶橙）设计系统，内联 SVG 图标，不依赖任何外部 CDN
- 装成 PWA 后全屏无地址栏，跟原生 App 差别不大

**能用**
- 真 PTY 终端：`cd` / `ls` / 跑任何命令、跑项目、`Ctrl+C`、Tab 补全，手机端有快捷键栏
- 文件管理：打开即落在**工作区**（你自己的文件），项目源码默认折叠，点工具栏「项目文件」切换；
  按类型着色、显示大小、新建/上传/CodeMirror 编辑
- 端口预览：终端跑 `python3 -m http.server 8000` → 「浏览」页顶部出现 `8000` 胶囊，点开实时内嵌
- 内置浏览器（截图模式）：服务端 Chromium 渲染回传画面；**点画面 = 点页面元素**，
  **上下滑动 = 滚动页面**，底部输入条负责键入/回车，地址栏自带搜索引擎切换
- AI 对话：**免 Key**（复用本机 codebuddy CLI 登录态）+ BYO（自己的 OpenAI 兼容接口）双通道，真流式、可切 15 个模型
- 多会话：对话页顶部是醒目的**历史会话按钮**（含数量徽标），点开底部抽屉切换/删除；
  标题自动总结，上下文靠 CLI 原生 session 续接
- Markdown 渲染：代码块带语言标签与一键复制，支持标题/列表/引用/表格

**限制**
- **没有 GPU**，别在容器里加载大模型，8 GB 内存是硬墙，容易 OOM
- 截图浏览器每页 0.5–2 秒延迟，重度 JS / 视频站点体验一般
- 终端权限 = 运行 `server.py` 那个用户的权限。**永远不要裸奔在公网**

---

## 六点五、安全设计（部署到公网前读这段）

| 机制 | 说明 |
|---|---|
| 密码 | 首次启动随机生成 18 位；PBKDF2-SHA256（12 万次迭代）加盐存储；文件权限 `600` |
| 登录限流 | 同一 IP 5 分钟内失败 5 次即锁 5 分钟 |
| 会话令牌 | 自签名 `过期时间.HMAC`，30 天；「记住我」180 天，**磁盘上只存 SHA-256**，明文令牌不落盘 |
| Bridge 密钥 | 每实例随机生成，`data/bridge_secret`（600）；优先走请求头，`compare_digest` 恒定时间比较 |
| SSRF 防护 | `/proxy` 先解析真实 IP 再按网段判定，拦 `127/8`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、CGNAT、IPv6 本地/映射地址，以及 `metadata.*` / `*.internal`；**不跟随重定向**（防 302 绕过） |
| 路径穿越 | 所有文件接口都做 `resolve()` 后的前缀校验，只能落在工作区或项目目录内 |

⚠️ **仍然要注意**：

1. 终端 = 真 shell。任何拿到密码的人都能执行任意命令。**别把密码给别人**。
2. `/proxy` 的修法是"解析后再请求"，理论上仍有 DNS rebinding 窗口（解析与连接之间）。极高安全要求的话，把这段换成固定出口白名单。
3. 平台通道（免 Key 对话）消耗的是**这台机器**的登录额度。公开分享前请切到「自有 API」。

## 七、目录结构

```
server.py              # 后端主体：认证 / PTY / 文件 API / 端口代理 / 截图浏览器 / 路由
store.py               # 会话与消息的 SQLite 持久化（两张表，WAL）
platform_chat.py       # 平台通道：把 codebuddy CLI 当本地模型网关，流式解析
static/
  index.html           # 单页前端骨架 + 交互逻辑（零构建）
  app.css              # 设计系统：色板 / 组件 / 响应式
  manifest.json        # PWA，可「添加到主屏幕」
  vendor/              # xterm.js、CodeMirror，已本地化（不依赖 CDN）
data/
  desk.db              # 会话库（gitignore）
  chat.jsonl           # 老版本记录，首次启动自动迁移
requirements.txt
Dockerfile / docker-compose.yml
```

三个后端文件的分工：`server.py` 只负责 HTTP 路由和转发，
存储细节在 `store.py`，模型通道细节在 `platform_chat.py`。
换存储（比如上 Postgres）只改前者，换模型通道只改后者。

---

## 八、常见问题

**Q：打开一片空白？**
A：先看终端日志有没有报错。浏览器强制刷新（Ctrl/⌘+Shift+R）。若通过 WorkBuddy 网关访问，URL 必须带 `?sharecode=...`。

**Q：终端连不上 / 一直转圈？**
A：反向代理没转发 WebSocket 升级头。补上 `Upgrade` / `Connection` 两行；项目会自动降级为 HTTP 轮询，只是略慢。

**Q：忘记密码了？**
A：删掉 `auth.json` 重启，会重新生成并打印；或 `DESK_PASSWORD=新密码 python3 server.py`（需先删 `auth.json`）。

**Q：文件树看不到我的项目？**
A：把 `WORKSPACE_ROOT` 指向项目所在目录，或直接把项目放在 `WORKSPACE_ROOT` 下面。

## License

MIT（按你的需要调整）。
