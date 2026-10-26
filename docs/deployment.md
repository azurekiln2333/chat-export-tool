# 部署指南（Linux 服务器 + 本机 Windows）

本页给出网页版阅读器（Vue 3 + Node.js + SQLite）的**实际路径与可执行命令**：
先列出本机当前使用的绝对路径，再给出 Linux 生产服务器的目录布局、安装、
HTTPS 反向代理与备份方式，最后说明「永久删除」到底删掉了什么。

- 运行环境：Node.js **>= 24.15.0**（本工具支持并验证的最低版本，使用内置
  `node:sqlite`，**不需要 Python**）。
- 只需要一个 Node 进程；没有 MySQL、没有额外服务、没有端口对外暴露的要求。
- 界面为简体中文；目录名、文件名、表名、字段名、接口字段一律英文。
- 版本：本页发布包为网页阅读器 **0.2.0**（`duoshan-chat-reader-0.2.0.zip`）。
  底层 Python 导出工具仍是 **0.1.0**，两者版本独立，可分别升级。

> 本页不含任何真实账号 ID、昵称或聊天内容，路径中的 `<uid>` 代表数字账号目录。

---

## 1. 本机（Windows）当前实际路径

| 用途 | 绝对路径 |
| --- | --- |
| 项目根目录 | `E:/analysis/duoshan-chat-export-tool` |
| 后端入口（HTTP 服务） | `E:/analysis/duoshan-chat-export-tool/web/server/index.mjs` |
| 后端模块 | `E:/analysis/duoshan-chat-export-tool/web/server/store.mjs`（数据库）、`media.mjs`（附件与分段读取） |
| 前端源码 | `E:/analysis/duoshan-chat-export-tool/web/src`（`index.html` 在 `web/` 下） |
| 前端构建产物 | `E:/analysis/duoshan-chat-export-tool/web/dist`（由 `npm run build` 生成，服务端实际下发它） |
| 数据目录（导出目录） | `E:/analysis/DuoshanChatExport` |
| 数据库（删除操作作用于此） | `E:/analysis/DuoshanChatExport/reader.sqlite`（运行时另有 `reader.sqlite-wal`、`reader.sqlite-shm`） |
| 附件目录 | `E:/analysis/DuoshanChatExport/attachments` |
| 各账号原始归档 | `E:/analysis/DuoshanChatExport/<uid>/`（`chat.sqlite`、`messages.jsonl`、CSV、TXT、离线 HTML） |

`attachments/` 下的实际子目录：`voice/`、`stickers/`、`bubbles/`、`inline_emoji/`、
`audio/`、`audioemoji/`。数据库里记录的媒体路径都是相对路径（以 `attachments/`
开头），因此整个数据目录可以整体搬移。

本机启动（PowerShell）：

```powershell
cd E:\analysis\duoshan-chat-export-tool\web
npm ci                                             # 安装锁定依赖
npm run build                                      # 生成 web\dist
npm start -- --data "E:\analysis\DuoshanChatExport"  # 默认 http://127.0.0.1:8765/
```

- 改过 `web/src` 或 `web/server` 之后：`Ctrl+C` 停服 → `npm run build` → 重新 `npm start`。
- 只改了 `web/server/*.mjs` 也需要重启进程，但不必重新构建。
- 只监听 `127.0.0.1`，不要把 `--data` 指向真实数据的副本以外的东西去做破坏性测试。

> 需要局域网/公网访问时必须加口令：`npm start -- --data "..." --host 0.0.0.0 --token 你的口令`。
> 非回环地址没有口令会拒绝启动。

---

## 2. Linux 生产布局

推荐两个目录分开：**程序代码**放 `/opt`，**数据**放 `/srv`，升级时只替换程序。

