<div align="center">

# 云桌面 · Cloud Desk

**手机点开即用的 Linux 工作台** —— 真终端、AI 对话、文件管理、网页预览，都在一个页面里。

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PWA](https://img.shields.io/badge/PWA-ready-5A0FC8?logo=pwa&logoColor=white)](#)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![License](https://img.shields.io/badge/License-MIT-d97757)](LICENSE)

<img src="docs/screenshots/hero-1200.png" alt="终端 / 对话 / 文件 / 浏览 四个标签页" width="100%">

</div>

---

## 这是什么

一台跑在浏览器里的 Linux 桌面。后端只有一份 `server.py`（FastAPI），前端是原生 HTML/JS，第三方资源（xterm.js、CodeMirror）全部本地化——**不需要构建，不需要 npm**。

它不绑定任何云平台：跑在谁的机器上，就吃谁的配置，没有账号体系，一个密码就是全部。

**最省事的跑法，是不买服务器。** 如果你本来就在用 WorkBuddy，这个项目可以直接跑在它的云端容器里：不用装环境、不用配域名，手机上打开链接就是一台能干活的小电脑。终端里的进程、装好的依赖、改过的文件都留在那台容器里，下次打开接着用；对话的平台通道复用的也是同一份登录态，不用另外申请 Key。手机在这里负责屏幕、键盘和网络，算力在云端那台容器里。

| 跑在哪 | 得到什么 | 要接受什么 |
|---|---|---|
| **WorkBuddy 云端容器** | 零部署、开箱即用，运行环境和模型额度都是现成的 | 容器空闲时可能被回收或重启，不适合当 24×7 常驻服务 |
| **自己的 VPS / 小主机 / Docker** | 长期在线、自己的域名、完全可控 | 要自己运维、自己准备环境和备份 |

| 标签 | 你能做什么 |
|---|---|
| 💻 **终端** | 真 PTY，`cd` / 跑项目 / `Ctrl+C` / Tab 补全；会话托管在 tmux，关页面、重启服务都还在 |
| 💬 **对话** | 免 Key（复用本机 `codebuddy` CLI 登录态）或自带 OpenAI 兼容接口；能克隆、能装依赖、能起服务，跑完直接弹出预览 |
| 📁 **文件** | 浏览 / 上传 / 新建 / 在线编辑（CodeMirror），按类型着色、显示大小 |
| 🌐 **浏览** | 终端或对话里跑起来的端口自动变胶囊，点开即内嵌预览；也可用服务端 Chromium 打开任意网站 |

## 为什么是「手机优先」

这个项目一开始就是为了**在手机上用**才写的，不是把桌面版缩一缩：

- 底部四标签、单列布局，拇指可达；桌面端才展开成侧栏
- PWA 可"添加到主屏幕"，全屏无地址栏，和原生 App 几乎没差别
- 登录态 30 天（记住我 180 天），终端会话常驻，**掏出手机就是连接状态**
- 手势：点画面即点击、上下滑即滚动、底部输入条负责键入与回车
- 桌面端同样能用，只是内容区限宽 1000px，不再铺满整屏

> 一句话：**移动优先，桌面兼容。** 手机是主场，电脑是顺带。

📱 **[手机端使用指引 →](docs/USAGE.md)** —— 第一次打开、四个标签各自怎么操作、手机上容易踩的坑，一页讲完。

## 30 秒跑起来

### 本机直跑

```bash
git clone https://github.com/whs2007/cloud-desk.git && cd cloud-desk
pip install -r requirements.txt
python3 server.py
```

首次启动会生成随机密码并打印在终端，浏览器打开 <http://localhost:8080> 输入即可。
想自己指定：`DESK_PASSWORD=你的密码 python3 server.py`。

### Docker（推荐）

```bash
# 没有默认密码：不设置会直接启动失败，从机制上避免"照抄示例口令"
DESK_PASSWORD='换成你自己的强密码' docker compose up -d --build
```

需要内置截图浏览器（能打开任意网站）时，把 `docker-compose.yml` 里的
`WITH_BROWSER: "0"` 改成 `"1"`，首次构建会多花几分钟装 Chromium。

### 自己的服务器

```bash
git clone https://github.com/whs2007/cloud-desk.git && cd cloud-desk
pip install -r requirements.txt
DESK_PASSWORD='一个足够长的密码' python3 server.py
```

上线前**必须**做三件事——这个应用能在终端执行任意命令，等于把 shell 交出去了：

1. **改密码**，别用示例里的；
2. **套 HTTPS 反代**，Caddy 两行搞定：
   ```caddyfile
   desk.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```
   Nginx 记得带 WebSocket 升级头，否则终端会退化成轮询：
   ```nginx
   proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection "upgrade";
   ```
3. **绑回环 + 非 root 用户**：`server.py` 默认监听 `0.0.0.0`，走反代时改成 `127.0.0.1`，并用独立系统用户运行。

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `DESK_PASSWORD` | 随机生成 | 访问密码，仅首次启动（无 `auth.json`）时生效 |
| `WORKSPACE_ROOT` | `/workspace` → `~/workspace` | 文件树与终端的工作区根目录 |
| `DESK_TMUX_SESSION` | `desk` | tmux 会话名，同名会话被所有访问端共享 |
| `DESK_CHAT_TOOLS` | `Bash,Read,Glob,Grep,Write,Edit` | 平台通道放行的工具。设 `Read,Glob,Grep` = 只读；设空字符串 = 纯聊天，不许动手 |
| `DESK_HIDE_PORTS` | `8089` | 端口探测里额外藏掉的端口（逗号分隔），用于屏蔽宿主自带服务 |
| `DESK_PROXY_ALLOW_PRIVATE` | 空（禁止） | 设为 `1` 才允许 `/proxy` 访问内网。**仅纯内网部署时打开** |
| `BRIDGE_SECRET` | 自动生成 | 本地脚本写入对话记录的校验密钥（`data/bridge_secret`） |

## 功能细节

**终端**：shell 跑在 tmux 里，网页只是"附着客户端"。关页面、刷新、切后台、重启服务、换实例，重新进来还是原来那个终端——工作目录、命令历史、正在跑的任务都在。手机和电脑可以同时附着。没装 tmux 会自动退回裸 bash（功能正常，重启即丢）。终端默认落在 `WORKSPACE_ROOT`。

**对话**：两条通道，自动选。

- **平台通道（免 Key）**：机器上装了 `codebuddy` CLI 且登录过，就直接复用这份登录态；实现上只是把 CLI 当本地模型网关，`--session-id/--resume` 续上下文，没做任何逆向。
- **自有 API（BYO）**：任何兼容 OpenAI `/chat/completions` 的服务——OpenAI、DeepSeek、通义、智谱、Moonshot、one-api 都行。

平台通道默认**开着工具**（`Bash,Read,Glob,Grep,Write,Edit`，通过 `--permissions.allow` 放行），所以它能真的克隆仓库、装依赖、起服务。想收回去：`DESK_CHAT_TOOLS="Read,Glob,Grep"`（只读）或 `DESK_CHAT_TOOLS=""`（纯聊天）。开头那句话很重要——默认配置等于把这台机器的 shell 交给模型，只适合自己用的私有部署。

对话页标题下面会写明当前能力（`仅对话` / `可读文件` / `可执行`），省得你试了才发现它动不了手。生成过程中右下角是停止键，随时能打断。

**等的时候，屏幕上有东西在动。** 这是专门为「慢」做的一块：平台通道冷启动要 10~20 秒，`npm install` 能跑几分钟，如果这期间只有一个转圈，你根本分不清它在干活还是已经死了。所以每轮回复顶部常驻一条状态：

```
● 执行 Bash                    32s
  命令在后台跑，结束会自动接着做
  ◠ Bash npm install             18s
  ✓ Bash git clone …             12s
```

- 阶段会变：唤醒模型 → 模型在思考 → 执行命令 → 作答
- 秒表在前端本地跳，后端一个字节都没发时也照样走
- 8 秒没新内容就来一次心跳，文案按阶段换（「装依赖可能几分钟」而不是「加载中」）
- 正文一到，整块收成一行 `执行过程 · 3 步 · 52s`，点开能看每条命令的真实输出头尾
- 停止时显示「已停止」，结束时给总耗时和步数——不留一个永远在转的圈

代价要说清楚：这些是**过程可见**，不是**过程变快**。首字该多慢还是多慢（纯聊天 10~20 秒，带工具更久），只是这段时间不再是黑箱。

### 在对话里把项目跑起来，旁边直接看

这是本项目的核心闭环，一句话就能走完：

> 「把这个项目拉下来跑起来：`https://github.com/xxx/yyy`」

1. 模型用 Bash 克隆到工作区、看清结构、选最轻的启动方式；
2. 用 `tmux new-session -d -s web-<端口> -c <项目目录> '<启动命令>'` 放到后台——**不会**把你晾在一个跑不完的前台命令上；
3. `curl` 自检拿到 2xx/3xx 后，回复末尾写一行 `PORT: <端口>`；
4. 后端发现这个端口**真的在监听**（不是模型瞎报的数字），通过 SSE 推一张「服务卡」到对话里；
5. 点卡上的「在这里打开」，全屏 iframe 走 `/preview/<端口>/` 反代，不离开对话就看到页面。

服务卡出现前，前端每 4 秒轮询一次新监听端口，所以即使模型忘了写 `PORT:`，卡片也会自己冒出来。顶栏绿色的 `● N` 是长期入口：列出所有在跑的服务，可逐个打开或停止。

<p align="center">
  <img src="docs/screenshots/05-service-card.png" alt="对话里的服务卡" width="32%">
  <img src="docs/screenshots/06-service-preview.png" alt="对话内全屏预览" width="32%">
  <img src="docs/screenshots/07-service-sheet.png" alt="运行中的服务列表" width="32%">
</p>

反代不是简单转发，做了三件让 SPA 也能跑的事：注入 `<base>`、把标签属性里以 `/` 开头的地址改写成 `/preview/<端口>/...`、把 302 的 `Location` 拉回前缀。POST 也会转发，登录表单能提交。

**会话存储**：SQLite 单文件（`data/desk.db`，WAL）。两张表：`conversations`、`messages`。不用 jsonl 是因为要按会话过滤、并发写防撕裂、可排序；不引 Redis/Postgres 是因为单机工具多一个服务就多一个会坏的地方。备份 = 拷走这一个文件。

**浏览**：终端里 `python3 -m http.server 8000`，浏览页顶部会冒出 `8000` 胶囊，点开即内嵌。任意网址走服务端 Chromium 截图回传，点画面 = 点元素，上下滑 = 滚动页面。

## 安全设计

部署到公网前请读这段。

| 机制 | 说明 |
|---|---|
| 密码 | 18 位随机；PBKDF2-SHA256（12 万次迭代）加盐；文件权限 `600` |
| 登录限流 | 同 IP 5 分钟内失败 5 次锁 5 分钟 |
| 会话令牌 | 自签名 `过期时间.HMAC`，30 天；「记住我」180 天，**磁盘只存 SHA-256** |
| Bridge 密钥 | 每实例随机，`data/bridge_secret`（600），请求头校验 + 恒定时间比较 |
| SSRF 防护 | `/proxy` 先解析真实 IP 再按网段判定，拦 `127/8`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、CGNAT、IPv6 本地/映射、`metadata.*`、`*.internal`；**不跟随重定向** |
| 路径穿越 | 所有文件接口 `resolve()` 后做前缀校验，越界直接拒 |
| 服务停止 | `/api/services/stop` 拒绝停系统端口（`<1024`、自身端口、`DESK_HIDE_PORTS`），拒绝停云桌面自身的常驻进程 |

仍然要注意：

- 终端 = 真 shell，拿到密码的人就能执行任意命令；
- **对话默认也能执行命令**（`DESK_CHAT_TOOLS` 含 `Bash`）。这是产品选择——不开就没法"拉项目、跑起来"；不放心就设成只读或留空；
- `/proxy` 的"解析后再请求"理论上仍有 DNS rebinding 窗口，安全要求极高时换固定出口白名单；
- 平台通道消耗的是**这台机器**的登录额度，公开分享前请切到自有 API。

## 架构

```
浏览器（原生 JS，零构建）
   │  WebSocket /ws/terminal        ← PTY，挂在 tmux 上
   │  HTTP  /api/*                  ← 文件 / 会话 / 设置
   ↓
server.py (FastAPI)  ── store.py (SQLite)  ── platform_chat.py (codebuddy CLI)
```

三个后端文件分工明确：`server.py` 只管路由与转发，存储细节在 `store.py`，模型通道在 `platform_chat.py`。换存储只改前者，换模型通道只改后者。

```
server.py              # 路由：认证 / PTY / 文件 API / 端口代理 / 截图浏览器
store.py               # 会话与消息的 SQLite 持久化
platform_chat.py       # 平台通道：把 codebuddy CLI 当本地模型网关
say.py                 # 小工具：让本地脚本把消息写进对话
static/
  index.html           # 单页骨架 + 交互逻辑
  app.css              # 设计系统：色板 / 组件 / 响应式
  manifest.json        # PWA
  vendor/              # xterm.js、CodeMirror（本地化，不走 CDN）
docs/screenshots/      # README 配图
data/                  # 会话库（gitignore）
```

## 常见问题

**打开一片空白？**
先看服务端日志。强制刷新（Ctrl/⌘+Shift+R）。若经网关访问，URL 必须带 `?sharecode=...`。

**终端一直转圈？**
反代没转发 WebSocket 升级头。补上 `Upgrade` / `Connection` 两行，或等它自动降级为轮询。

**我让它把某个 GitHub 项目拉下来跑，它只回了一堆命令？**
先确认对话页标题下面写的是「平台通道 · 可执行」而不是「· 仅对话」。是后者说明 `DESK_CHAT_TOOLS` 被设空或设成了只读——放开它才能动手。

**它说跑起来了，但点开是文件列表 / 白屏？**
大概率是服务的根目录不对（在 `/workspace` 而不是项目目录里起的），或者依赖没装完。让它「重新在这个项目目录里起一遍」即可；真跑的是 `tmux new-session -d -c <项目目录>`，你在 💻 终端里 `tmux ls` 能看到会话名。

**`git clone` 一直超时？**
沙箱到 GitHub 的直连经常断（TLS 中断）。模型会自己换 jsdelivr / codeload zip 之类的镜像，提示词里也要求"同一个域名最多试 2 次"。真的拉不动，就在终端里手动上传 zip。

**页面样式错乱、热更新不生效？**
反代能改 HTML 里的绝对路径，改不了 JS 里拼出来的接口地址，也不转发 WebSocket——所以 Vite/Next 的 HMR 会失败，改完点 ⟳ 刷新即可。完全依赖 WebSocket 的应用（部分协作工具）建议用 `↗` 在系统浏览器里开。

**忘记密码？**
删掉 `auth.json` 重启会重新生成并打印；或 `DESK_PASSWORD=新密码 python3 server.py`（需先删 `auth.json`）。

**文件树看不到我的项目？**
把 `WORKSPACE_ROOT` 指到项目所在目录。前端会自动跟随这个路径，不需要改代码。

**能接 WorkBuddy 的云端配置吗？**
WorkBuddy 给的是一台沙箱容器，不是一个可调用的配置服务，所以"填个地址接过来"不存在。正确路径是在会话里让 Agent 把项目跑起来，再发布成公网链接。

## 限制（实话）

- **没有 GPU**，别在容器里加载大模型；8 GB 内存是硬墙，容易 OOM
- 截图浏览器每页 0.5–2 秒延迟，重度 JS / 视频站点体验一般
- 单机单进程，没有多租户隔离；要多人用请各自部署
- 对话里跑一次的冷启动 + 装依赖可能几分钟，别指望秒回；`npm install` 那种堵在同一个域名上反复重试的，提示词已要求最多试两次就换路子
- 预览反代不转发 WebSocket，HMR/协作类应用会退化（刷新可用，实时不变）

## 参与

Issue / PR 都欢迎。改动前端不需要构建步骤，改完刷新即可；后端就一个 `server.py`，改完重启。

仓库自带一份 CI 示例（`docs/ci.yml`）：语法检查 + 启动冒烟。想让它跑起来，复制到 `.github/workflows/ci.yml` 再提交即可（提交 workflow 文件要求 token 带 `workflow` 权限）。

## License

[MIT](LICENSE)
