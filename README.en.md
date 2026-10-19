<p align="center">
  <picture>
    <source srcset="hub-docs/images/logo-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="hub-docs/images/logo-light.svg" media="(prefers-color-scheme: light)">
    <img src="hub-docs/images/logo-light.svg" alt="xiaozhi-hub-server">
  </picture>
</p>
<p align="center">Talk to Claude Code and other LLMs on your own computer through a Xiaozhi device.</p>
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
  <a href="hub-docs/guide.md">User guide</a> · <a href="hub-docs/design.md">Design notes</a> (both in Chinese)
</p>

[![Admin console](hub-docs/images/admin.png)](hub-docs/guide.md)

---

A **local server** for Xiaozhi devices (M5Stack StackChan, ESP32 boards, and more). The device listens and speaks; the server runs on your own computer and hands what you say to the local Claude Code, or to the LLM of your choice.

```
Xiaozhi device   ⇄   xiaozhi-hub-server on your computer   ⇄   Claude Code / DeepSeek / Qwen / …
listens, speaks      recognizes, routes, synthesizes            thinks and does the work
```

> A community project compatible with the Xiaozhi protocol. Not an official Xiaozhi project.

## Features

- **Direct Claude Code by voice**: it works with the account you signed in to on this computer, for example "turn this week's notes into a checklist". For tasks that take a few minutes, the device waits with you and reports the result when it's done.
- **Switch models by name**: say "嗨 Claude" ("Hey Claude") to hand over to Claude Code; after giving other models a call name in the admin console, say "嗨 DeepSeek" to switch. Supports Claude, OpenAI, Gemini, DeepSeek, Qwen, Doubao, Zhipu GLM, Kimi, and any OpenAI-compatible API.
- **Pick up where you left off**: reconnects resume automatically; after a longer break, say "接着上次聊" ("continue where we left off") to get back to it (with Claude Code).
- **Interrupt any time**: tap the face on the screen while the device is speaking, and it stops right away.
- **Mixed Chinese and English**: the default X-ASR runs offline on your machine and gets sentences like "帮我跑一下 npm install" right; Doubao streaming ASR is also available.
- **Web admin console**: LLMs and API keys, Claude Code permissions, call names, hotwords and corrections, TTS voice, plus online devices and environment checks.

## Quick start

Before you begin: the computer and the device must be on the same LAN, and git must be installed. To use Claude Code, install it and run `claude` once in a terminal to sign in; without it, the installer uses the Claude API instead, and you fill in the API key in the admin console.

### 1. Install

**macOS / Linux**, in a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | bash
```

It installs to `~/xiaozhi-hub-server` and, at the end, asks whether to start the server at login (press Enter to accept). On macOS, missing ffmpeg and opus are installed with [Homebrew](https://brew.sh), so install Homebrew first in that case.

**Windows 10 / 11**, in PowerShell:

```powershell
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # install
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # start
```

On first start, Windows Firewall asks whether Python may access the network. Allow it on private networks, or the device cannot connect. Windows has no autostart yet; run `start.ps1` once after each boot.

To install without `curl | bash`, speed up slow downloads, or see what gets installed, read [User guide · Install](hub-docs/guide.md#1-安装) (Chinese).

### 2. Set the admin password

Open `http://127.0.0.1:16688/admin` in a browser on this computer and set an admin password on first use.

### 3. Connect the device

When setting up the device's network, enter the OTA address under "Advanced options":

```
http://<your computer's LAN IP>:16688/xiaozhi/ota/
```

For StackChan setup steps and how to talk to the device, see the [user guide](hub-docs/guide.md#3-连接设备) (Chinese).

## Update and uninstall

| | macOS / Linux | Windows |
| --- | --- | --- |
| Update | `bash ~/xiaozhi-hub-server/scripts/update.sh` | Run `git pull` in the project folder, then `install.ps1` again |
| Uninstall | `bash ~/xiaozhi-hub-server/scripts/uninstall.sh` | Run `scripts\uninstall.ps1`; see the [user guide](hub-docs/guide.md#卸载) first |

If you cloned manually, replace `~/xiaozhi-hub-server` with your own directory. For turning autostart on and off and what uninstall removes, see [User guide · Autostart, update and uninstall](hub-docs/guide.md#7-开机自启更新与卸载) (Chinese).

## Security

- Use the server only on your LAN. Do not expose it to the internet.
- By default Claude Code can only read files in the working directory `~/xiaozhi-workspace`. Once you allow writing files or running commands in the admin console, voice commands can do the same, so be careful. Commands run at the Auto level are not limited to the working directory.

## About this repository

This project only uses `main/xiaozhi-server`, `scripts/` and `hub-docs/`. The other directories are unchanged code from the upstream [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) and are not used. They are kept so that regular merges from upstream, which keep up with Xiaozhi firmware updates, produce fewer conflicts; the one-line installer does not download them.

To learn what happens from wake word to answer, read the [design notes](hub-docs/design.md) (Chinese) or the "How it works" page in the admin console.

## License

[MIT License](LICENSE). Based on [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) (MIT). Third-party credits are listed in the [user guide](hub-docs/guide.md#致谢与许可).
