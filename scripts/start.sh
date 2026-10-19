#!/usr/bin/env bash
# xiaozhi-hub-server 启动（macOS / Linux）：先快速自检，再启动服务。
# 用法：bash scripts/start.sh        跳过自检：SKIP_DOCTOR=1 bash scripts/start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/main/xiaozhi-server"
PY="$SERVER/.venv/bin/python"

[[ -x "$PY" ]] || { echo "未找到虚拟环境，请先运行：bash scripts/install.sh" >&2; exit 1; }

# 开着开机自启时服务已经在后台运行，再手动启动会抢同一个端口
if [[ "${HUB_AUTOSTART:-0}" != "1" ]] && status="$(bash "$ROOT/scripts/autostart.sh" status 2>/dev/null)" && [[ "$status" == *正在运行* ]]; then
  echo "$status" >&2
  echo "服务已经在后台运行，不用再手动启动。想在终端里看实时输出，先运行 bash \"$ROOT/scripts/autostart.sh\" disable" >&2
  exit 1
fi

# 开机自启（autostart.sh）时，启动失败的报错写在这个文件里；反复启动失败时不让它无限变大
if [[ -n "${AUTOSTART_ERR_LOG:-}" && -f "$AUTOSTART_ERR_LOG" ]] && (( $(wc -c <"$AUTOSTART_ERR_LOG") > 1048576 )); then
  : >"$AUTOSTART_ERR_LOG"
fi

if [[ "${SKIP_DOCTOR:-0}" != "1" ]]; then
  if ! "$PY" "$ROOT/scripts/doctor.py" 2>/dev/null; then
    if [[ -t 0 ]]; then
      read -r -p "自检有异常，仍要启动吗？[y/N] " answer
      [[ "$answer" =~ ^[Yy] ]] || exit 1
    else
      echo "自检有异常，已停止。设置 SKIP_DOCTOR=1 可跳过自检。" >&2
      exit 1
    fi
  fi
fi

echo "正在启动服务，按 Ctrl+C 停止 ..."
cd "$SERVER"
exec "$PY" app.py
