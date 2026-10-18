"""[xiaozhi-hub] 音频下发的预缓冲逻辑：队列发空时把设备缓冲补到 N 包（每轮开头、每句开头），结束时按剩余缓冲等待。"""

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


async def test_top_up_to_n_when_next_sentence_arrives_early(conn):
    rc, fc, queued = controller(conn)
    await h._send_audio_with_rate_control(conn, frames(10), rc, fc, 0)
    # 设备里还有约 0.6 秒（10 包）没播完，下一句紧接着到达：只补 5 包到 15 包，缓冲不会越积越多
    await h._send_audio_with_rate_control(conn, frames(20), rc, fc, 0)
    assert len(conn.websocket.sent) == 15
    assert len(queued) == 15


async def test_top_up_when_device_buffer_low_but_not_empty(conn):
    rc, fc, queued = controller(conn)
    await h._send_audio_with_rate_control(conn, frames(30), rc, fc, 0)
    queued.clear()
    # 上一句发完后，设备只剩约 0.24 秒（4 包）：补 11 包，整句播放期间都保持约 15 包余量。
    # 旧逻辑只在快播空（不到 1 包）时才补，这一句会只剩 0.24 秒余量，Wi-Fi 一抖就断续
    fc["play_end"] = time.monotonic() + 4 * 0.06
    await h._send_audio_with_rate_control(conn, frames(30), rc, fc, 0)
    assert len(conn.websocket.sent) == 15 + 11
    assert len(queued) == 19


async def test_no_burst_when_device_already_full(conn):
    rc, fc, queued = controller(conn)
    fc["play_end"] = time.monotonic() + 20 * 0.06  # 设备里已经有 20 包，超过设定的 15 包
    await h._send_audio_with_rate_control(conn, frames(10), rc, fc, 0)
    assert not conn.websocket.sent and len(queued) == 10


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
