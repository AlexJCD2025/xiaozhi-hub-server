# xiaozhi-hub-server 使用说明

xiaozhi-hub-server 是小智设备（StackChan、ESP32 开发板等）的**本地中枢服务端**：设备负责听和说，服务端运行在你自己的电脑上，把语音交给本机的 Claude Code 或你选择的大模型来思考和执行。

它是兼容小智协议的社区项目，不是小智官方项目，与官方没有关联。

## 安装与启动

要求：macOS 或 Linux（Windows 脚本 `scripts/install.ps1`、`scripts/start.ps1` 尚未实测），能访问网络。

```bash
bash scripts/install.sh
bash scripts/start.sh
```

安装脚本会准备好运行需要的一切：

- uv（Python 包管理工具）、Python 3.10、opus 和 ffmpeg：电脑上已有就直接用，缺少时先询问再安装；
- Python 依赖：全部装在项目自己的虚拟环境 `main/xiaozhi-server/.venv` 里，约 550MB，不影响系统里的 Python；
- 语音识别模型：SenseVoice（sherpa-onnx int8 版），约 240MB；
- 配置文件 `main/xiaozhi-server/data/.config.yaml`：检测到本机已安装 Claude Code 时，默认使用 Claude Code 作为大模型；否则使用智谱 glm-4-flash，需要到管理后台填写 API Key。

安装结束和每次启动时都会运行自检，并显示 OTA 地址和管理后台地址。运行内存约 0.9GB。

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` | 使用国内 Python 包镜像 |
| `ASSUME_YES=1` | 非交互安装，所有询问都按默认回答 |
| `SKIP_DOCTOR=1` | 启动时跳过自检 |

## 首次配置

1. **设置管理密码**：在运行服务的电脑上打开 `http://127.0.0.1:16688/admin`，设置管理密码（至少 6 位）。为了安全，首次设置只能在本机操作。
2. **配置大模型**：在管理后台选择大模型、填写 API Key，按需调整语音合成音色和意图识别。保存后，设备重新连接时生效。
3. **准备 Claude Code**：使用 Claude Code 时，要先在终端运行一次 `claude` 完成登录。Claude Code 只能访问工作目录（默认 `~/xiaozhi-workspace`），默认只开放只读工具（Read、Grep、Glob）。模型、思考级别和权限级别可以在管理后台调整。

配置保存在 `main/xiaozhi-server/data/.config.yaml`，这个文件不会提交到 Git。API Key 在这个文件里是明文，管理密码只保存哈希值。

## 连接设备

设备配网时，在"高级选项"里填写 OTA 地址：

```
http://<电脑的局域网 IP>:16688/xiaozhi/ota/
```

| 设备 | 方法 |
| --- | --- |
| 官方小智固件 1.6.1 及以上的开发板 | 进入配网模式，在配网页的"高级选项"里填写 OTA 地址 |
| M5Stack StackChan（官方固件，实测 1.5.1） | 在 App 中解绑设备 → 设置 → 系统 → Factory Reset → 开机引导页选 Skip → 进入 AI Agent → 手机连接设备热点打开配网页 → 在"高级选项"里填写 OTA 地址 → 填写 2.4GHz Wi-Fi |

设备同一时间只连一台电脑。换一台电脑运行服务时，要重新配网，填写新电脑的 OTA 地址。

StackChan 的几点说明：

- 唤醒词固定为 "Hi, StackChan"。
- 待机时会随机摇头，可以在"设置 → AI.Agent → General → Idle movement"里关闭。

## 对话怎么用

**唤醒**：说"Hi, StackChan"，设备回一句问候后开始听你说话；也可以点一下屏幕，设备直接开始听（没有问候）。

