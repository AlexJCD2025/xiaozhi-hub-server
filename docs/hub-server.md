# xiaozhi-hub-server 使用说明

xiaozhi-hub-server 是一个**兼容小智协议的社区项目**，基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（MIT）改造。它不是小智官方项目，与官方没有关联。

定位：小智设备（StackChan、ESP32 开发板等）的**本地中枢服务端**。设备负责听和说，具体交给哪个 AI 去思考和执行，由这个服务端决定。

## 与原项目的区别

| 功能 | 说明 |
| --- | --- |
| **Claude Code 接入（`claude_cli`）** | 设备可以直接调用本机已登录的 Claude Code：语音对话、读取工作目录里的文件、执行任务。每台设备对应一个常驻进程，支持打断、断线重连后接续上下文，也可以说"接着上次聊"接续更早的会话 |
| **轻量管理后台** | 网页 `/admin`，可以配置大模型、API Key、语音识别和语音合成、意图识别、Claude CLI 参数，查看环境自检结果和在线设备。不需要数据库、Java 或 Redis，不使用原项目的智控台 |
| **一键安装和启动** | 源码方式运行，不需要 Docker。`scripts/install.sh` 和 `scripts/start.sh`（macOS / Linux），`scripts/install.ps1` 和 `scripts/start.ps1`（Windows，**未实测**） |
| **默认端口** | HTTP（OTA 接口、管理后台）用 16688，WebSocket 用 16689 |
| **体验修复** | 设备说话断续（每句话开始时重新预缓冲）、OTA 地址漏写结尾斜杠时返回 404、Claude API 隐藏思考导致响应慢甚至没有回复 |

对原项目文件的改动都控制在几行以内，并用 `[xiaozhi-hub]` 注释标出，方便同步上游。

## 安装与启动

要求：macOS 或 Linux（Windows 脚本未实测），能访问网络。安装脚本会自动安装 uv、Python 3.10、opus、ffmpeg 和全部依赖，并下载约 900MB 的语音识别模型。

```bash
bash scripts/install.sh
bash scripts/start.sh
```

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` | 使用国内 Python 包镜像 |
| `ASSUME_YES=1` | 非交互安装，所有询问都按默认回答 |
| `SKIP_DOCTOR=1` | 启动时跳过自检 |

**安装说明**：

- 当前平台装不上的可选依赖（比如 macOS 上的 vosk）会被自动跳过，安装结束后会运行自检。
- 检测到本机已安装 Claude Code 时，默认使用 Claude CLI 作为大模型；否则使用智谱 glm-4-flash，需要到管理后台填写 API Key。

## 首次配置

1. **设置管理密码**：在运行服务的电脑上打开 `http://127.0.0.1:16688/admin`，设置管理密码（至少 6 位）。为了安全，首次设置只能在本机操作。
2. **配置模块**：在管理后台选择大模型、填写 API Key、调整语音识别和合成的设置。保存后，设备重新连接时生效。
3. **准备 Claude Code**：如果使用 Claude CLI，要先在终端运行一次 `claude` 完成登录。Claude Code 只能访问工作目录（默认 `~/xiaozhi-workspace`），默认只开放只读工具（Read、Grep、Glob）。

配置保存在 `main/xiaozhi-server/data/.config.yaml`，这个文件不会提交到 Git。API Key 在这个文件里是明文，管理密码只保存哈希值。

## 连接设备

设备配网时，在"高级选项"里填写 OTA 地址，**注意结尾要带斜杠**（不带也能用）：

```
http://<电脑的局域网 IP>:16688/xiaozhi/ota/
```

| 设备 | 方法 |
| --- | --- |
| 官方小智固件 1.6.1 及以上的开发板 | 进入配网模式，在配网页的"高级选项"里填写 OTA 地址 |
| M5Stack StackChan（官方固件，实测 1.5.1） | 在 App 中解绑设备 → 设置 → 系统 → Factory Reset → 开机引导页选 Skip → 进入 AI Agent → 手机连接设备热点打开配网页 → 在"高级选项"里填写 OTA 地址 → 填写 2.4GHz Wi-Fi |

StackChan 的几点说明：

- 唤醒词固定为 "Hi, StackChan"，不刷固件改不了。
- 设备处于 auto 监听模式时，说话过程中只能用唤醒词打断。
- 待机时会随机摇头，可以在"设置 → AI.Agent → General → Idle movement"里关闭。

## 常见问题

- **设备显示"检查新版本失败"**：确认电脑和设备在同一个局域网，OTA 地址的 IP、端口正确。用手机浏览器打开 OTA 地址，能看到"OTA接口运行正常"就说明网络是通的。
- **电脑 IP 变了，设备连不上**：在路由器里给电脑设置固定 IP。
- **Windows 防火墙**：需要放行 TCP 16688 和 16689 端口（仅限专用网络）。
- **设备说话断续**：调大 `data/.config.yaml` 里的 `tts_audio_prebuffer_count`（默认 15 帧，约 0.9 秒），合理范围是 10~25。
- **Claude CLI 没有反应**：先在管理后台的状态页确认 Claude Code 能找到并已登录，然后查看 `main/xiaozhi-server/tmp/server.log`，里面记录了工具调用、限流状态和每轮的结果。

## 安全提示

- 服务**只应在局域网内使用**，不要暴露到公网。
- 管理后台必须设置密码；API Key 不要提交到 Git。
- Claude CLI 默认只开放只读工具。放开 Bash、Write 等工具后，语音指令就能在电脑上执行命令、修改文件，请谨慎。

## 致谢与许可

- **原项目**：[xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)，MIT License，Copyright (c) 2025 xinnan-tech。本项目保留了原项目的 [LICENSE](../LICENSE)。
- **设备端协议与固件**：[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)。
- **语音识别模型 SenseVoiceSmall**：来自 [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（阿里巴巴通义实验室），按 [FunASR 模型开源许可协议](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) 使用。模型由安装脚本从 ModelScope 下载，本仓库不包含模型权重。
- **语音活动检测 silero-vad**：[snakers4/silero-vad](https://github.com/snakers4/silero-vad)，MIT License。
- **Claude Code 接入**：使用 Anthropic 的 [claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/)。需要用户自行安装 Claude Code 并登录。
- **测试页里的 Live2D 核心库**：`main/digital-human/js/live2d/live2dcubismcore.min.js`，属于 Live2D Inc. 的专有许可，随原项目一起提供，本项目没有修改。
