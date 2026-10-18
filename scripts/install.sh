#!/usr/bin/env bash
# xiaozhi-hub-server 一键安装（macOS / Linux）
# 用法：bash scripts/install.sh [--full]
#   默认精简安装：不装 torch、FunASR 等，语音识别使用 sherpa-onnx 版 SenseVoice（模型约 240MB）
#   --full       完整安装原项目全部依赖，并额外下载 FunASR 的模型（约 900MB），可在管理后台切换到原项目的所有模块
# 可选环境变量：
#   UV_INDEX_URL   Python 包镜像，例如 https://mirrors.aliyun.com/pypi/simple/
#   ASSUME_YES=1   所有询问都按默认回答（非交互安装）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/main/xiaozhi-server"
VENV="$SERVER/.venv"
PY="$VENV/bin/python"
# 默认的语音识别模型：sherpa-onnx 版 SenseVoice（int8），目录与原项目 config.yaml 中 SherpaASR 的 model_dir 一致
SHERPA_DIR="$SERVER/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SHERPA_URL="https://modelscope.cn/models/pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue/resolve/master"
# 完整安装时额外下载：FunASR 使用的 SenseVoiceSmall
FUNASR_MODEL="$SERVER/models/SenseVoiceSmall/model.pt"
FUNASR_URL="https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt"
CONFIG="$SERVER/data/.config.yaml"

step() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info() { printf "    %s\n" "$*"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }
# ask "问题" Y|N：返回 0 表示"是"；ASSUME_YES=1 时直接用默认值
ask() {
  local default="${2:-N}" hint="[y/N]" answer
  [[ "$default" == "Y" ]] && hint="[Y/n]"
  if [[ "${ASSUME_YES:-0}" == "1" || ! -t 0 ]]; then answer="$default"; else read -r -p "    $1 $hint " answer; fi
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy] ]]
}
# download <地址> <保存路径> <最小字节数>：断点续传，下载完校验大小
download() {
  local url="$1" dest="$2" min="$3"
  if (( $(file_size "$dest") >= min )); then info "已存在，跳过：$dest"; return; fi
  mkdir -p "$(dirname "$dest")"
  curl -L --fail --retry 3 -C - -o "$dest.part" "$url" || fail "下载失败，可重新运行本脚本断点续传：$url"
  (( $(file_size "$dest.part") >= min )) || fail "文件大小异常，请删除 $dest.part 后重试"
  mv "$dest.part" "$dest"
  info "已下载：$dest"
}
file_size() { "$PY" -c 'import os,sys; print(os.path.getsize(sys.argv[1]) if os.path.exists(sys.argv[1]) else 0)' "$1"; }

FULL=0
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    *) fail "未知参数：$arg（可用参数：--full）" ;;
  esac
done

OS="$(uname -s)"
[[ "$OS" == "Darwin" || "$OS" == "Linux" ]] || fail "不支持的系统：$OS（Windows 请使用 scripts/install.ps1）"

# ---------- 1. uv ----------
step "检查 uv（用于管理 Python 版本和虚拟环境）"
if ! command -v uv >/dev/null 2>&1; then
  [[ -x "$HOME/.local/bin/uv" ]] && export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  if ask "未找到 uv，是否用官方脚本安装？（curl -LsSf https://astral.sh/uv/install.sh | sh）" Y; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else
    fail "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"
  fi
fi
info "$(uv --version)"

