# 多闪聊天记录导出工具（中文快速上手）

把本地 Android「多闪」（`my.maya.android`）备份导出成一个可长期保存、完全离线的
档案：独立的 SQLite 数据库、原始 JSONL、防表格公式注入的 CSV、按会话拆分的文本，
以及可以直接用浏览器打开的离线阅读页面，并尽量还原语音、表情、文字小表情和聊天气泡。

工具只**读取**你提供的备份，不会登录、不会修改原始文件，也**默认不联网**。
英文说明见 [README.md](README.md)。

## Vue 网页版：查阅与编辑

网页使用 **Vue 3 + Node.js + SQLite**，不需要 Python 运行环境。Python 部分负责
从 Android 备份提取原始归档；网页版通过 Node 接口查询 SQLite。

需要 Node.js 24.15 或更新版本，在项目目录执行：

```powershell
cd web
npm ci
npm run build
npm start -- --data "D:\DuoshanChatExport"
```

打开终端显示的本机地址。首次启动会建立 `reader.sqlite`；搜索、分页和编辑都基于
数据库。正文修改、备注、隐藏状态单独保存，可恢复原文。原始归档继续保留。

网页还提供月历视图：按导出时区统计每天的消息数，点击某一天只列出当天消息，
清空日期回到完整列表。

### 隐藏、恢复与永久删除的区别

| 操作 | 影响 | 可撤销 |
| --- | --- | --- |
| 隐藏 | 只写备注表，消息行保留 | 可以，取消隐藏即可 |
| 恢复原文 | 清空正文改写/备注/隐藏，版本号继续增长 | 可以，重新编辑即可 |
| **永久删除** | 真正 `DELETE` 消息行、备注行与会话统计 | **不可以** |

- 单条、勾选多条（每批最多 500 条）和整个会话都可以删除，删除前有中文确认，
  写明条数、影响范围与不可恢复；批次中只要有失效项就整体失败、一条都不删。
- 数据库里**没有回收站**，也不保留被删内容；只记录「种类 + SHA-256 哈希 + 时间」，
  这样重新导入时不会把已删记录带回来。
- **删除不会抹掉原始导出归档**：`<uid>/chat.sqlite`、`messages.jsonl`、CSV/TXT/离线
  HTML 以及 `attachments/` 里的共享媒体都保持原样。要真正抹除，请自行处理这些
  文件（媒体是共享的，删除会破坏其它未删消息）。
- **删除 `reader.sqlite` 会同时丢失编辑、备注、隐藏状态，以及删除抑制记录**，
  被删消息会在下次导入时重新出现。它不是备份。

把整个导出目录上传服务器，保持 `reader.sqlite`、`attachments/` 等文件的相对位置，
再启动 Node 服务。媒体地址采用相对路径，换目录或域名无需修改数据库中的盘符。
远程部署需设置访问口令。数据模型与接口见 [docs/web-reader.md](docs/web-reader.md)，
本机与 Linux 服务器的实际路径、`npm ci`/构建/启动命令、HTTPS 反向代理和备份步骤见
[deployment.md](docs/deployment.md)。

> 语言边界（重要）
> - 生成出来的**目录名和文件名全部是英文**，例如 `chat.sqlite`、
>   `messages.jsonl`、`conversation_001_page_001.html`。
> - 代码、数据库表/字段、清单与校验报告的键名、控制台信息也全部是英文。
> - 默认的 **HTML 阅读界面是简体中文**（页面标注 `<html lang="zh-CN">`，按钮、
>   标题、提示等均为中文）。
> - **原始聊天内容、联系人昵称、消息正文不会被翻译**，按原样保存。
> - 界面语言不会影响导出的数据；`validate` 只校验结构与链接，不依赖界面文字。

## 环境要求

- Python **3.10 或更高版本**。
- 已验证 **Android** 客户端的数据布局；其它平台或版本不保证适用。
- 加密数据库（SQLCipher）需要可选依赖；HEIC 图片需要可选依赖；离线转码需要 ffmpeg。

## 安装