```
/opt/duoshan-chat-export-tool/
  web/
    package.json
    package-lock.json
    server/
      index.mjs        # 唯一入口（node server/index.mjs）
      store.mjs
      media.mjs
    dist/              # 构建产物：index.html + assets/*.js|*.css（服务端下发的就是它）
    node_modules/      # 由 npm ci 生成，不要手工拷贝

/srv/duoshan-data/            # 从 Windows 整个导出目录上传，保持相对结构
  export_summary.json
  media_manifest.json
  decoration_manifest.json
  index.html
  reader.css
  reader.sqlite               # 删除操作只影响这个文件（+ -wal / -shm）
  attachments/
    voice/  stickers/  bubbles/  inline_emoji/  audio/  audioemoji/
  <uid>/                      # 数字账号目录，保留 chat.sqlite / messages.jsonl 等原始归档
```

- 程序目录归 root，普通用户只读即可；**数据目录必须可写**（要在里面创建/更新
  `reader.sqlite` 及其 `-wal`/`-shm`）。
- 想让数据库不落在数据目录里，用 `--database /var/lib/duoshan/reader.sqlite`。

### 2.1 需要上传哪些文件

两种发布方式，选一种即可：

**A. 生产 zip（推荐，服务器不装构建工具）**

发布包名为 `duoshan-chat-reader-0.2.0.zip`（构建在项目的 `dist/` 目录下：
`E:/analysis/duoshan-chat-export-tool/dist/duoshan-chat-reader-0.2.0.zip`）。顶层是
`web/`，另附四份文档。生产包只包含运行所需内容：

```
web/package.json
web/package-lock.json
web/server/index.mjs  web/server/store.mjs  web/server/media.mjs
web/dist/index.html   web/dist/assets/*
README.md  README_zh-CN.md  docs/web-reader.md  docs/deployment.md
```

不含 `web/src`、`web/tests`、`node_modules`。上传后解压到 `/opt` 的上一层目录，
得到 `/opt/duoshan-chat-export-tool/web`：

```bash
sudo mkdir -p /opt/duoshan-chat-export-tool
sudo unzip duoshan-chat-reader-0.2.0.zip -d /opt/duoshan-chat-export-tool
cd /opt/duoshan-chat-export-tool/web
sudo npm ci --omit=dev
```

`--omit=dev` 跳过 `vite` 与 `@vitejs/plugin-vue`（只在构建时需要）。因为 `dist/`
已经构建好，服务器上不需要再跑 `npm run build`。

**B. 源码构建**

```bash
cd /opt/duoshan-chat-export-tool/web
npm ci                 # 含 vite / plugin-vue
npm run build          # 生成 dist/
```

然后可以 `npm prune --omit=dev` 去掉构建依赖以减小体积。

### 2.2 Node 版本检查

```bash
node -v          # 本工具要求 v24.15.0 或更高
```

低于 24.15.0 不在本工具的支持与验证范围内（`node:sqlite` 在更早的 Node 版本中已
存在，但 API 与行为可能不同），启动可能失败。`web/package.json` 的 `engines`
字段声明了 `>=24.15.0`。

### 2.3 数据目录权限与首次启动

```bash
sudo useradd --system --home /srv/duoshan-data --shell /usr/sbin/nologin duoshan
sudo mkdir -p /srv/duoshan-data
# 上传/解压数据到 /srv/duoshan-data 之后：
sudo chown -R duoshan:duoshan /srv/duoshan-data
sudo chmod 750 /srv/duoshan-data

cd /opt/duoshan-chat-export-tool/web
sudo -u duoshan DUOSHAN_ACCESS_TOKEN='请换成足够长的随机口令' \
  node server/index.mjs --data /srv/duoshan-data --host 127.0.0.1 --port 8765
```

首次启动会扫描 `messages.jsonl` 等文件并在数据目录内建立 `reader.sqlite`
（数据量大时第一次会比较慢，之后按指纹判断，未变更就跳过导入）。

### 2.4 systemd 常驻

`/etc/systemd/system/duoshan-reader.service`：

