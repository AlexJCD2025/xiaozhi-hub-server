import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


def _abort_reason(msg_json) -> str:
    if not isinstance(msg_json, dict):
        return ""
    return str(msg_json.get("reason") or "")


def _is_device_confirm_abort(msg_json) -> bool:
    """设备在播放时主动发来的 abort，才可能是点屏幕 / 按键确认。

    服务端 VAD 触发的打断没有这份 JSON。唤醒词打断在官方固件里会带
    reason=wake_word_detected，那种不能当成确认，否则喊唤醒词就会同意写入。
    """
    if not isinstance(msg_json, dict):
        return False
    return "wake" not in _abort_reason(msg_json).lower()


async def handleAbortMessage(conn: "ConnectionHandler", msg_json=None):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    # [xiaozhi-hub] Codex 正在等确认时，点屏幕 / 按键（设备 abort）表示同意，而不是打断。
    consumed = False
    if _is_device_confirm_abort(msg_json):
        hook = getattr(conn.llm, "handle_device_abort", None)
        if callable(hook):
            try:
                consumed = bool(hook(conn))
            except Exception as e:
                conn.logger.bind(tag=TAG).warning(f"设备确认钩子出错: {e}")
                consumed = False
    # claude_cli 向 Claude Code 发送中断；Codex 没被确认钩子吃掉时，这一轮转到后台
    if not consumed and callable(getattr(conn.llm, "abort", None)):
        conn.llm.abort(conn)
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")