```powershell
# 一键创建 .venv 并安装全部依赖（含 SQLCipher、HEIC，并自检）
./install.ps1

# 只安装核心依赖
./install.ps1 -CoreOnly

# 或者用 pip 手动安装
pip install ".[full]"      # 含 sqlcipher3-wheels 与 pillow-heif
pip install .              # 仅核心
```

安装完成后可用 `./run.ps1 <命令>`，等价于 `python -m duoshan_export <命令>`，
也可直接用脚本 `duoshan-export <命令>`。

## 快速开始

```powershell
# 1. 查看备份里有几个账号、各有多少条消息（临时解压，不导出）
./run.ps1 inspect "D:\backup\DataBackup"

# 2. 导出到一个新目录（默认离线）
./run.ps1 export "D:\backup\DataBackup" -o "D:\DuoshanChatExport"

# 3. 打开离线阅读页面（界面为简体中文）
start "D:\DuoshanChatExport\index.html"
```

`export` 会自动完成：解压/快照 → 解密各账号 → 写 JSONL/CSV/联系人/统计并生成页面 →
准备媒体与装饰资源 → 重建阅读页并刷新 SQLite 媒体索引 → 校验。校验不通过会以非零
退出码结束。

## 常用命令

### `inspect SOURCE`

只探测账号和消息数量，不导出。

- `--work-dir DIR` 指定解压/快照目录（默认使用临时目录，且必须与备份分开）
- `--sqlcipher-module-path DIR` 指定已安装的 SQLCipher 绑定所在目录
- `--json` 以 JSON 输出

### `export SOURCE -o OUTPUT`

| 参数 | 说明 |
| --- | --- |
| `SOURCE` | 备份目录、`*.tar.zst` 压缩包、`databases` 目录，或单个数据库文件 |
| `-o`, `--output DIR` | 导出目标目录（必填） |
| `--account UID` | 只导出该账号，可重复；默认导出全部账号 |
| `--timezone TZ` | 显示用 IANA 时区（默认 `Asia/Shanghai`） |
| `--page-size N` | 每页消息数，1–5000（默认 600） |
| `--resume` | 在同一未变更来源上继续/刷新已存在的导出 |
| `--download-media` | 允许从原始链接下载语音、表情和气泡（默认关闭，保持离线） |
| `--reuse-media EXPORT` | 从已有导出中复制可校验匹配的媒体 |
| `--decode-media` | 校验时额外解码音频与每一帧图片 |
| `--work-dir DIR` | 私有解压/快照目录（默认 `OUTPUT/.work`） |
| `--workers N` | 并发下载数，1–32（默认 6） |
| `--sqlcipher-module-path DIR` | SQLCipher 绑定所在目录 |
| `--ffmpeg`, `--ffprobe` | 可执行文件名或路径 |
| `--json` | 以 JSON 输出 |

### `media OUTPUT`

为已存在的导出重新处理媒体：复用/下载、重建阅读页、刷新 SQLite 媒体索引并校验。

- `--source SOURCE` 可选，提供原始备份以刷新本地气泡/小表情素材
- 其余参数与 `export` 相同（同样支持 `--download-media`、`--reuse-media`、`--decode-media` 等）

### `validate OUTPUT`

独立校验已有导出，会把报告写入导出目录：

- `validation_results.json`：数据完整性（始终生成）
- `reader_media_validation_results.json`：页面与媒体交叉校验（始终生成）
- `media_validation_results.json`：逐文件解码结果（配合 `--decode-media`）

参数：`--decode-media`、`--ffmpeg NAME`、`--json`。

### `doctor`

检查 Python 版本、依赖是否可用、`ffmpeg`/`ffprobe` 是否就绪；核心依赖缺失时退出码为 1。

## 支持的输入

1. 已解压的 Android 应用数据根目录（含 `databases/` 或 `my.maya.android` 包目录）。
2. 含 `*.tar.zst`（或 `.tar`、`.tar.gz`、`.tgz`、`.zst`）的 DataBackup 目录。
3. 一个 `databases` 目录。
4. 单个多闪数据库文件（如 `encrypted_<uid>_im.db`）。

压缩包按不可信内容处理：绝对路径、`..` 穿越、Windows 保留名、盘符、链接/设备文件
和超大成员会被跳过，且绝不会写到工作目录之外。原始备份不会被修改。

