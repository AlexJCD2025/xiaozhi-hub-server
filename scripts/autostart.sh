#!/usr/bin/env bash
# xiaozhi-hub-server 开机自启（macOS / Linux）
# 用法：bash scripts/autostart.sh enable|disable|restart|status
#   enable   设置开机自启，并马上在后台启动服务
#   disable  取消开机自启，并停止后台服务
#   restart  重启后台服务（更新后、改了需要重启的配置后）
#   status   查看是否已开启、是否在运行
# macOS 用 launchd（~/Library/LaunchAgents），登录后启动；Linux 用 systemd 用户服务。
# 服务的日志照常写在 main/xiaozhi-server/tmp/server.log。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/main/xiaozhi-server"
LABEL="com.xiaozhi-hub.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT="xiaozhi-hub.service"
UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT"
# 启动失败时的报错（日志系统起来之前的异常）；正常运行的日志在 tmp/server.log
ERR_LOG="$SERVER/tmp/autostart-error.log"

info() { printf "    %s\n" "$*"; }
fail() { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

xml_escape() { local s="${1//&/&amp;}"; s="${s//</&lt;}"; printf '%s' "${s//>/&gt;}"; }

# ---------- macOS：launchd ----------
mac_enabled() { [[ -f "$PLIST" ]]; }
mac_pid() { launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '$1 == "pid" {print $3; exit}'; }

mac_enable() {
  mkdir -p "$(dirname "$PLIST")" "$SERVER/tmp"
  # launchd 的 PATH 只有系统目录，找不到 Homebrew 的 ffmpeg 和 ~/.local/bin 里的 claude，所以带上当前终端的 PATH
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$(xml_escape "$ROOT/scripts/start.sh")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$SERVER")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml_escape "$PATH")</string>
    <key>SKIP_DOCTOR</key><string>1</string>
    <key>PYTHONUTF8</key><string>1</string>
    <key>AUTOSTART_ERR_LOG</key><string>$(xml_escape "$ERR_LOG")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- 异常退出时自动重启，间隔至少 30 秒 -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$ERR_LOG")</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl enable "gui/$(id -u)/$LABEL"
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
}

mac_disable() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
}

mac_restart() { launchctl kickstart -k "gui/$(id -u)/$LABEL"; }

# ---------- Linux：systemd 用户服务 ----------
linux_check() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1 \
    || fail "这台电脑没有可用的 systemd 用户服务，无法自动设置开机自启。可以用 crontab 的 @reboot 或其他方式运行 bash \"$ROOT/scripts/start.sh\""
}
linux_enabled() { [[ -f "$UNIT_FILE" ]]; }
linux_pid() { local p; p="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || true)"; [[ "$p" != "0" ]] && echo "$p"; }

linux_enable() {
  linux_check
  mkdir -p "$(dirname "$UNIT_FILE")"
  # systemd 的值里 % 要写成 %%；路径里有空格时整体加引号
  local path_env="${PATH//%/%%}"
  cat >"$UNIT_FILE" <<EOF
[Unit]
Description=xiaozhi-hub-server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$SERVER
Environment="PATH=$path_env" SKIP_DOCTOR=1 PYTHONUTF8=1
ExecStart=/bin/bash "$ROOT/scripts/start.sh"
Restart=on-failure
RestartSec=30
StandardOutput=null

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT"
  # 不开 linger 的话，用户登录后服务才会启动、退出登录后停止
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
    info "提示：现在要登录这个用户后服务才会启动。想开机就启动（不用登录），请运行："
    info "  sudo loginctl enable-linger $USER"
  fi
}

linux_disable() {
  if command -v systemctl >/dev/null 2>&1; then systemctl --user disable --now "$UNIT" 2>/dev/null || true; fi
  rm -f "$UNIT_FILE"
  if command -v systemctl >/dev/null 2>&1; then systemctl --user daemon-reload 2>/dev/null || true; fi
}

linux_restart() { systemctl --user restart "$UNIT"; }

# ---------- 命令 ----------
case "$(uname -s)" in
  Darwin) P=mac ;;
  Linux) P=linux ;;
  *) fail "不支持的系统：$(uname -s)" ;;
esac

case "${1:-}" in
  enable)
    [[ -x "$SERVER/.venv/bin/python" ]] || fail "还没安装，请先运行：bash \"$ROOT/scripts/install.sh\""
    "${P}_enable"
    info "已设置开机自启，服务正在后台启动"
    [[ "$P" == mac ]] && info "（登录这个 macOS 用户后自动启动；想开机就启动，请在 系统设置 → 用户与群组 里开启自动登录）"
    info "日志：$SERVER/tmp/server.log"
    info "取消：bash \"$ROOT/scripts/autostart.sh\" disable"
    ;;
  disable)
    "${P}_disable"
    info "已取消开机自启，后台服务已停止"
    ;;
  restart)
    "${P}_enabled" || fail "没有开启开机自启。手动启动的服务请在它的终端按 Ctrl+C 停止，再运行 bash \"$ROOT/scripts/start.sh\""
    "${P}_restart"
    info "后台服务已重启"
    ;;
  status)
    if ! "${P}_enabled"; then echo "未开启开机自启"; exit 1; fi
    pid="$("${P}_pid" || true)"
    if [[ -n "$pid" ]]; then echo "已开启开机自启，服务正在运行（进程 $pid）"
    else echo "已开启开机自启，但服务没有在运行。启动失败的原因见 $ERR_LOG 和 $SERVER/tmp/server.log"; fi
    ;;
  *)
    echo "用法：bash scripts/autostart.sh enable|disable|restart|status" >&2
    exit 2
    ;;
esac
