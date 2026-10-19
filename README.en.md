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
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-64748b?style=flat-square" />
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

## What it does

- **Direct Claude Code by voice**: for example "list the files in my working directory" or "turn this week's notes into a checklist". It works with the account you signed in to on this computer; for tasks that take a few minutes, the device waits with you and reports the result when it's done.
- **Switch between LLMs**: say "嗨 Claude" or "嗨 DeepSeek" ("Hey Claude" / "Hey DeepSeek") to hand the conversation to that model. Each device remembers the one it used last. Supports Claude, OpenAI, Gemini, DeepSeek, Qwen, Doubao, Zhipu GLM, Kimi, and any OpenAI-compatible API.
- **Pick up where you left off**: reconnects resume automatically; after a longer break, say "接着上次聊" ("continue where we left off") to get back to it (with Claude Code).
- **Interrupt any time**: tap the face on the screen while the device is speaking, and it stops right away.
- **Mixed Chinese and English**: the default X-ASR speech recognition runs on your machine without internet and gets sentences like "帮我跑一下 npm install" right; Doubao streaming ASR is also available.
- **Web admin console**: set up LLMs and API keys, Claude Code's model and permissions, the names you call models by, hotwords and corrections for speech recognition, and the TTS voice; see online devices and environment checks.

## Quick start

Requirements:

- macOS or Linux with git; on macOS, [Homebrew](https://brew.sh) is needed if ffmpeg or opus is missing. The Windows scripts have not been tested yet, and Windows has no one-line install, autostart or update script yet;
- the computer and the device on the same LAN;
- to use Claude Code, install it first and run `claude` once in a terminal to sign in.

Run this line in a terminal. It installs to `~/xiaozhi-hub-server` and, at the end, asks whether to start the server automatically at login:

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | bash
```

If you prefer not to pipe into bash, download and install manually:

```bash
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
bash scripts/install.sh   # install
bash scripts/start.sh     # start (if autostart is not enabled)
```

To update to the latest version later: `bash ~/xiaozhi-hub-server/scripts/update.sh` (use your own directory if you cloned manually).

Once the server is running, you need two addresses:

| | Address | Use |
| --- | --- | --- |
| Admin console | `http://127.0.0.1:16688/admin` | Open it in a browser on this computer; set an admin password on first use |
| OTA address | `http://<your computer's LAN IP>:16688/xiaozhi/ota/` | Enter it under "Advanced options" when setting up the device's network |

For StackChan setup steps, how to talk to the device, and the FAQ, see the **[user guide](hub-docs/guide.md)** (Chinese).

## Documentation

- [User guide](hub-docs/guide.md) (Chinese): installation, device setup, how to talk to it, FAQ.
- [Design notes](hub-docs/design.md) (Chinese): what happens from wake word to disconnect, when the connection and the Claude Code process start and stop, what is saved, and why it is designed this way.
- The "How it works" page in the admin console: based on your current settings, what each step uses and what happens when you say what.

## Security

- Use the server only on your LAN. Do not expose it to the internet.
- By default Claude Code can only read files in the working directory `~/xiaozhi-workspace`. Once you allow writing files or running commands in the admin console, voice commands can do the same, so be careful. Commands run at the Auto level are not limited to the working directory.

## Repository layout

This project only runs `main/xiaozhi-server`, plus `scripts/` (install, start, autostart and update scripts) and `hub-docs/` (documentation). The other directories are unchanged code from the upstream [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) (management console, digital human front end, Docker deployment, and so on) and are not used. They are kept so that regular merges from upstream, which keep up with Xiaozhi firmware updates, produce fewer conflicts. The one-line installer downloads only the directories that are used.

## License

[MIT License](LICENSE). This project is based on [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) (MIT). Third-party credits are listed in the [user guide](hub-docs/guide.md#致谢与许可).
