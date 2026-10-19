# xiaozhi-hub-server 使用说明

[返回 README](../README.md) · [设计细节](design.md)

xiaozhi-hub-server 是小智设备（M5Stack StackChan、ESP32 开发板等）的**本地服务端**。设备负责听和说；服务端运行在你自己的电脑上，把你说的话交给本机的 Claude Code，或者你选的大模型。

它是兼容小智协议的社区项目，不是小智官方项目，与官方没有关联。

从安装到第一次对话，按顺序看前四节就够了：

1. [安装](#1-安装)
2. [首次配置](#2-首次配置)
3. [连接设备](#3-连接设备)
4. [跟设备说话](#4-跟设备说话)
5. [管理后台](#5-管理后台)
6. [常见问题](#6-常见问题)
7. [开机自启与更新](#7-开机自启与更新)
8. [安装细节](#8-安装细节)

---

## 1. 安装

需要：

- macOS 或 Linux（Windows 脚本 `scripts/install.ps1`、`scripts/start.ps1` 还没有实测）；
- 能访问网络；
- 已安装 git（macOS 没有时运行 `xcode-select --install`）。

**一键安装**：在终端运行下面这一行。代码会下载到 `~/xiaozhi-hub-server`，然后自动安装；安装结束时会询问是否开机自启（见 [§7](#7-开机自启与更新)）。

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | bash
```

**手动安装**：

```bash
git clone --depth 1 https://github.com/justhalfbit/xiaozhi-hub-server.git
cd xiaozhi-hub-server
bash scripts/install.sh   # 安装
bash scripts/start.sh     # 启动（没有开启开机自启时）
```

安装脚本会准备好运行需要的一切，缺什么会先问你再装：Python 3.10 和依赖（装在项目自己的虚拟环境里，不影响系统的 Python）、opus 和 ffmpeg、语音识别模型，以及配置文件。电脑上装了 Claude Code 时，默认用它当大模型；没装时用智谱 glm-4-flash，需要到管理后台填 API Key。

一共下载约 370MB，装好后占用约 800MB 硬盘，运行时内存约 0.6GB。国内网络慢的话，先看 [§8 安装细节](#8-安装细节) 里的镜像设置。

安装结束和每次启动时都会运行自检，并显示管理后台和 OTA 的地址。

## 2. 首次配置

1. **设置管理密码**：在运行服务的这台电脑上，用浏览器打开 `http://127.0.0.1:16688/admin`，设置管理密码（至少 6 位）。为了安全，第一次设置只能在本机操作；设置好之后，局域网里的其他电脑也能登录。
2. **准备 Claude Code**（要用 Claude Code 时）：在终端运行一次 `claude` 完成登录。Claude Code 默认只能读取工作目录 `~/xiaozhi-workspace` 里的文件；模型、思考级别和权限级别可以在管理后台的"大模型"页调整。
3. **配置别的大模型**（可选）：在"大模型"页选一个厂商，填接口地址、模型和 API Key，可以先点"测试连接"试一下。

所有设置都保存在 `main/xiaozhi-server/data/.config.yaml`，这个文件不会提交到 Git。API Key 在里面是明文，管理密码只保存哈希值。

## 3. 连接设备

设备配网时，在配网页的"高级选项"里填写 OTA 地址：

```
http://<电脑的局域网 IP>:16688/xiaozhi/ota/
```

| 设备 | 方法 |
| --- | --- |
| 官方小智固件 1.6.1 及以上的开发板 | 进入配网模式，在配网页的"高级选项"里填写 OTA 地址 |
| M5Stack StackChan（官方固件，实测 1.5.1） | 在 App 中解绑设备 → 设置 → 系统 → Factory Reset → 开机引导页选 Skip → 进入 AI Agent → 手机连接设备热点打开配网页 → 在"高级选项"里填写 OTA 地址 → 填写 2.4GHz Wi-Fi |

- 设备同一时间只连一台电脑。换一台电脑运行服务时，要重新配网，填新电脑的 OTA 地址。
- 建议在路由器里给这台电脑设置固定 IP，否则 IP 变了设备就连不上。
- StackChan 待机时会随机摇头，可以在设备的"设置 → AI.Agent → General → Idle movement"里关闭。

## 4. 跟设备说话

下面的时长都是默认值，可以在管理后台改。后台的"工作原理"页会按你当前的设置列出同样的内容。

**唤醒**：说"Hi, StackChan"，设备回一句问候后开始听；也可以点一下屏幕，设备直接开始听（没有问候）。

**换模型（呼名）**：句子开头说"嗨 + 称呼"（说"Hi""Hey"也行），就换成对应的模型回答。比如"嗨 Claude"交给本机 Claude Code，"嗨 DeepSeek"交给 DeepSeek。
- 称呼后面可以直接接内容：说"嗨 Claude，看看工作目录里有什么"，称呼会去掉，后半句交给 Claude。
- 只说"嗨 Claude"时，设备回一声"我在"，等你说下一句。
- 换过一次后，这台设备之后都交给这个模型，直到你叫别的称呼；断线重连、服务重启后也记得。
- 每个模型只记得跟自己的对话，切换时不带上下文过去。
- 默认只能叫 Claude。给其他模型配称呼，或者按你的发音录入称呼，在后台的"呼名"页（见 [§5](#5-管理后台)）。

**打断**：设备说话时**不收音**，不能直接插话（这是固件的收音方式）。要打断，点一下屏幕上的脸，它马上停下，然后你接着说。喊唤醒词也能打断，但设备音量大时容易听不到。

**长任务**：让 Claude 做要几分钟的事（比如整理一批文件）时，设备会陪着等，最长 10 分钟，做完当场说结果。
- 30 秒还没开口，设备会说"还在处理，请稍等"；之后报已用时间，间隔逐渐拉长到 2 分钟一次。
- 等待期间想说话，先点屏幕打断。
- 超时被停下、Claude 没有回应、或者你打断之后，说"继续"它会接着做。

**接着上次聊**（使用 Claude Code 时）：
- 两小时内再次唤醒，自动接上上次的对话。
- 隔得更久的，说"接着上次聊"或"继续刚才的话题"也能接上；说"嗨 Claude，接着上次聊"同样可以。
- 对话记录按设备、按电脑分别保存。
- 用其他大模型（API）时，对话只在这次连接里有效，设备断开后就不记得了。

**结束**：说"退出"或"关闭"，马上断开。2 分钟没人说话时，设备说一句"好一会儿没听到你说话，我先休息啦"后断开。

想知道连接什么时候断、Claude Code 进程什么时候启动和关闭、哪些记录会保存，见 [设计细节](design.md)。

## 5. 管理后台

在本机打开 `http://127.0.0.1:16688/admin`；局域网里的其他电脑用 `http://<电脑的局域网 IP>:16688/admin`。

| 页面 | 能做什么 | 什么时候生效 |
| --- | --- | --- |
| 状态 | 在线设备、连接地址、环境自检；给设备发表情，看固件支持哪些表情 | — |
| 工作原理 | 按当前设置列出一句话经过的每一步、说什么会怎样，以及发给大模型的提示词 | — |
| 大模型 | 在本机 Claude Code 和各家 API 之间选一个默认模型。Claude Code 可以选模型、思考级别、权限级别（只读 / 可编辑 / Auto）、是否联网，以及超时、进度提示等高级设置 | 设备重新连接后 |
| 呼名 | 给每个模型配称呼，改只说称呼时的回复。"用设备录入"：唤醒设备说 3 遍"嗨 + 称呼"，把识别到的写法勾选加入称呼 | 马上 |
| 语音识别 | 在本地 X-ASR 和豆包流式识别之间切换（豆包填 API Key，可以先测试连接）；说完停顿多久算一句；热词；识别后纠错；最近识别的句子，认错的词一键加进热词或纠错表 | 停顿时长、纠错：马上；切换识别、热词：设备重新连接后 |
| 语音合成 | 选 EdgeTTS 的音色并试听 | 设备重新连接后 |
| 意图识别 | 用 API 模型时，选择要不要让模型调用插件（查天气、听新闻、放音乐等）。用 Claude Code 时固定为不用插件 | 设备重新连接后 |

"设备重新连接"：等设备因为没人说话自动断开，下次唤醒时就用上了；着急的话可以重启设备。

**热词和纠错怎么选**：某个词经常被认错时，先把正确的写法加进热词，让识别更偏向它；加了热词还是认错（比如 "Claude Code" 总被认成 "cloud code"），就在纠错表里写"认错的写法 → 正确写法"，识别出文字后直接替换。

## 6. 常见问题

**连接**

- **设备显示"检查新版本失败"**：确认电脑和设备在同一个局域网，OTA 地址的 IP、端口正确。用手机浏览器打开 OTA 地址，能看到"OTA接口运行正常"，就说明网络是通的。
- **电脑 IP 变了，设备连不上**：在路由器里给电脑设置固定 IP，然后重新配网。
- **Windows 防火墙**：需要放行 TCP 16688 和 16689 端口（仅限专用网络）。

**听和说**

- **话说到一半就被截断**：在"语音识别"页把"说完停顿多久算一句"调大，一般 800～1200 毫秒比较合适。
- **某个词总是认错**：用"语音识别"页的热词和纠错，见 [§5](#5-管理后台) 末尾。
- **叫"嗨 Claude"没反应**：在"语音识别"页的"最近识别"里看你的发音被认成了什么，再到"呼名"页用"用设备录入"把这种写法加进称呼。
- **设备说话断续**：调大 `data/.config.yaml` 里的 `tts_audio_prebuffer_count`（默认 15，约 0.9 秒），合理范围是 10～25。调大后，打断时要多播一会儿才停。
- **用豆包时设备说"语音识别连不上"**：到"语音识别"页检查豆包的 API Key，点"测试连接"看具体原因。

**Claude Code**

- **Claude Code 没有反应**：先在"状态"页确认能找到 Claude Code。状态页只检查有没有安装，不检查有没有登录；没登录时，在终端运行一次 `claude` 登录。然后查看 `main/xiaozhi-server/tmp/server.log`，里面记录了工具调用、限流状态和每轮的结果。
- **回答太慢**：在"大模型"页把模型换成 Haiku，思考级别选"低"。
- **它说没有权限做某件事**：默认只能读文件。要让它改文件或执行命令，在"大模型"页把权限级别改成"可编辑"或"Auto"，改之前先看 [安全提示](#安全提示)。

**安装**

- **自检提示"模块依赖"缺包**：重新运行一次安装脚本补装依赖。
- **下载模型很慢或失败**：设置 Hugging Face 镜像后重新运行安装脚本，见 [§8](#8-安装细节)；下载支持断点续传。

## 7. 开机自启与更新

### 开机自启

安装结束时会询问是否开机自启。开启后，服务在后台运行，不用开着终端；异常退出时 30 秒后自动重启。以后也可以随时开关：

```bash
bash scripts/autostart.sh enable    # 开启，并马上在后台启动
bash scripts/autostart.sh disable   # 关闭，并停止后台服务
bash scripts/autostart.sh status    # 查看是否在运行
bash scripts/autostart.sh restart   # 重启后台服务
```

- **macOS**：用 launchd（`~/Library/LaunchAgents/com.xiaozhi-hub.server.plist`），**登录这个用户后**自动启动。Claude Code 的登录信息存在用户的钥匙串里，所以不能做成登录前就启动的系统服务。想开机就启动，在"系统设置 → 用户与群组"里开启自动登录（开了 FileVault 的电脑不能自动登录）。安装目录不要放在"文稿""桌面""下载"里，后台服务没有权限读这些目录。
- **Linux**：用 systemd 用户服务（`~/.config/systemd/user/xiaozhi-hub.service`），默认登录后启动。想开机就启动（不用登录），运行 `sudo loginctl enable-linger $USER`。
- 日志照常在 `main/xiaozhi-server/tmp/server.log`。服务启动失败时，macOS 的报错在 `tmp/autostart-error.log`，Linux 用 `journalctl --user -u xiaozhi-hub` 查看。
- 开启时会记下当前终端的 PATH，让后台服务能找到 ffmpeg 和 claude。以后 Claude Code 换了安装位置，重新运行一次 `enable` 就行。
- 开着自启时，不要再手动运行 `start.sh`，否则端口会冲突。想在终端里看实时输出调试，先 `disable`，调试完再 `enable`。

### 更新

```bash
bash ~/xiaozhi-hub-server/scripts/update.sh
```

手动下载的，换成自己的目录。更新脚本会：

1. 从 GitHub 拉取最新代码。配置 `data/.config.yaml`、语音识别模型和虚拟环境都不在 Git 里，不受影响；
2. 重新运行安装脚本，补装新版本新增的依赖和模型，已有的直接跳过；
3. 开了开机自启的话，自动重启后台服务。手动启动的服务，在它的终端按 Ctrl+C 停止，再运行 `start.sh`。

如果改过仓库里的文件（例如直接改了 `config.yaml`），更新脚本会列出这些文件并停下，不会覆盖。自己的配置请在管理后台修改，或者写在 `data/.config.yaml` 里。

再次运行一键安装命令也可以：检测到已经安装过，会改为运行更新脚本。

## 8. 安装细节

### 装了什么

- uv（Python 包管理工具）、Python 3.10、opus 和 ffmpeg：电脑上已有就直接用，缺少时先询问再安装；
- Python 依赖：全部装在项目自己的虚拟环境 `main/xiaozhi-server/.venv` 里；
- 语音识别模型：X-ASR 中英混合模型（sherpa-onnx int8 版），从 Hugging Face 下载；
- 配置文件 `main/xiaozhi-server/data/.config.yaml`：已经存在时保持不变；
- 一键安装只下载运行需要的 `main/xiaozhi-server`、`scripts`、`hub-docs`，不下载上游的智控台、数字人等目录和提交历史。

### 下载量和内存

2026-10-20 在 macOS 上实测：

| 项目 | 下载 | 装好后占用硬盘 |
| --- | --- | --- |
| 代码（只含运行需要的目录） | 约 21MB | 约 50MB |
| Python 3.10 | 约 25MB | 约 66MB |
| Python 依赖（87 个包，最大的是 claude-agent-sdk，约 61MB） | 约 146MB | 约 500MB |
| 语音识别模型 X-ASR | 约 175MB | 约 175MB |
| **合计** | **约 370MB** | **约 800MB** |

- 电脑上没有 uv、ffmpeg、opus 时，还要额外下载它们，不算在上表里。
- uv 会在 `~/.cache/uv` 里留一份 Python 包的缓存（约 500MB），下次安装、更新时直接用，不用重新下载。不需要的话可以运行 `uv cache clean` 删掉。
- 运行时内存：服务本身约 0.6GB，大部分是语音识别模型。用 Claude Code 对话时，每台连着的设备多一个 Claude Code 进程，约 0.26GB；对话结束、空闲超时后这个进程会退出。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/` | 使用国内 Python 包镜像 |
| `HF_ENDPOINT=https://hf-mirror.com` | 使用国内 Hugging Face 镜像下载语音识别模型 |
| `ASSUME_YES=1` | 非交互安装，所有询问都按默认回答 |
| `SKIP_DOCTOR=1` | 启动时跳过自检 |
| `XIAOZHI_HUB_DIR` | 一键安装时的安装目录，默认 `~/xiaozhi-hub-server` |

一键安装时，环境变量要写在 `bash` 前面，例如：

```bash
curl -fsSL https://raw.githubusercontent.com/justhalfbit/xiaozhi-hub-server/main/scripts/bootstrap.sh | HF_ENDPOINT=https://hf-mirror.com bash
```

## 安全提示

- 服务**只应在局域网内使用**，不要暴露到公网。
- 管理后台一定要设置密码；API Key 不要提交到 Git。
- Claude Code 默认只开放只读工具。在后台放开写文件、执行命令后，语音指令就能修改文件、在电脑上执行命令，请谨慎。读写文件限制在工作目录里，但执行的命令（Auto 级别）不受这个限制。

## 致谢与许可

本项目使用 [MIT License](../LICENSE)。

- **原项目**：基于 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 改造，MIT License，Copyright (c) 2025 xinnan-tech。
- **设备端协议与固件**：[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)。
- **语音识别模型 X-ASR**（默认）：来自 [Gilgamesh-J/X-ASR](https://github.com/Gilgamesh-J/X-ASR)，Apache-2.0。使用的 int8 版由 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 转换（Apache-2.0），从 Hugging Face 的 `csukuangfj2/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03` 下载。
- **语音识别模型 SenseVoiceSmall**（可选）：来自 [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（阿里巴巴通义实验室），按 [FunASR 模型开源许可协议](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE) 使用。int8 版由 sherpa-onnx 转换，从 ModelScope 的 `pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue` 下载；只在把语音识别切换为 SherpaASR 时才会下载。
- 模型都由安装脚本或程序下载，本仓库不包含模型权重。
- **语音活动检测 silero-vad**：[snakers4/silero-vad](https://github.com/snakers4/silero-vad)，MIT License。
- **Claude Code 接入**：使用 Anthropic 的 [claude-agent-sdk](https://pypi.org/project/claude-agent-sdk/)。需要用户自行安装 Claude Code 并登录。
- **测试页里的 Live2D 核心库**：`main/digital-human/js/live2d/live2dcubismcore.min.js`，属于 Live2D Inc. 的专有许可，随原项目一起提供，本项目没有修改。
