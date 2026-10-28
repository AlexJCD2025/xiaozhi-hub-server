<p align="center">
  <picture>
    <source srcset="hub-docs/images/logo-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="hub-docs/images/logo-light.svg" media="(prefers-color-scheme: light)">
    <img src="hub-docs/images/logo-light.svg" alt="xiaozhi-hub-server">
  </picture>
</p>
<p align="center">让小智设备成为 AI 的语音中枢：叫谁的名字，就交给谁干活。</p>
<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/justhalfbit/xiaozhi-hub-server?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10-3776ab?style=flat-square" />
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-64748b?style=flat-square" />
  <a href="https://github.com/justhalfbit/xiaozhi-hub-server/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/justhalfbit/xiaozhi-hub-server?style=flat-square" /></a>
  <a href="https://x.com/justhalfbit"><img alt="X (Twitter)" src="https://img.shields.io/badge/X-%40justhalfbit-000000?style=flat-square&logo=x&logoColor=white" /></a>
</p>
<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.en.md">English</a>
</p>
<p align="center">
  <a href="hub-docs/guide.md">使用说明</a> · <a href="hub-docs/design.md">设计细节</a>
</p>

[![管理后台](hub-docs/images/admin.png)](hub-docs/guide.md)

---

小智设备（M5Stack StackChan、ESP32 开发板等）的**中枢网关**。设备负责听和说；网关负责识别和合成语音，再按你叫的名字，把话交给对应的 agent 或大模型去干活。

- **部署在自己的电脑上**：可以直接让本机的 Claude Code 或 Codex CLI 干活，也可以用各家大模型的 API。
- **部署在云端服务器上**：用不了本机的 Claude Code，改用大模型的 API。

```
小智设备   ⇄   xiaozhi-hub-server        ⇄   Claude Code / Codex CLI / 大模型 API / …
听和说          识别、按称呼分派、合成语音       思考和干活
```

> 兼容小智协议的社区项目，不是小智官方项目。

## 功能

**一个设备，多个 AI**