## 输出目录（文件名均为英文）

```
OUTPUT/
  index.html                       入口页（简体中文界面）
  reader.css                       页面样式
  media_manifest.json              每个媒体资源的下载结果、路径、哈希与消息关联
  decoration_manifest.json         气泡 ID、配置与小表情素材映射
  export_summary.json              机器可读的导出与媒体统计
  validation_results.json          数据校验报告
  reader_media_validation_results.json
  media_validation_results.json    使用 --decode-media 时生成
  README.txt                       导出目录内的中文说明
  attachments/                     共享的本地媒体（语音、表情、气泡等）
  <uid>/                           每个账号一个目录
    chat.sqlite                    独立的解密数据库（保留全部原始行）
    messages.jsonl                 每行一条原始 msg 记录，另含 _export 信息
    messages.csv                   可读的时间/发送者信息，便于表格查看
    contacts.json / contacts.csv   用于补全昵称的联系人缓存
    conversation_metadata.json     会话列表、群信息、成员、附件表
    statistics.json                计数、消息类型、时间范围
    conversation_index.csv         会话索引
    conversations/                 conversation_NNN_page_NNN.html
    transcripts/                   conversation_NNN.txt
```

`messages.jsonl`、`chat.sqlite`、`messages.csv` 与 TXT 保留原始文本和标识符；CSV 可能
给某个单元格加前导单引号，以避免表格软件把消息当公式执行或破坏超长 ID（JSONL 与
SQLite 始终保存原值）。ID 可能超出 JavaScript 安全整数范围，建议用 Python、SQLite
或支持大整数的 JSON 读取器处理。

## 离线与隐私

- 默认完全离线；只有显式加 `--download-media` 才会联网下载语音、表情和气泡。
- 不会伪造任何 URL；下载失败的资源会记录失败原因并在页面显示。
- `--resume` 很严格：只允许**同一未变更来源**且**同一账号选择**继续导出；来源变化、
  账号选择不同，或目录并非本工具创建，都会拒绝并提示。
- 多个账号保存了同一会话副本时分别保留，不跨账号去重。
- `msg.deleted` 非零的行会导出并标注，但这不等同于确认被撤回。

## 常见问题

- 提示需要 SQLCipher：`pip install "sqlcipher3-wheels"`，或用
  `--sqlcipher-module-path` 指向包含 `sqlcipher3` 的目录。
- 删除后重启又被导回来：`reader.sqlite` 被删过或换了新的，删除抑制记录已丢失。
- 网页保存/删除提示页面地址与服务地址不一致：反向代理没有转发 `Host`。
- 提示需要 zstandard：`pip install zstandard`。
- 提示时区无效：改用 `Asia/Shanghai`、`UTC` 等 IANA 名称，并确保已安装 `tzdata`。
- 提示输出目录非空：换一个目录，或对同一来源加 `--resume`。
- 页面显示素材缺失：该气泡/媒体在备份中本就不存在或链接已过期，属于数据限制；正文、
  气泡 ID 与原始链接仍保留在 JSONL 和数据库中。
- 语音无法在浏览器播放：文件已在本地，可用页面中「打开语音文件」链接用本地播放器播放。
- 更多排查见 [docs/troubleshooting.md](docs/troubleshooting.md)（英文）。

## 开发与测试

```powershell
python -m unittest discover -s tests -v
```

测试在临时目录中构造真实结构的 SQLite 数据库（含真实的 `-wal` 边车文件）、应用数据
目录和 DataBackup 压缩包，然后像用户一样驱动库和命令行。测试不包含任何真实聊天内容、
真实账号 ID 或媒体文件，也不依赖固定分析路径。详见 [docs/testing.md](docs/testing.md)
与 [docs/usage.md](docs/usage.md)（英文）。

## 验证结果

真实完整备份导出的聚合可复现结果（原始行一致性、完整媒体解码、阅读器计数、编码行为，
以及无法恢复的资源说明）见 [docs/verification.md](docs/verification.md)（英文）。该文档
只记录聚合数字（不含账号 ID、聊天正文或绝对路径），如实说明 121 个不可用媒体资源为何
无法恢复；安装包构建和独立环境安装后的离线导出均已验证通过。
