#!/usr/bin/env bash
# xiaozhi-hub-server 一键安装（macOS / Linux）
# 用法：bash scripts/install.sh
# 可选环境变量：
#   UV_INDEX_URL   Python 包镜像，例如 https://mirrors.aliyun.com/pypi/simple/
#   ASSUME_YES=1   所有询问都按默认回答（非交互安装）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/main/xiaozhi-server"
VENV="$SERVER/.venv"
PY="$VENV/bin/python"
MODEL="$SERVER/models/SenseVoiceSmall/model.pt"
MODEL_URL="https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt"
MODEL_MIN_BYTES=900000000
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
file_size() { "$PY" -c 'import os,sys; print(os.path.getsize(sys.argv[1]) if os.path.exists(sys.argv[1]) else 0)' "$1"; }

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
  command -v brew >/dev/null 2>&1 || fail "未找到 Homebrew，请先安装：https://brew.sh ，然后重新运行本脚本"
  for pkg in opus ffmpeg; do
    if brew list "$pkg" >/dev/null 2>&1; then info "$pkg 已安装"; else
      info "安装 $pkg ..."; brew install "$pkg"
    fi
  done
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
step "安装 Python 依赖（首次较慢，torch 等体积较大）"
"$PY" "$ROOT/scripts/install_deps.py" --python "$PY" || fail "Python 依赖安装失败"

# ---------- 5. 语音识别模型 ----------
step "下载语音识别模型 SenseVoiceSmall（约 900MB）"
if (( $(file_size "$MODEL") >= MODEL_MIN_BYTES )); then
  info "已存在，跳过：$MODEL"
else
  mkdir -p "$(dirname "$MODEL")"
  curl -L --fail --retry 3 -C - -o "$MODEL.part" "$MODEL_URL" || fail "模型下载失败，可重新运行本脚本断点续传"
  (( $(file_size "$MODEL.part") >= MODEL_MIN_BYTES )) || fail "模型文件大小异常，请删除 $MODEL.part 后重试"
  mv "$MODEL.part" "$MODEL"
  info "已下载：$MODEL"
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

cat <<EOF

安装完成。

  启动服务：  bash scripts/start.sh
  首次打开管理后台需要在本机浏览器访问 http://127.0.0.1:8003/admin 设置密码。

提醒：
  - 请在路由器里给这台电脑设置固定 IP，否则 IP 变了设备会连不上。
  - 服务只应在局域网内使用，不要暴露到公网。
EOF