```ini
[Unit]
Description=Duoshan chat reader
After=network.target

[Service]
Type=simple
User=duoshan
Group=duoshan
WorkingDirectory=/opt/duoshan-chat-export-tool/web
Environment=DUOSHAN_ACCESS_TOKEN=请换成足够长的随机口令
ExecStart=/usr/bin/node server/index.mjs --data /srv/duoshan-data --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3
# 让 SQLite 的 WAL 正常落盘
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now duoshan-reader
sudo systemctl status duoshan-reader
curl -fsS http://127.0.0.1:8765/api/session
```

`curl` 返回 `{"authenticated":false,"auth_required":true}` 之类的 JSON 即表示进程健康。

### 2.5 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--data DIR` | 必填 | 导出数据目录 |
| `--host` | `127.0.0.1` | 监听地址；非回环必须配 `--token` |
| `--port` | `8765` | 端口，`0` 表示随机空闲端口 |
| `--token` | 环境变量 `DUOSHAN_ACCESS_TOKEN` | 访问口令；不要把口令写进 URL 或提交到仓库 |
| `--database PATH` | `DIR/reader.sqlite` | 把数据库放到数据目录之外 |
| `--reindex` | 关闭 | 强制重新导入（保留备注/编辑/删除记录） |

---

## 3. HTTPS 反向代理

服务本身只监听回环，由 nginx/Caddy 终止 TLS。要点只有两条：
**必须转发 `Host`**，并转发 `X-Forwarded-Proto: https`（这样会话 Cookie 才会带 `Secure`）。
服务会用 `Origin` 与 `Host` 比对来拒绝跨站写入，`Host` 丢失或被改写成上游地址时，
保存/删除会被 403 拒绝。

### 3.1 站点根路径

```nginx
server {
    listen 443 ssl;
    server_name reader.example.com;

    ssl_certificate     /etc/letsencrypt/live/reader.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reader.example.com/privkey.pem;

    client_max_body_size 2m;          # 接口请求体上限 1 MiB，留一点余量

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP         $remote_addr;

        # 语音/视频建议关闭代理侧缓冲，便于拖动进度
        proxy_buffering off;
    }
}
```

- `proxy_set_header Host $host;` 不要省略，也不要用 `$proxy_host`。
- 端口如果是 443，`Host` 不带端口与 `Origin` 的默认端口等价，服务端已做归一化。

### 3.2 可选前缀 `/chat/`（代理处剥掉前缀）

前端所有请求都是相对路径（`api/...`、`media/...`），`vite` 使用相对 `base: './'`，
因此挂在子路径下无需改数据库、无需改构建。

```nginx
location = /chat { return 308 /chat/; }

location /chat/ {
    proxy_pass http://127.0.0.1:8765/;      # 末尾的 / 表示剥掉 /chat 前缀
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
}
```

访问 `https://reader.example.com/chat/` 即可。反代**不要**改写 `/api`、
`/media` 路径本身，也**不要**缓存 `/api/*`（响应已带 `Cache-Control: no-store`）。

### 3.3 安全提示

这是**单用户**阅读器，不是多用户服务。请始终放在 HTTPS + 口令之后，
只给可信设备使用；不要直接把 `8765` 端口暴露到公网。

---

## 4. 备份与恢复

`reader.sqlite` 运行在 WAL 模式，**服务运行时单独复制一个 `reader.sqlite` 不是一致快照**。

### 4.1 停服复制（最简单）

```bash
sudo systemctl stop duoshan-reader
sudo rsync -a --delete /srv/duoshan-data/ /backup/duoshan-data-$(date +%F)/
sudo systemctl start duoshan-reader
```

Windows 本机同理：先 `Ctrl+C` 停掉 `npm start`，再复制整个
`E:\analysis\DuoshanChatExport`（Linux 上不可在被占用时替换 SQLite 文件）。

### 4.2 不停服快照（`VACUUM INTO`）

```bash
cd /srv/duoshan-data
node --input-type=module -e "
import { DatabaseSync } from 'node:sqlite';
const db = new DatabaseSync('reader.sqlite');
try { db.exec(\"VACUUM INTO '/backup/reader-$(date +%F).sqlite'\"); } finally { db.close(); }
"
```