**呼名**：唤醒后，句子开头说"嗨 + 称呼"（或 "Hi"、"Hey" + 称呼），换成对应的模型回答，比如"嗨 Claude"交给本机 Claude Code，"嗨 DeepSeek"交给 DeepSeek API。
- 称呼在管理后台的"呼名"页给每个模型分别设置，只能选已经配置好的模型（Claude Code，或在"大模型"页填好 Key 的厂商）。默认只有 Claude。
- 叫过一次后，这台设备之后直接说话都交给这个模型，直到叫别的称呼；断线重连、服务重启后也记得。设备第一次连上时用后台选的默认模型。
- 各聊各的：每个模型只记得跟自己的对话，切换时不带上下文过去。切到别的模型后，Claude Code 进程不会马上关，空闲超时后才关，方便切回来。
- 只说"嗨 + 称呼"时，服务端切换模型后直接回一声"我在"，不会启动 Claude Code。回复可以在后台改，中间的标点会去掉，一次合成播完。
- 称呼后面可以直接接内容，比如"嗨 Claude，看看工作目录里有什么"，称呼会在交给模型前去掉。
- "嗨 Claude Code 怎么安装"这类称呼后面紧跟英文单词的句子，会当作普通问题原样交给当前模型。
- 语音识别常把 Claude 识别成 "cloud"，这些常见的识别结果都能认出来。
- 你的发音如果总被识别成别的写法，可以到管理后台的"呼名"页，在对应模型下点"用设备录入"，唤醒设备后说 3 遍"嗨 + 称呼"，把识别到的写法勾选加入称呼并保存，马上生效。录入期间设备听到的话不会交给模型。

**长任务**：让 Claude 做要几分钟的事（比如整理一批文件）时，设备会陪着等，最长 10 分钟，每 30 秒没开口就说一句"还在处理，请稍等"，做完当场播报结果。这段时间不能插话，要停就点屏幕打断。超过时间被停下、或者你打断之后，说"继续"它会接着做。时长和提示语在管理后台 Claude Code 卡片的"高级设置"里改。

**打断**：设备说话时不收音，不能直接插话。要打断，点一下屏幕上的脸（最可靠），或者喊唤醒词（音量大时容易听不到）。打断后 Claude 会停下当前回答，下一句话照常接着聊。

**接续上次的对话**（使用 Claude Code 时）：
- 两小时内再次唤醒，会自动接上上次的对话。
- 时间更久的，说"接着上次聊"（或"继续刚才的话题"等）也能接上；"嗨 Claude，接着上次聊"同样可以。
- 对话记录按设备、按电脑保存。
- 使用其他大模型（API）时，对话只在本次连接内有效（同一次连接里切走再切回来还记得），设备断开后就清空，不能接续。

连接什么时候断开、Claude Code 进程什么时候启动和关闭、哪些记录能跨重启保留，见 [设计细节](design.md)。

## 常见问题

- **设备显示"检查新版本失败"**：确认电脑和设备在同一个局域网，OTA 地址的 IP、端口正确。用手机浏览器打开 OTA 地址，能看到"OTA接口运行正常"就说明网络是通的。
- **电脑 IP 变了，设备连不上**：在路由器里给电脑设置固定 IP。
- **Windows 防火墙**：需要放行 TCP 16688 和 16689 端口（仅限专用网络）。
- **设备说话断续**：调大 `data/.config.yaml` 里的 `tts_audio_prebuffer_count`（默认 15 帧，约 0.9 秒），合理范围是 10~25。
- **Claude Code 没有反应**：先在管理后台的状态页确认能找到 Claude Code（状态页只检查是否安装，不检查是否登录；未登录时请在终端运行一次 `claude` 登录），然后查看 `main/xiaozhi-server/tmp/server.log`，里面记录了工具调用、限流状态和每轮的结果。
- **自检提示"模块依赖"缺包**：重新运行一次安装脚本补装依赖。

## 安全提示

- 服务**只应在局域网内使用**，不要暴露到公网。
- 管理后台必须设置密码；API Key 不要提交到 Git。
- Claude Code 默认只开放只读工具。放开 Bash、Write 等工具后，语音指令就能在电脑上执行命令、修改文件，请谨慎。

## 致谢与许可

本项目使用 [MIT License](../LICENSE)。

- **原项目**：基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 改造，MIT License，Copyright (c) 2025 xinnan-tech。
- **设备端协议与固件**：[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)。
- **语音识别模型 SenseVoiceSmall**：来自 [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（阿里巴巴通义实验室），按 [FunASR 模型开源许可协议](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) 使用。使用的 int8 版由 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 转换（Apache-2.0），从 ModelScope 的 `pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue` 下载。模型由安装脚本下载，本仓库不包含模型权重。
- **语音活动检测 silero-vad**：[snakers4/silero-vad](https://github.com/snakers4/silero-vad)，MIT License。
- **Claude Code 接入**：使用 Anthropic 的 [claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/)。需要用户自行安装 Claude Code 并登录。
- **测试页里的 Live2D 核心库**：`main/digital-human/js/live2d/live2dcubismcore.min.js`，属于 Live2D Inc. 的专有许可，随原项目一起提供，本项目没有修改。