# ---------- 2. 系统依赖：opus、ffmpeg ----------
step "检查系统依赖（opus、ffmpeg）"
if [[ "$OS" == "Darwin" ]]; then
  # 只看命令和库是否可用，不看由哪个 formula 提供（例如已装 ffmpeg-full 时不能再装 ffmpeg）
  need=()
  command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
  brew_prefix="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
  [[ -n "$(ls "$brew_prefix"/lib/libopus*.dylib /usr/local/lib/libopus*.dylib 2>/dev/null)" ]] || need+=(opus)
  if (( ${#need[@]} == 0 )); then info "opus、ffmpeg 已可用"; else
    command -v brew >/dev/null 2>&1 || fail "缺少 ${need[*]}，且未找到 Homebrew。请先安装 Homebrew（https://brew.sh）或手动安装 ${need[*]}"
    if ask "需要安装：${need[*]}，执行 brew install ${need[*]}？" Y; then brew install "${need[@]}"; else
      fail "请手动安装 ${need[*]} 后重新运行本脚本"
    fi
  fi
else
  need=()
  command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
  if ! (ldconfig -p 2>/dev/null | grep -q libopus); then need+=(opus); fi
  if (( ${#need[@]} == 0 )); then info "opus、ffmpeg 已安装"; else
    if command -v apt-get >/dev/null 2>&1; then
      pkgs=(); for p in "${need[@]}"; do [[ "$p" == opus ]] && pkgs+=(libopus0) || pkgs+=(ffmpeg); done
      cmd=(sudo apt-get install -y "${pkgs[@]}")
    elif command -v dnf >/dev/null 2>&1; then
      pkgs=(); for p in "${need[@]}"; do [[ "$p" == opus ]] && pkgs+=(opus) || pkgs+=(ffmpeg); done
      cmd=(sudo dnf install -y "${pkgs[@]}")
    elif command -v pacman >/dev/null 2>&1; then
      cmd=(sudo pacman -S --noconfirm "${need[@]}")
    else
      fail "无法自动安装 ${need[*]}，请用系统的包管理器安装后重新运行本脚本"
    fi
    info "需要安装：${need[*]}"
    if ask "执行 ${cmd[*]}？" Y; then "${cmd[@]}"; else fail "请手动安装 ${need[*]} 后重新运行本脚本"; fi
  fi
fi

# ---------- 3. Python 3.10 虚拟环境 ----------
step "准备 Python 3.10 虚拟环境"
if [[ -x "$PY" ]] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)'; then
  info "已存在：$VENV"
else
  uv venv --python 3.10 "$VENV"
fi

# ---------- 4. Python 依赖 ----------
if (( FULL )); then
  step "安装 Python 依赖（完整安装，首次较慢，torch 等体积较大）"
  "$PY" "$ROOT/scripts/install_deps.py" --python "$PY" --full || fail "Python 依赖安装失败"
else
  step "安装 Python 依赖（精简安装）"
  "$PY" "$ROOT/scripts/install_deps.py" --python "$PY" || fail "Python 依赖安装失败"
  if "$PY" -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("torch") else 1)' 2>/dev/null; then
    info "提示：虚拟环境里还留有之前完整安装的 torch 等依赖。想释放空间，可以删除 $VENV 后重新运行本脚本"
  fi
fi

# ---------- 5. 语音识别模型 ----------
step "下载语音识别模型 SenseVoice（sherpa-onnx int8 版，约 240MB）"
download "$SHERPA_URL/model.int8.onnx" "$SHERPA_DIR/model.int8.onnx" 200000000
download "$SHERPA_URL/tokens.txt" "$SHERPA_DIR/tokens.txt" 100000
if (( FULL )); then
  step "下载 FunASR 语音识别模型 SenseVoiceSmall（约 900MB，完整安装才需要）"
  download "$FUNASR_URL" "$FUNASR_MODEL" 900000000
fi

# ---------- 6. 配置文件 ----------
step "生成配置文件"
CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.claude/local/claude" ]] && CLAUDE_BIN="$HOME/.claude/local/claude"
if [[ -f "$CONFIG" ]]; then
  info "已存在，保持不变：$CONFIG"
else
  mkdir -p "$(dirname "$CONFIG")"
  cp "$ROOT/scripts/config.template.yaml" "$CONFIG"
  chmod 600 "$CONFIG"
  if [[ -n "$CLAUDE_BIN" ]] && ask "检测到 Claude Code，默认使用它作为大模型？" Y; then
    info "大模型：ClaudeCLI（本机 Claude Code）"
  else
    # 原项目默认的免费模型；API Key 在管理后台填写
    "$PY" - "$CONFIG" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r"(\n  LLM: )ClaudeCLI[^\n]*", r"\1ChatGLMLLM        # 在管理后台填写 API Key", s, count=1)
s = re.sub(r"(\n  Intent: )nointent[^\n]*", r"\1function_call", s, count=1)
open(p, "w", encoding="utf-8").write(s)
PYEOF
    info "大模型：ChatGLMLLM（智谱 glm-4-flash，需要在管理后台填写 API Key）"
  fi
  info "已生成：$CONFIG"
fi

# 精简安装没有 FunASR：之前完整安装时选了 FunASR 的配置，切换到 SherpaASR（同一个 SenseVoice 模型）
if (( ! FULL )) && [[ "$(cd "$SERVER" && "$PY" - <<'PYEOF' 2>/dev/null
from core.admin import config_store
print(config_store.entry_type(config_store.load_merged(), "ASR"))
PYEOF
)" == "fun_local" ]]; then
  if ask "当前配置的语音识别是 FunASR，精简安装不包含它。切换到 SherpaASR（同一个 SenseVoice 模型，内存约减半）？" Y; then
    (cd "$SERVER" && "$PY" -c 'from core.admin import config_store; config_store.select_entry("ASR", "SherpaASR", config_store.load_merged())' 2>/dev/null) \
      && info "语音识别已切换为 SherpaASR" || info "切换失败，请在管理后台的「语音识别」页切换"
  else
    info "保持 FunASR。需要它时请用完整安装：bash scripts/install.sh --full"
  fi
fi

# ---------- 7. Claude Code ----------
step "检查 Claude Code"
if [[ -n "$CLAUDE_BIN" ]]; then
  info "$("$CLAUDE_BIN" --version 2>/dev/null || echo '无法获取版本')（$CLAUDE_BIN）"
  info "如果还没登录，请在终端运行 claude 并按提示登录（安装脚本不会代替你登录）"
else
  info "未找到 Claude Code。如需使用，请先安装：https://docs.claude.com/claude-code ，安装并登录后在管理后台切换到 ClaudeCLI"
fi
mkdir -p "$HOME/xiaozhi-workspace"

# ---------- 8. 自检 ----------
step "环境自检"
"$PY" "$ROOT/scripts/doctor.py" --skip-ports 2>/dev/null || info "自检发现异常，请按上面的提示处理后再启动"

HTTP_PORT="$("$PY" - "$SERVER" <<'PYEOF'
import sys, yaml
from pathlib import Path
d = Path(sys.argv[1])
port = 8003
for f in (d / "config.yaml", d / "data" / ".config.yaml"):
    try:
        port = ((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("server") or {}).get("http_port", port)
    except Exception:
        pass
print(port)
PYEOF
)"

cat <<EOF

安装完成。

  启动服务：  bash scripts/start.sh
  首次打开管理后台需要在本机浏览器访问 http://127.0.0.1:${HTTP_PORT}/admin 设置密码。

提醒：
  - 请在路由器里给这台电脑设置固定 IP，否则 IP 变了设备会连不上。
  - 服务只应在局域网内使用，不要暴露到公网。
EOF