`VACUUM INTO` 生成的是完整一致的单文件副本，恢复时停服后替换 `reader.sqlite`
并删除旧的 `-wal`/`-shm`。

---

## 5. 删除边界（务必阅读）

网页里的「永久删除」是**对阅读器数据库的真实 SQL 删除**，只影响 `reader.sqlite`：

| 对象 | 删除后 |
| --- | --- |
| `reader.sqlite` 中的消息行（含 `raw_json`） | 立即消失，接口与页面都不再返回 |
| 该消息的备注/编辑/隐藏记录 | 一并删除 |
| 会话的统计（条数、首末时间） | 立即重算；空会话被移除 |
| `deletions` 抑制表 | 只新增「种类 + SHA-256 哈希 + 时间」，**不保存任何正文、昵称或原始 JSON** |
| 原始 `<uid>/chat.sqlite` | **不受影响**，仍是完整原始行 |
| 原始 `<uid>/messages.jsonl`、CSV、TXT、离线 HTML | **不受影响** |
| `attachments/` 里的语音、表情、气泡等共享媒体 | **不受影响** |

由此有两条必须知道的后果：

1. **删除不等于销毁原始归档。** 想彻底抹掉内容，需要自己处理
   `<uid>/chat.sqlite`、`messages.jsonl`、CSV/TXT 和离线 HTML，本工具不会代劳。
   同时注意：媒体文件是**共享**的，删掉会破坏其它仍保留的消息。
2. **删除 `reader.sqlite` 会同时丢失两类东西：**
   - 所有本地编辑、正文改写、备注、隐藏状态；
   - `deletions` 抑制记录。之后重新导入会把被删消息**重新导入**回来。

   因此 `reader.sqlite` 既不是备份、也不能当作删除凭证。要长期维持删除结果，
   必须保留 `reader.sqlite`（或至少保留其中的 `deletions` 表）。

「隐藏」是另一回事：隐藏只改注解，可随时恢复，不写抑制表；「恢复原文」
（`POST api/message/restore`）只清注解，**不会**变成永久删除。

---

## 6. 升级

```bash
sudo systemctl stop duoshan-reader
sudo cp -a /opt/duoshan-chat-export-tool/web /opt/duoshan-chat-export-tool/web.bak-$(date +%F)
sudo unzip -o duoshan-chat-reader-0.2.0.zip -d /opt/duoshan-chat-export-tool
cd /opt/duoshan-chat-export-tool/web && sudo npm ci --omit=dev
sudo systemctl start duoshan-reader
curl -fsS http://127.0.0.1:8765/api/session
```

- **不要**删除 `/srv/duoshan-data`，升级只替换程序。
- 数据库结构升级是增量的：备注、编辑和 `deletions` 抑制记录会保留。
  如果 `messages.jsonl` 已经不在了，仍可用现有 `reader.sqlite` 提供服务。
- 升级后若页面空白，先确认 `dist/` 是否随包更新、`index.html` 与 `assets/` 是否同批次。

## 7. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 启动即报 `node:sqlite` 相关的模块/语法错误 | 当前 Node 低于本工具要求的 24.15.0，升级 Node |
| 打开页面空白、`/api` 正常 | `dist/` 缺失或未重新构建；`npm run build` |
| 保存/删除返回 403 `origin_rejected` | 反代没有转发 `Host`，或改成了上游地址 |
| 登录后没有保持登录 | 浏览器是 http 而 Cookie 带 `Secure`，或反代未转发 `X-Forwarded-Proto` |
| 语音不能拖动进度 | 反代开启了大缓冲；`proxy_buffering off` |
| 提示数据库无法读取 | `reader.sqlite` 被占用或权限不足；确认数据目录可写 |
| 删除后重启又出现 | 删过 `reader.sqlite`，抑制记录丢失；从备份恢复该数据库 |

相关文档：[web-reader.md](web-reader.md)（数据模型与接口）、
[usage.md](usage.md)（生成导出）、[troubleshooting.md](troubleshooting.md)。
