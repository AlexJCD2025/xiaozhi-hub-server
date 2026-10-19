<p align="center">
  <picture>
    <source srcset="hub-docs/images/logo-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="hub-docs/images/logo-light.svg" media="(prefers-color-scheme: light)">
    <img src="hub-docs/images/logo-light.svg" alt="xiaozhi-hub-server">
  </picture>
</p>
<p align="center">让小智设备用语音指挥你电脑上的 Claude Code 和大模型。</p>
<p align="center">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/justhalfbit/xiaozhi-hub-server?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10-3776ab?style=flat-square" />
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-64748b?style=flat-square" />
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

小智设备（M5Stack StackChan、ESP32 开发板等）的**本地中枢服务端**：设备负责听和说，服务端运行在你自己的电脑上，把语音交给本机的 Claude Code 或你选择的大模型来思考和执行。

> 兼容小智协议的社区项目，不是小智官方项目。

> 想知道每一处**为什么这样做**——一次对话从唤醒到断开经过了什么、设备和服务端的连接什么时候断、Claude Code 进程什么时候启动和关闭、什么落盘什么不落盘、为什么 API 模型不能接续——见 [设计细节](hub-docs/design.md)。

## 功能

- **语音接入本机 Claude Code**：用说话的方式和本机已登录的 Claude Code 对话，让它读取工作目录里的文件、执行任务。
  - 每台设备对应一个常驻进程，回复快。
  - 设备说话时可以打断（StackChan 点一下屏幕上的脸，或喊唤醒词），打断后能接着聊。
  - 支持上下文接续：断线重连后自动接上，也可以说"接着上次聊"接续更早的对话。→ [design.md §6](hub-docs/design.md#6-会话接续只对-claude-code)
- **也能用其他大模型**：Claude、OpenAI、Gemini、DeepSeek、通义千问、豆包、智谱 GLM、Kimi，以及任意 OpenAI 兼容接口。
- **呼名切换模型**：唤醒后说"嗨 + 称呼"（如"嗨 Claude""嗨 DeepSeek"），就换成对应的模型回答，每台设备记住上次用的模型；每个模型只记得跟自己的对话。称呼可以在管理后台对着设备录入，按你的发音来认。→ [design.md §5](hub-docs/design.md#5-呼名与多模型路由)
- **本地语音识别**：X-ASR 中英混合模型在本机运行，不需要联网，"GitHub"、"npm install" 这类中英夹杂的话也能认对。语音合成使用 EdgeTTS，可选多种音色。
- **网页管理后台**：
  - 设置大模型和 API Key；Claude Code 可选模型、思考级别、权限级别。
  - 给每个模型配呼名的称呼：手动添加，或者对着设备说几遍自动录入。
  - 语音识别：在本地 X-ASR 和豆包流式语音识别之间切换（豆包填 API Key 即可，可先测试连接）；设置说完停顿多久算一句、热词（两种识别共用一份），以及识别后纠错（比如把 “cloud code” 改成 “Claude Code”）；看最近识别的句子，认错的词一键加进热词或纠错表。
  - 选择语音合成音色并试听，设置意图识别。
  - 查看环境自检、在线设备，给设备发表情测试。
  - "工作原理"页展示一句话从设备到播放的完整流程和发给大模型的提示词。
- **一键安装、自启和更新**：macOS / Linux 一条命令安装，自动准备 Python 环境、依赖和语音识别模型；可选开机自启，一条命令更新到最新版本。

## 快速开始

需要：macOS 或 Linux（Windows 脚本尚未实测），电脑和设备在同一个局域网。使用 Claude Code 的话，先安装并在终端运行一次 `claude` 登录。

在终端运行这一行，代码会下载到 `~/xiaozhi-hub-server`，然后自动安装。安装结束时会询问是否开机自启：

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | bash
```

不想用 `curl | bash` 的话，也可以手动下载再安装：

```bash
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
bash scripts/install.sh   # 安装
bash scripts/start.sh     # 启动（没有开启开机自启时）
```

以后更新到最新版本：`bash ~/xiaozhi-hub-server/scripts/update.sh`（手动下载的，换成自己的目录）。

启动后终端会显示两个地址：

- **管理后台**：在本机浏览器打开 `http://127.0.0.1:16688/admin`，首次使用时设置管理密码。
- **OTA 地址**：`http://<电脑的局域网 IP>:16688/xiaozhi/ota/`，设备配网时填写。

## 连接设备

设备配网时，在配网页的"高级选项"里填写上面的 OTA 地址。StackChan 的具体步骤、常见问题和更多配置，见 **[使用说明](hub-docs/guide.md)**。

## 安全提示

- 服务只应在局域网内使用，不要暴露到公网。
- Claude Code 默认只能访问工作目录 `~/xiaozhi-workspace`，只开放只读工具。放开写入、执行命令等权限后，语音指令就能修改文件、执行命令，请谨慎；其中执行命令（Auto 级别）不受工作目录限制。

## 仓库里哪些目录用到了

本项目只运行 `main/xiaozhi-server`。小智固件更新时，上游服务端会跟着适配，本项目定期合并上游来兼容新固件。为了让合并少出冲突，上游的其他代码原样保留，但没有用到，不需要关心：

| 目录 / 文件 | 说明 |
| --- | --- |
| `main/xiaozhi-server` | **用到**：服务端本体，本项目的改动都在这里 |
| `scripts/` | **用到**：本项目的安装、启动、开机自启和更新脚本 |
| `hub-docs/`、`README.en.md` | **用到**：本项目的文档（使用说明、设计细节、README 图片）和英文 README |
| `main/manager-api`、`main/manager-web`、`main/manager-mobile` | 未用到：上游的智控台，本项目用自带的网页管理后台代替 |
| `main/digital-human` | 未用到：上游的数字人前端 |
| `main/README.md`、`main/README_en.md`、`docs/` | 未用到：上游的说明文档，部分内容不适用于本项目 |
| `Dockerfile-*`、`docker-setup.sh`、`.dockerignore` | 未用到：上游的 Docker 部署 |
| `Makefile` | 未用到：上游的测试入口（含智控台的 Java、前端测试） |

## 许可

[MIT License](LICENSE)。本项目基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）改造，第三方组件的致谢见[使用说明](hub-docs/guide.md#致谢与许可)。
