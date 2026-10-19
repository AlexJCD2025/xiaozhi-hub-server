#!/usr/bin/env bash
# xiaozhi-hub-server 卸载（macOS / Linux）
# 用法：bash ~/xiaozhi-hub-server/scripts/uninstall.sh
#   1. 取消开机自启并停止后台服务
#   2. 删除整个安装目录（代码、虚拟环境、语音识别模型、配置和 API Key）
#   3. 询问是否删除 Claude Code 的工作目录 ~/xiaozhi-workspace（默认保留）
#   uv、Python 3.10、ffmpeg、opus、Claude Code 可能被别的程序共用，不删，最后列出怎么手动删。
# 可选环境变量：ASSUME_YES=1 不询问，直接卸载（工作目录仍然保留）
set -euo pipefail

# 整个脚本放在函数里：后面会删掉本脚本所在的目录，bash 边读边执行会读不到后面的内容
main() {
  local root server workspace port
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  server="$root/main/xiaozhi-server"
  workspace="$HOME/xiaozhi-workspace"
  # 运行本脚本的终端停在安装目录里时，删完后那个终端会留在一个已经不存在的目录，最后提醒一下
  local here="$PWD"

  # 确认是本项目的安装目录，避免删错
  [[ -f "$server/app.py" && -f "$root/scripts/install.sh" && "$root" != "/" && "$root" != "$HOME" ]] \
    || fail "${root} 看起来不是 xiaozhi-hub-server 的安装目录，已停止"

  # 开发用的完整仓库（不是一键安装下载的浅克隆）里可能有没推送的提交，不替用户删
  local dev_repo=0
  if git -C "$root" rev-parse --git-dir >/dev/null 2>&1 && [[ "$(git -C "$root" rev-parse --is-shallow-repository)" != "true" ]]; then
    dev_repo=1
  fi

  printf "\n将要卸载 xiaozhi-hub-server：\n"
  info "取消开机自启，停止后台服务"
  if [[ "$dev_repo" == "1" ]]; then
    info "（这是完整的 git 仓库，可能是开发用的，只取消开机自启，不删除目录：${root}）"
  else
    info "删除安装目录：$root"
    info "（包括虚拟环境、语音识别模型、配置文件 data/.config.yaml 和里面的 API Key）"
  fi
  confirm "确定卸载？" || { echo "已取消，没有做任何改动。"; exit 0; }

  step "取消开机自启"
  local autostart_state=0 autostart_status
  autostart_status="$(bash "$root/scripts/autostart.sh" status 2>/dev/null)" || autostart_state=$?
  if [[ "$autostart_state" == "0" ]]; then
    bash "$root/scripts/autostart.sh" disable
  elif [[ "$autostart_state" == "3" ]]; then
    # 自启属于另一份安装，不能替它取消
    info "$autostart_status"
    info "这份安装没有开机自启，那份的不动"
  else
    info "没有开启开机自启"
  fi

  # 用 start.sh 手动启动的服务还在跑的话，先让用户停掉，不在它运行时删文件
  # 直接从配置文件读端口，不依赖虚拟环境（虚拟环境坏了也要能卸载）
  port="$(sed -n 's/^  http_port: *\([0-9][0-9]*\).*/\1/p' "$server/data/.config.yaml" 2>/dev/null | head -1 || true)"
  port="${port:-16688}"
  # 刚停掉的后台服务退出要一点时间，等最多 10 秒
  local i up=0
  if command -v curl >/dev/null 2>&1; then
    for ((i = 0; i < 10; i++)); do
      curl -s -m 2 -o /dev/null "http://127.0.0.1:$port/xiaozhi/ota/" || { up=0; break; }
      up=1; sleep 1
    done
  fi
  # 占着端口的如果是另一份安装的后台服务（自启属于那份、端口又一样），不是这份的，照常卸载
  if [[ "$up" == "1" && "$autostart_state" == "3" ]] && command -v lsof >/dev/null 2>&1; then
    local other_pid listener
    other_pid="$(printf '%s' "$autostart_status" | sed -n 's/.*（进程 \([0-9][0-9]*\)）.*/\1/p')"
    listener="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
    [[ -n "$other_pid" && "$listener" == "$other_pid" ]] && up=0
  fi
  if [[ "$up" == "1" ]]; then
    fail "服务还在运行（端口 ${port}），可能是用 start.sh 手动启动的。请在它的终端按 Ctrl+C 停止后，重新运行本脚本"
  fi

  if [[ "$dev_repo" == "1" ]]; then
    printf "\n已取消开机自启。目录 %s 没有删除，确认不需要后请手动删除。\n" "$root"
  else
    step "删除安装目录"
    cd "$HOME"
    rm -rf "$root"
    info "已删除：$root"
    [[ "$here/" == "$root/"* ]] && info "你的终端还停在已删除的目录里，请运行 cd ~ 离开"
  fi

  if [[ -d "$workspace" ]]; then
    step "Claude Code 的工作目录"
    if [[ "${ASSUME_YES:-0}" != "1" ]] && [[ -t 0 ]] && ask_no "是否删除 ${workspace}？里面是 Claude Code 帮你读写过的文件"; then
      rm -rf "$workspace"
      info "已删除：$workspace"
    else
      info "保留：${workspace}（不需要的话可以手动删除）"
    fi
  fi

  cat <<EOF

卸载完成。下面这些可能被别的程序共用，没有删除，确认不需要的话可以手动处理：
  - uv 的下载缓存（约 500MB）：uv cache clean
  - uv 安装的 Python 3.10：uv python uninstall 3.10
  - uv 本身、ffmpeg、opus、Claude Code：用当初安装它们的方式卸载（例如 brew uninstall ffmpeg opus）
EOF
}

step() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info() { printf "    %s\n" "$*"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }
# 卸载要明确回答 y 才执行；ASSUME_YES=1 时直接执行；没有终端又没设 ASSUME_YES 时不执行
confirm() {
  [[ "${ASSUME_YES:-0}" == "1" ]] && return 0
  [[ -t 0 ]] || { echo "没有终端，无法确认。要直接卸载请设置 ASSUME_YES=1" >&2; return 1; }
  ask_no "$1"
}
ask_no() { local answer; read -r -p "    $1 [y/N] " answer; [[ "$answer" =~ ^[Yy] ]]; }

main "$@"
