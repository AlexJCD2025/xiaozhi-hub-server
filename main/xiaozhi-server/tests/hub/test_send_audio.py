"""[xiaozhi-hub] 音频下发的预缓冲逻辑：每轮开头、设备缓冲快播空时各预缓冲一次，结束时按剩余缓冲等待。"""

import time
from types import SimpleNamespace

import pytest

from core.handle import sendAudioHandle as h


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, packet):
        self.sent.append(packet)


def make_conn(prebuffer=15):
    return SimpleNamespace(
        config={"tts_audio_prebuffer_count": prebuffer},
        sentence_id="s1",
        client_abort=False,
        websocket=FakeWS(),
        conn_from_mqtt_gateway=False,
        last_activity_time=0,
    )


@pytest.fixture
def conn():
    c = make_conn()
    yield c
    rc = getattr(c, "audio_rate_controller", None)
    if rc and rc.pending_send_task:
        rc.pending_send_task.cancel()


def controller(conn):
    """拿到控制器，并让入队的包只记录不发送（conftest 把 asyncio.sleep 改成了立即返回，后台会瞬间发完）。"""
    rc, fc = h._get_or_create_rate_controller(conn, 60, False)
    rc.pending_send_task.cancel()
    queued = []
    rc.add_audio = queued.append
    return rc, fc, queued


def frames(n):
    return [bytes([i % 256]) for i in range(n)]


async def test_turn_start_prebuffers_exactly_n(conn):
    rc, fc, queued = controller(conn)
    await h._send_audio_with_rate_control(conn, frames(40), rc, fc, 0)
    # 只直接发出 15 包，其余进入节奏队列（不再出现开头 30 包的双倍预缓冲）
    assert len(conn.websocket.sent) == 15
    assert len(queued) == 25


async def test_reburst_when_device_buffer_drained(conn):
    rc, fc, queued = controller(conn)
    await h._send_audio_with_rate_control(conn, frames(10), rc, fc, 0)  # 短句（如填充语）全部直接发出
    assert len(conn.websocket.sent) == 10 and not queued
    fc["play_end"] = time.monotonic() - 1  # 模拟：上一句已经播完，下一句才合成好
    await h._send_audio_with_rate_control(conn, frames(30), rc, fc, 0)
    assert len(conn.websocket.sent) == 25  # 新一句重新预缓冲完整的 15 包（不是上次剩下的 5 包）
    assert len(queued) == 15


async def test_no_extra_burst_while_device_still_buffered(conn):
    rc, fc, queued = controller(conn)
    await h._send_audio_with_rate_control(conn, frames(10), rc, fc, 0)
    # 设备里还有约 0.6 秒没播完，下一句紧接着到达：不重新预缓冲，避免缓冲越积越多
    await h._send_audio_with_rate_control(conn, frames(20), rc, fc, 0)
    assert len(conn.websocket.sent) == 15  # 只补完第一次预缓冲剩余的 5 包
    assert len(queued) == 15


async def test_play_end_tracks_sent_audio(conn):
    rc, fc, queued = controller(conn)
    t0 = time.monotonic()
    await h._send_audio_with_rate_control(conn, frames(15), rc, fc, 0)
    assert fc["play_end"] - t0 == pytest.approx(15 * 0.06, abs=0.05)


def test_prebuffer_count_config():
    assert h._pre_buffer_count(make_conn(20)) == 20
    assert h._pre_buffer_count(SimpleNamespace(config={})) == h.PRE_BUFFER_COUNT
    assert h._pre_buffer_count(make_conn("abc")) == h.PRE_BUFFER_COUNT
    assert h._pre_buffer_count(make_conn(0)) == 1


async def test_completion_waits_for_remaining_buffer(conn, monkeypatch):
    rc, fc, queued = controller(conn)
    conn.logger = SimpleNamespace(bind=lambda **k: SimpleNamespace(debug=lambda *a, **kw: None))
    fc["play_end"] = time.monotonic() + 1.5   # 设备里还有 1.5 秒没播完
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)
    monkeypatch.setattr(h.asyncio, "sleep", fake_sleep)
    await h._wait_for_audio_completion(conn)
    assert waited and waited[0] == pytest.approx(1.5 + 0.12, abs=0.05)
