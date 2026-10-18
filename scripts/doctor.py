"""环境自检，三个平台共用。用虚拟环境里的 Python 运行：

    main/xiaozhi-server/.venv/bin/python scripts/doctor.py          # macOS / Linux
    main\\xiaozhi-server\\.venv\\Scripts\\python scripts\\doctor.py   # Windows

检查项：核心依赖、插件能否导入、配置文件、所选模块的依赖、ffmpeg / opus、ASR 模型、
所选大模型（Claude Code 或 API Key）、端口是否空闲、局域网地址。有"异常"时退出码为 1。
"""

import argparse
import importlib
import os
import pkgutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "main" / "xiaozhi-server"
# 精简安装和完整安装都必须有的包；FunASR 等可选模块的依赖由"模块依赖"一项按所选模块检查
CORE_MODULES = ["opuslib_next", "onnxruntime", "sherpa_onnx", "edge_tts", "openai", "websockets",
                "aiohttp", "ruamel.yaml", "loguru"]
ICON = {"ok": "✅", "warn": "⚠️ ", "error": "❌"}


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ports", action="store_true", help="服务已在运行时跳过端口检查")
    args = parser.parse_args()

    os.chdir(SERVER_DIR)
    sys.path.insert(0, str(SERVER_DIR))
    results = []

    def add(name, level, detail=""):
        results.append((name, level, detail))

    # 1. 核心依赖
    missing = []
    for mod in CORE_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:
            missing.append(f"{mod}（{type(e).__name__}）")
    add("核心依赖", "error" if missing else "ok", "缺少 " + "、".join(missing) if missing else f"{len(CORE_MODULES)} 个均可导入")
    if missing:
        return report(results)

    # 插件在服务启动时全部导入，任何一个导入失败都会导致服务无法启动。
    # 同步上游后如果新增了插件依赖、而精简清单没有跟上，这里会直接指出缺了什么
    import plugins_func.functions as functions
    broken = []
    names = [m.name for m in pkgutil.iter_modules(functions.__path__)]
    for name in names:
        try:
            importlib.import_module(f"plugins_func.functions.{name}")
        except Exception as e:
            broken.append(f"{name}（{type(e).__name__}: {e}）")
    add("插件", "error" if broken else "ok",
        "导入失败：" + "、".join(broken) + "。请把缺少的包加入 requirements-hub-core.txt" if broken
        else f"{len(names)} 个均可导入")

    # 2. 配置文件
    cfg_path = SERVER_DIR / "data" / ".config.yaml"
    if not cfg_path.exists():
        add("配置文件", "error", f"缺少 {cfg_path}，请先运行安装脚本")
        return report(results)
    from core.admin import config_store
    try:
        merged = config_store.load_merged()
        selected = merged.get("selected_module") or {}
        bad = [f"{k}={v}" for k, v in selected.items()
               if k in config_store.SECTIONS and v not in (merged.get(k) or {})]
        add("配置文件", "error" if bad else "ok", f"选中的模块不存在：{', '.join(bad)}" if bad else str(cfg_path))
    except Exception as e:
        add("配置文件", "error", f"无法解析：{e}")
        return report(results)

    if config_store.entry_type(merged, "LLM") == "claude_cli":
        try:
            importlib.import_module("claude_agent_sdk")
            add("claude-agent-sdk", "ok", "已安装")
        except Exception as e:
            add("claude-agent-sdk", "error", f"未安装（{e}），请重新运行安装脚本")

    # 3. 与管理后台状态页相同的检查
    from core.admin.status import collect_status
    status = collect_status(merged, merged)
    for c in status["checks"]:
        add(c["name"], c["level"], c["detail"])

    # 4. 端口
    if not args.skip_ports:
        server = merged.get("server", {})
        for label, port in (("WebSocket 端口", int(server.get("port", 8000))),
                            ("HTTP 端口", int(server.get("http_port", 8003)))):
            ok = port_free(port)
            add(label, "ok" if ok else "error", f"{port} 空闲" if ok else f"{port} 已被占用，可能服务已经在运行")

    addr = status["addresses"]
    print(f"\n  本机 IP：  {addr['ip']}\n  OTA 地址： {addr['ota']}\n  管理后台： {addr['admin']}")
    return report(results)


def report(results) -> int:
    print("\n环境自检")
    for name, level, detail in results:
        print(f"  {ICON.get(level, '  ')} {name}：{detail}")
    errors = sum(1 for _, level, _ in results if level == "error")
    warns = sum(1 for _, level, _ in results if level == "warn")
    print(f"\n结果：{errors} 项异常，{warns} 项需要注意\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
