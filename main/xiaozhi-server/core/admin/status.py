"""管理后台状态页：环境自检与当前运行信息。所有检查都是只读、快速的。"""

import ctypes.util
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config.config_loader import get_project_dir
from core.admin import config_store
from core.utils.util import get_local_ip

_cli_cache = {}  # {cli_path: (检查时间, 版本文本)}


def _claude_version(cli_path: str) -> str:
    cached = _cli_cache.get(cli_path)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    try:
        out = subprocess.run([cli_path, "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=15)
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as e:
        version = f"运行失败: {e}"
    _cli_cache[cli_path] = (time.time(), version)
    return version


def _check(name, ok, detail="", level=None):
    return {"name": name, "level": level or ("ok" if ok else "error"), "detail": detail}


def collect_status(config: dict, merged: dict) -> dict:
    """config 是服务正在使用的配置，merged 是磁盘上的最新配置。"""
    ip = get_local_ip()
    server = config.get("server", {})
    http_port = int(server.get("http_port", 8003))
    ws_port = int(server.get("port", 8000))
    selected = dict(merged.get("selected_module") or {})
    running = dict(config.get("selected_module") or {})

    checks = []
    checks.append(_check("Python", sys.version_info[:2] == (3, 10), sys.version.split()[0],
                         level="ok" if sys.version_info[:2] == (3, 10) else "warn"))
    ffmpeg = shutil.which("ffmpeg")
    checks.append(_check("ffmpeg", bool(ffmpeg), ffmpeg or "未找到，请安装 ffmpeg"))
    opus = ctypes.util.find_library("opus")
    try:
        import opuslib_next  # noqa: F401
        opus_ok = True
    except Exception as e:
        opus_ok, opus = False, f"{opus or ''} 加载失败: {e}"
    checks.append(_check("opus", opus_ok, opus or "已加载"))

    # 语音识别模型（只检查本地 FunASR 的模型文件）
    asr_entry = (merged.get("ASR") or {}).get(selected.get("ASR"), {})
    if asr_entry.get("type") == "fun_local":
        model = Path(get_project_dir()) / asr_entry.get("model_dir", "models/SenseVoiceSmall") / "model.pt"
        size = model.stat().st_size if model.exists() else 0
        checks.append(_check("语音识别模型", size > 100 * 1024 * 1024,
                             f"{model.name} {size // (1024 * 1024)}MB" if size else f"缺少 {model}"))

    # 大模型
    llm_type = config_store.entry_type(merged, "LLM")
    llm_entry = (merged.get("LLM") or {}).get(selected.get("LLM"), {})
    if llm_type == "claude_cli":
        from core.providers.llm.claude_cli.claude_cli import find_claude_cli

        cli = find_claude_cli(llm_entry.get("cli_path") or None)
        checks.append(_check("Claude Code", bool(cli),
                             f"{_claude_version(cli)}（{cli}）" if cli else "未找到 claude，请先安装 Claude Code 并登录"))
        intent_type = config_store.entry_type(merged, "Intent")
        if intent_type != "nointent":
            checks.append(_check("意图识别", False, f"使用 Claude CLI 时应设为 nointent，当前为 {intent_type}",
                                 level="warn"))
    else:
        secrets = [k for k in llm_entry if config_store.SECRET_FIELD.search(k)]
        missing = [k for k in secrets if config_store.is_placeholder(llm_entry.get(k))]
        if secrets:
            checks.append(_check("大模型密钥", not missing,
                                 "已填写" if not missing else f"{selected.get('LLM')} 未填写 {', '.join(missing)}"))
        if llm_type == "openai" and config_store.entry_type(merged, "Intent") == "function_call" and \
                "claude" in str(llm_entry.get("model_name", "")).lower():
            checks.append(_check("意图识别", False, "Claude 在 function_call 模式下表情和回复长度都有问题，建议用 nointent",
                                 level="warn"))

    pending = {k: v for k, v in selected.items() if running.get(k) != v and k in config_store.SECTIONS}
    return {
        "addresses": {
            "ip": ip,
            "ota": f"http://{ip}:{http_port}/xiaozhi/ota/",
            "websocket": f"ws://{ip}:{ws_port}/xiaozhi/v1/",
            "admin": f"http://{ip}:{http_port}/admin",
        },
        "modules": {k: {"selected": selected.get(k), "type": config_store.entry_type(merged, k),
                        "running": running.get(k)} for k in config_store.SECTIONS},
        "pending": pending,
        "checks": checks,
    }