- **叫名字换 AI**：说"嗨 Claude"交给 Claude Code，说"嗨 Codex"交给本机 Codex CLI，说"嗨 DeepSeek"交给 DeepSeek，称呼在管理后台配。每个 AI 各聊各的，每台设备记得上次用的是哪个。
- **Claude Code**：用你在本机登录的账号干活，比如"把这周的笔记整理成一份清单"。
- **Codex CLI**：在 Mac 上用已经登录 ChatGPT 的 Codex CLI 回答。只读查看直接说；删除、写入、部署或发消息之前，要在设备上点一下确认。用法见下面的 [本机 Codex CLI](#本机-codex-cli)。
- **儿童模式**：给孩子用的双语聊天。只连接一个 OpenAI 兼容接口，不能碰 Codex、命令、文件和其他 agent。用法见 [儿童模式](#儿童模式)。
- **大模型 API**：Claude、OpenAI、Gemini、DeepSeek、通义千问、豆包、智谱 GLM、Kimi，以及任意 OpenAI 兼容接口。
- **Grok Bot**：给 xAI 的云端 AI 同事派活。在后台用 Cursor 账号登录、勾选 Bot、配上称呼，说"嗨 孙悟空，帮我把这周的 X 热点整理一下"，它的回复会念出来。长任务可以打断不等，它在云端做完后设备会主动汇报。走的是 App 用的内部接口，不是官方 API，App 升级后可能失效。

**长任务也能陪着做**

- **陪着等**：让 Claude Code 做几分钟的任务，设备一直陪着，隔一会儿报一下进度，做完当场说结果。
- **随时打断**：设备说话时按一下设备上的按键（StackChan 是点一下屏幕上的脸），或者喊唤醒词，它马上停下。
- **接着上次聊**：断线重连自动接上；隔得久了，说"接着上次聊"也能接回去（Claude Code、Codex CLI）。Codex 正在做的事被按键打断后会转到后台，做完再念；整句说"停止"或"取消"才会把它杀掉。

**听得准**

- 中英夹杂也能认对：默认用离线运行的 X-ASR，"帮我跑一下 npm install"也能认准；认错的词可以加进热词和纠错表。也可以换成豆包流式识别。

**管理后台**

- 在网页上配置大模型和 API Key、Claude Code 的权限、称呼、插件、热词纠错和音色，查看在线设备和环境自检。

## 快速开始

开始前：电脑和设备连在同一个局域网，电脑上装好 git。要用 Claude Code 的话，先装好它并在终端运行一次 `claude` 登录；没装时会改用 Claude API，到管理后台填 API Key 就行。

### 1. 安装

**macOS / Linux**，在终端运行：

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | bash
```

代码装到 `~/xiaozhi-hub-server`，结束时询问是否开机自启（直接回车就是开启）。macOS 缺 ffmpeg、opus 时会用 [Homebrew](https://brew.sh) 安装，这种情况要先装好 Homebrew。

**Windows 10 / 11**，在 PowerShell 里运行：

```powershell
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # 安装
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # 启动
```

第一次启动时 Windows 防火墙会询问，请勾选"专用网络"并允许，否则设备连不上。Windows 暂时没有开机自启，每次开机后运行一次 `start.ps1`。

不想用 `curl | bash`、下载太慢或者想看装了什么，见[使用说明 · 安装](hub-docs/guide.md#1-安装)。

### 2. 设置管理密码

在这台电脑的浏览器里打开 `http://127.0.0.1:16688/admin`，第一次使用时先设置管理密码。

### 3. 连接设备

设备配网时，在配网页的"高级选项"里填 OTA 地址：

```
http://<电脑的局域网 IP>:16688/xiaozhi/ota/
```

StackChan 的配网步骤、怎么跟设备说话，见[使用说明](hub-docs/guide.md#3-连接设备)。

**部署到云端服务器**：安装方法一样，另外要放行端口、在配置里写明公网地址，见[使用说明 · 部署到云端服务器](hub-docs/guide.md#部署到云端服务器)。

## 更新与卸载

| | macOS / Linux | Windows |
| --- | --- | --- |
| 更新 | `bash ~/xiaozhi-hub-server/scripts/update.sh` | 在项目目录里 `git pull`，再运行一次 `install.ps1` |
| 卸载 | `bash ~/xiaozhi-hub-server/scripts/uninstall.sh` | 运行 `scripts\uninstall.ps1`，注意事项见[使用说明](hub-docs/guide.md#卸载) |

手动下载的，把 `~/xiaozhi-hub-server` 换成自己的目录。开机自启的开关、卸载会删什么，见[使用说明 · 开机自启、更新与卸载](hub-docs/guide.md#7-开机自启更新与卸载)。

## 本机 Codex CLI

给 Mac 上的 M5Stack StackChan 接本机 Codex CLI（0.153.x，模型 `gpt-5.6-sol`）。设备仍走小智协议；Codex 跑在这台 Mac 上，用你已经登录的 ChatGPT 账号，不另填 API Key。

### Mac 上先准备好

1. 安装 Codex CLI，并确认它在 `/opt/homebrew/bin/codex`（Homebrew 的常见位置）。终端里运行 `codex --version`，应能看到 0.153 这一支。
2. 在终端运行 `codex login`，用 ChatGPT 账号登录。没登录时，设备会说「Codex 还没登录或者登录过期了」。
3. 按[快速开始](#快速开始)把本仓库装好并启动。管理后台是 `http://127.0.0.1:16688/admin`。

### 配置

改本机的 `data/.config.yaml`（安装后在 `~/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml`，不会进 Git）。可以整段照抄 `scripts/config.template.yaml` 里的 `CodexCLI`。

让设备一连上就用 Codex：

```yaml
selected_module:
  LLM: CodexCLI
  Intent: nointent

LLM:
  CodexCLI:
    type: codex_cli
    cli_path: /opt/homebrew/bin/codex
    model: gpt-5.6-sol
    workdir: ~/xiaozhi-workspace
```

`Intent` 必须是 `nointent`。不改默认模型也可以：对设备说「嗨 Codex」。称呼还能在后台「呼名」页改。`cli_path` 留空时会依次找 PATH、`/opt/homebrew/bin/codex`、`/usr/local/bin/codex`。

改完配置后重启服务，或等设备断开再连一次。

### 把 StackChan 指过来

电脑和 StackChan 在同一个局域网。配网页的「高级选项」里，OTA 地址填：

```
http://<Mac 的局域网 IP>:16688/xiaozhi/ota/
```

手机浏览器打开这个地址，能看到 OTA 接口正常，就说明地址对了。

### 确认怎么按

每一轮先在只读沙箱里看（`codex exec --json --sandbox read-only`）。`rg`、不带 `-i` 的 `sed`、`cat`、`ls`、`git status` 这类查看不会要求确认，也不会因为命令输出里出现 deleted 就误报。

计划里要删除、付款、部署、发消息或邮件，或者要改文件，设备会先说一句，例如「要删除。点一下屏幕确认，或者说确认。」**不会自动同意。**

- 设备说话时不收音。确认请在它念这句话时**点一下 StackChan 屏幕上的脸**（开发板是 BOOT 键）。官方固件这时会发 `{"type":"abort"}`，服务端把它当成同意，然后另起一个 `workspace-write` 进程，只恢复计划阶段记下的线程号。
- 这句话播完之后，也可以说「确认」「确定」「同意」「执行」或「批准」。
- 说「取消」「停止」「不用了」，或超过 `confirm_timeout_seconds`（默认 45 秒）没人理，都是取消。填 `0` 表示一直等到你取消，仍然不会自动同意。
- 喊唤醒词打断**不算**确认。官方固件的唤醒打断带 `reason=wake_word_detected`。

不需要改 StackChan 固件，只要点屏幕或按键时设备会发上面这条 abort。固件如果不发这条消息，就等它说完再说「确认」。

### 说话时的节奏

- 大约 2 秒还没结果，先说「好的，我看一下」。
- 比较久的话，大约在第 30、60、120 秒报一次进度。命令还在跑的时候不算卡住。
- 点屏幕或喊唤醒词：这一轮转到后台，做完再念。整句说「停止」或「取消」才会杀掉。
- 说「继续」或「接着上次聊」会接上上次的线程。
- 念出来的是 Codex 最后一句的短摘要，不是整段 Markdown。

### 已知限制

- 执行阶段只用 `workspace-write`，不会加 `--full-auto`、`--yolo`，也不会升到 `danger-full-access`。部署、付款、发信如果超出这个沙箱，会失败，服务端不会为了做成而放开权限。
- 读写在 `workdir`（默认 `~/xiaozhi-workspace`）里。
- 后台做完的那句汇报只在内存里，服务重启就没了。
- 管理后台没有单独的 Codex 设置页，参数写在 `data/.config.yaml`。状态页只检查能不能找到 `codex`，不检查有没有登录。
- 日志会藏起密钥，也不会把整段提示词写进去。

## 儿童模式

给大约十二岁的孩子一台只聊天的 StackChan。它按大约十三到十五岁的口气说话，中文英文都会接，回答适合朗读。这一档和 Codex 分开：没有命令行，没有文件，没有账号，也不能呼名切到别的 agent。可以单独跑在家里的 Mac 上，也可以跑在一台 Ubuntu 服务器上。

你需要自己准备一个 OpenAI 兼容接口的密钥。可以是 xAI 的 Grok API（`https://api.x.ai/v1`），也可以是国内的兼容接口。模型名以服务商控制台为准。

### 要设置的环境变量

不要把密钥写进 Git。启动服务之前：

```bash
export KID_MODE_API_KEY="你的密钥"
export KID_MODE_BASE_URL="https://api.x.ai/v1"   # 或国内兼容接口的地址，末尾一般带 /v1
export KID_MODE_MODEL="控制台里的模型名"
```

Ubuntu 上如果用开机自启，把这三行放进服务的环境，而不是写进 `data/.config.yaml`。

### 配置

在 `data/.config.yaml` 里让这台设备一连上就用儿童模式（可以照抄 `scripts/config.template.yaml` 里的 `KidMode`）：

```yaml
selected_module:
  LLM: KidMode
  Intent: nointent

LLM:
  KidMode:
    type: kid_mode
    base_url: ""          # 空着就用上面的环境变量
    model_name: ""
    api_key: ""
    isolate: true         # 孩子说「嗨 Codex」也不会切走
    safety_enabled: true
    max_turns_per_day: 0  # 0 表示不限制。想限制就填每天最多几轮
    quiet_hours: ""       # 例如 21:30-07:00，时区固定是 Asia/Shanghai。留空表示不启用
    conversation_log: true
```

`Intent` 必须是 `nointent`。后台如果选了「用插件」，保存时会被拒绝。默认不限制次数，也不设安静时段；这两项要自己打开。

对话记在 `main/xiaozhi-server/data/kid_mode/chat.jsonl`，文件变大后会滚动留下几份。里面会藏起密码和密钥。不想留记录就把 `conversation_log` 改成 `false`。

### Mac 上怎么跑

1. 按[快速开始](#快速开始)装好并启动服务。
2. 设好上面三个环境变量，写好 `KidMode`，重启服务。
3. 电脑和 StackChan 在同一个局域网。配网页的「高级选项」里，OTA 地址填：

```
http://<Mac 的局域网 IP>:16688/xiaozhi/ota/
```

设备随后连的语音地址由 OTA 下发，默认是这台电脑的 `16689` 端口。管理后台是 `http://127.0.0.1:16688/admin`，第一次打开先设密码。状态页会检查接口、模型和密钥有没有填，不会把密钥显示出来。

### Ubuntu 服务器上怎么跑

安装方法和 Mac 一样。另外：

1. 安全组或防火墙放行 `16688`（OTA 和管理后台）和 `16689`（语音）。
2. 在 `data/.config.yaml` 的 `server:` 下写上公网地址，否则设备拿到的是内网 IP：

```yaml
server:
  websocket: ws://<服务器的公网 IP 或域名>:16689/xiaozhi/v1/
```

3. 配网时 OTA 地址填 `http://<公网 IP>:16688/xiaozhi/ota/`。
4. 马上设置管理后台密码。儿童模式本身不能操作服务器，但后台密码仍然要设。

### 它会拦住什么

人设里写了规则，发送前和收到回复后还会再查一遍。拦住的包括：色情、血腥暴力、自伤（会温和地请孩子告诉爸爸妈妈，不会讲方法）、烟酒毒品、赌博、仇恨、危险做法，以及地址、学校名称、密码这类隐私。日常说法不会拦，例如「学校作业」「杀死细菌」。

`moderation: true` 时，会再试一次接口上的审核端点。对方没有这个接口就只用关键词，聊天不会因此停掉。家长还可以加 `extra_block_phrases`。

不需要改 StackChan 固件。孩子直接说话就行。

## 安全提示

- 部署在自己的电脑上时，只在局域网内使用，不要把端口暴露到公网。
- 部署到云端服务器时，设备连接和管理后台都在公网上，请用防火墙限制访问；服务启动后尽快打开管理后台，设置足够强的管理密码。
- Claude Code 默认只能读取工作目录 `~/xiaozhi-workspace` 里的文件。在管理后台放开写文件、执行命令后，语音指令就能修改文件、执行命令，请谨慎；其中执行命令（Auto 级别）不受工作目录限制。
- Codex CLI 计划阶段是只读沙箱。写入、删除、部署或发消息必须在设备上明确确认，确认后也只使用 `workspace-write`，不会绕过沙箱。
- 儿童模式只连接一个聊天接口，不能使用 Codex、命令、文件或其他 agent。密钥放在环境变量里，不要写进仓库。

## 关于本仓库

本项目只用到 `main/xiaozhi-server`、`scripts/` 和 `hub-docs/`。其余目录是上游 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的原样代码，本项目不使用，保留它们是为了定期合并上游时少出冲突，好跟上小智固件的更新；一键安装不会下载这些目录。

想了解一句话从唤醒到回答经过了什么，看[设计细节](hub-docs/design.md)，或者管理后台的"工作原理"页。

## 许可

[MIT License](LICENSE)。基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）改造，第三方组件的致谢见[使用说明](hub-docs/guide.md#致谢与许可)。
