"""[xiaozhi-hub] 长时间没说话时的告别：配置了固定告别语就直接说这句，不发给大模型。"""

import time
from types import SimpleNamespace

from core.handle import receiveAudioHandle as h


def make_conn(end_prompt):
    logger = SimpleNamespace(info=lambda *a, **k: None)
    return SimpleNamespace(
        config={"close_connection_no_voice_time": 120, "end_prompt": end_prompt},
        last_activity_time=(time.time() - 300) * 1000,  # 5 分钟没说话
        close_after_chat=False, client_abort=True, sentence_id=None,
        logger=SimpleNamespace(bind=lambda **k: logger),
    )


def patch(monkeypatch):
    calls = {"speak": [], "chat": []}

    async def fake_chat(conn, text):
        calls["chat"].append(text)

    monkeypatch.setattr(h, "speak_txt", lambda conn, text: calls["speak"].append(text))
    monkeypatch.setattr(h, "startToChat", fake_chat)
    return calls


async def test_fixed_farewell_skips_llm(monkeypatch):
    calls = patch(monkeypatch)
    c = make_conn({"enable": True, "reply": "我先休息啦", "prompt": "请你以时间过得真快开头"})
    await h.no_voice_close_connect(c, have_voice=False)
    assert calls == {"speak": ["我先休息啦"], "chat": []}
    assert c.close_after_chat is True and c.sentence_id  # 说完这句就断开


async def test_without_reply_falls_back_to_llm(monkeypatch):
    calls = patch(monkeypatch)
    c = make_conn({"enable": True, "prompt": "请你以时间过得真快开头"})
    await h.no_voice_close_connect(c, have_voice=False)
    assert calls == {"speak": [], "chat": ["请你以时间过得真快开头"]}


async def test_recent_voice_does_nothing(monkeypatch):
    calls = patch(monkeypatch)
    c = make_conn({"enable": True, "reply": "我先休息啦"})
    c.last_activity_time = time.time() * 1000
    await h.no_voice_close_connect(c, have_voice=False)
    assert calls == {"speak": [], "chat": []} and c.close_after_chat is False
