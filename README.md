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

小智设备（M5Stack StackChan、ESP32 开发板等）的**本地服务端**。设备负责听和说；服务端运行在你自己的电脑上，把你说的话交给本机的 Claude Code，或者你选的大模型。

```
小智设备   ⇄   本机的 xiaozhi-hub-server   ⇄   Claude Code / DeepSeek / 通义千问 / …
听和说          识别、换模型、合成语音             思考和干活
```

> 兼容小智协议的社区项目，不是小智官方项目。

## 能做什么

- **对着设备指挥 Claude Code**：比如"看看工作目录里有哪些文件""把这周的笔记整理成一份清单"。它用你在本机登录的账号干活；要做几分钟的任务，设备会陪着等，做完当场说结果。
- **换着用不同的大模型**：说"嗨 Claude"就交给本机的 Claude Code；在管理后台给其他模型配好称呼后，说"嗨 DeepSeek"这样的话就换成对应的模型回答。每台设备记得上次用的是哪个。支持 Claude、OpenAI、Gemini、DeepSeek、通义千问、豆包、智谱 GLM、Kimi，以及任意 OpenAI 兼容接口。
- **接着上次聊**：断线重连后自动接上；隔得久了，说"接着上次聊"也能接回去（使用 Claude Code 时）。
- **随时打断**：设备说话时点一下屏幕上的脸，它马上停下，你接着说。
- **中英夹杂也能认对**：默认用在本机运行的 X-ASR 语音识别，不联网，"帮我跑一下 npm install"这样的话也能认准；也可以换成豆包流式语音识别。
- **网页管理后台**：设置大模型和 API Key，Claude Code 的模型和权限，叫模型用的称呼，语音识别的热词和纠错，语音合成的音色；查看在线设备和环境自检。

## 快速开始

需要：

- 电脑和设备连在同一个局域网；
- 使用 Claude Code 的话，先安装好，并在终端运行一次 `claude` 登录。没装 Claude Code 时，安装脚本会改用 Claude API，到管理后台填 API Key 就行。

### macOS / Linux

需要已安装 git（macOS 没有时运行 `xcode-select --install`）。macOS 上没有 ffmpeg、opus 时，安装脚本用 [Homebrew](https://brew.sh) 安装它们，所以这种情况下要先装好 Homebrew。

在终端运行这一行。代码会装到 `~/xiaozhi-hub-server`，安装结束时会询问是否开机自启（直接回车就是开启）：

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

以后更新到最新版本：`bash ~/xiaozhi-hub-server/scripts/update.sh`；卸载：`bash ~/xiaozhi-hub-server/scripts/uninstall.sh`，会一起取消开机自启（手动下载的，换成自己的目录）。

### Windows

需要 Windows 10 或 11，已安装 git（没有的话在 PowerShell 里运行 `winget install --id Git.Git -e`，装完重新打开 PowerShell）。

在 PowerShell 里运行：

```powershell
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # 安装
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # 启动
```

- 安装脚本会自动准备 uv、Python 3.10、依赖和语音识别模型；ffmpeg 用 winget 安装，opus 自动下载 `opus.dll`。
- 第一次启动时，Windows 防火墙会询问是否允许 Python 访问网络，请勾选"专用网络"并允许，否则设备连不上。
- Windows 目前没有开机自启，每次开机后运行一次 `start.ps1`。
- 以后更新：在项目目录里运行 `git pull`，再运行一次 `install.ps1` 补装新增的依赖。
- 卸载：先运行 `cd ~` 离开项目目录（Windows 删不掉正在使用的目录），再运行 `powershell -ExecutionPolicy Bypass -File xiaozhi-hub-server\scripts\uninstall.ps1`（项目不在用户目录下的，换成实际路径）。

服务启动后，会用到两个地址：

| | 地址 | 用途 |
| --- | --- | --- |
| 管理后台 | `http://127.0.0.1:16688/admin` | 在这台电脑的浏览器里打开，第一次使用时先设置管理密码 |
| OTA 地址 | `http://<电脑的局域网 IP>:16688/xiaozhi/ota/` | 设备配网时，填在配网页的"高级选项"里 |

StackChan 的配网步骤、怎么跟设备说话、常见问题，见 **[使用说明](hub-docs/guide.md)**。

## 文档

- [使用说明](hub-docs/guide.md)：安装、配网、对话怎么用、常见问题。
- [设计细节](hub-docs/design.md)：一句话从唤醒到断开经过了什么，连接和 Claude Code 进程什么时候启动和关闭，哪些记录会保存，以及为什么这样设计。
- 管理后台的"工作原理"页：按你当前的配置，列出每一步用的是什么，以及说什么会怎样。

## 安全提示

- 服务只应在局域网内使用，不要暴露到公网。
- Claude Code 默认只能读取工作目录 `~/xiaozhi-workspace` 里的文件。在管理后台放开写文件、执行命令后，语音指令就能修改文件、执行命令，请谨慎；其中执行命令（Auto 级别）不受工作目录限制。

## 仓库结构

本项目只运行 `main/xiaozhi-server`，另外用到 `scripts/`（安装、启动、开机自启、更新、卸载脚本）和 `hub-docs/`（文档）。其余目录是上游 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的原样代码（智控台、数字人前端、Docker 部署等），本项目不使用。保留它们是为了定期合并上游时少出冲突，好跟上小智固件的更新。一键安装只下载用到的目录。

## 许可

[MIT License](LICENSE)。本项目基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）改造，第三方组件的致谢见[使用说明](hub-docs/guide.md#致谢与许可)。
