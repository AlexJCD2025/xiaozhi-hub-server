# xiaozhi-hub-server

小智设备（M5Stack StackChan、ESP32 开发板等）的**本地中枢服务端**：设备负责听和说，服务端运行在你自己的电脑上，把语音交给本机的 Claude Code 或你选择的大模型来思考和执行。

> 兼容小智协议的社区项目，不是小智官方项目。

## 功能

- **语音接入本机 Claude Code**：用说话的方式和本机已登录的 Claude Code 对话，让它读取工作目录里的文件、执行任务。
  - 每台设备对应一个常驻进程，回复快。
  - 设备说话时可以打断（StackChan 点一下屏幕上的脸，或喊唤醒词），打断后能接着聊。
  - 支持上下文接续：断线重连后自动接上，也可以说"接着上次聊"接续更早的对话。
- **也能用其他大模型**：Claude、OpenAI、Gemini、DeepSeek、通义千问、豆包、智谱 GLM、Kimi，以及任意 OpenAI 兼容接口。
- **呼名切换模型**：唤醒后说"嗨 + 称呼"（如"嗨 Claude""嗨 DeepSeek"），就换成对应的模型回答，每台设备记住上次用的模型；每个模型只记得跟自己的对话。称呼可以在管理后台对着设备录入，按你的发音来认。
- **本地语音识别**：SenseVoice 模型在本机运行，不需要联网。语音合成使用 EdgeTTS，可选多种音色。
- **网页管理后台**：
  - 设置大模型和 API Key；Claude Code 可选模型、思考级别、权限级别。
  - 给每个模型配呼名的称呼：手动添加，或者对着设备说几遍自动录入。
  - 选择语音合成音色并试听，设置意图识别。
  - 查看环境自检、在线设备，给设备发表情测试。
  - "工作原理"页展示一句话从设备到播放的完整流程和发给大模型的提示词。
- **一键安装和启动**：macOS / Linux 一条命令安装，自动准备 Python 环境、依赖和语音识别模型。

## 快速开始

需要：macOS 或 Linux（Windows 脚本尚未实测），电脑和设备在同一个局域网。使用 Claude Code 的话，先安装并在终端运行一次 `claude` 登录。

```bash
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
bash scripts/install.sh   # 安装
bash scripts/start.sh     # 启动
```

启动后终端会显示两个地址：

- **管理后台**：在本机浏览器打开 `http://127.0.0.1:16688/admin`，首次使用时设置管理密码。
- **OTA 地址**：`http://<电脑的局域网 IP>:16688/xiaozhi/ota/`，设备配网时填写。

## 连接设备

设备配网时，在配网页的"高级选项"里填写上面的 OTA 地址。StackChan 的具体步骤、常见问题和更多配置，见 **[使用说明](docs/hub-server.md)**。

## 安全提示

- 服务只应在局域网内使用，不要暴露到公网。
- Claude Code 默认只能访问工作目录 `~/xiaozhi-workspace`，只开放只读工具。放开写入、执行命令等权限后，语音指令就能修改文件、执行命令，请谨慎。

## 许可

[MIT License](LICENSE)。本项目基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）改造，第三方组件的致谢见[使用说明](docs/hub-server.md#致谢与许可)。
