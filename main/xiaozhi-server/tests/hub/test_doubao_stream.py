"""豆包流式语音识别：新旧控制台两种鉴权、热词直传、占位文本不发送、日志里不出现完整 token。"""

import json

from core.providers.asr import doubao_stream
from core.utils.modules_initialize import initialize_asr

UPSTREAM = {  # config.yaml 里 DoubaoStreamASRV2 的占位值
    "type": "doubao_stream", "appid": "你的火山引擎语音合成服务appid", "access_token": "你的火山引擎语音合成服务access_token",
    "api_key": "你的火山引擎新版控制台API Key", "resource_id": "volc.seedasr.sauc.duration",
    "boosting_table_name": "（选填）你的热词文件名称", "correct_table_name": "（选填）你的替换词文件名称",
}


def provider(**overrides):
    return doubao_stream.ASRProvider({**UPSTREAM, **overrides}, True)


def test_new_console_api_key():
    p = provider(api_key="volc-key", hotwords=["Claude", "GitHub"])
    headers = p.token_auth()
    assert headers["X-Api-Key"] == "volc-key" and "X-Api-App-Key" not in headers
    req = p.construct_request("r-1")
    assert "app" not in req  # 官方请求参数里没有 app，新版鉴权只看请求头
    # 占位的热词表名不发，后台的热词放在 context 里
    assert req["request"]["corpus"] == {"context": json.dumps(
        {"hotwords": [{"word": "Claude"}, {"word": "GitHub"}]}, ensure_ascii=False)}


def test_old_console_app_id_and_masked_log():
    p = provider(api_key="", appid="123456", access_token="tok-secret-value", boosting_table_name="my-table")
    assert p.token_auth()["X-Api-App-Key"] == "123456" and p.token_auth()["X-Api-Access-Key"] == "tok-secret-value"
    req = p.construct_request("r-2")
    assert req["app"] == {"appid": "123456", "token": "tok-secret-value"}
    assert req["request"]["corpus"] == {"boosting_table_name": "my-table"}  # 没有热词时不带 context
    assert "tok-secret-value" not in json.dumps(p._loggable(req))


def test_hotwords_truncated_to_limit():
    words = [f"word{i:03d}" for i in range(40)]
    p = provider(api_key="k", hotwords=words)
    assert 0 < len(p.hotwords) < len(words) and p.hotwords == words[:len(p.hotwords)]


def test_initialize_asr_passes_shared_hotwords():
    config = {"selected_module": {"ASR": "DoubaoStreamASRV2"}, "ASR": {"DoubaoStreamASRV2": {**UPSTREAM, "api_key": "k"}},
              "asr_hotwords": ["StackChan"], "call_names": {"models": {"ClaudeCLI": ["cloud"]}}}
    asr = initialize_asr(config)
    assert asr.hotwords == ["Claude", "StackChan"]  # 呼名的模型名在前
    assert asr.end_window_size == 200  # 没有 VAD 配置时用条目里的值


def test_end_window_follows_vad_silence():
    """判停时长跟后台"说完停顿多久算一句"一致，不用上游写死的 200ms。"""
    config = {"selected_module": {"ASR": "DoubaoStreamASRV2", "VAD": "SileroVAD"},
              "ASR": {"DoubaoStreamASRV2": {**UPSTREAM, "api_key": "k", "end_window_size": 200}},
              "VAD": {"SileroVAD": {"type": "silero", "min_silence_duration_ms": 1000}}}
    asr = initialize_asr(config)
    assert asr.end_window_size == 1000 and asr.construct_request("r")["request"]["end_window_size"] == 1000


async def test_check_connection_against_local_server(monkeypatch):
    """测试连接：Key 对时通过；被拒绝（401）、初始化返回错误、连不上时给出能看懂的原因。"""
    import websockets

    init_reply = {"code": None}

    def reply(payload: dict) -> bytes:
        data = json.dumps(payload).encode()
        return bytes([0x11, 0x91, 0x10, 0x00]) + (1).to_bytes(4, "big") + len(data).to_bytes(4, "big") + data

    def error_reply(code: int) -> bytes:
        data = json.dumps({"error": "bad resource"}).encode()
        return bytes([0x11, 0xF0, 0x10, 0x00]) + code.to_bytes(4, "big") + len(data).to_bytes(4, "big") + data

    def check_auth(connection, request):
        if request.headers.get("X-Api-Key") != "good":
            return connection.respond(401, '{"error":"Invalid X-Api-Key"}')

    async def handler(ws):
        await ws.recv()
        await ws.send(error_reply(init_reply["code"]) if init_reply["code"] else reply({"result": {"text": ""}}))

    async with websockets.serve(handler, "127.0.0.1", 0, process_request=check_auth) as server:
        port = server.sockets[0].getsockname()[1]
        init = doubao_stream.ASRProvider.__init__

        def local(self, *a, **k):
            init(self, *a, **k)
            self.ws_url = f"ws://127.0.0.1:{port}"
        monkeypatch.setattr(doubao_stream.ASRProvider, "__init__", local)

        assert (await doubao_stream.check_connection({**UPSTREAM, "api_key": "good"}))["ok"] is True
        r = await doubao_stream.check_connection({**UPSTREAM, "api_key": "wrong"})
        assert r["ok"] is False and "鉴权失败（401）" in r["error"] and "Invalid X-Api-Key" in r["error"]
        init_reply["code"] = 45000001
        r = await doubao_stream.check_connection({**UPSTREAM, "api_key": "good"})
        assert r["ok"] is False and "45000001" in r["error"]

    r = await doubao_stream.check_connection({**UPSTREAM, "api_key": "good"}, timeout=2)  # 服务已关闭
    assert r["ok"] is False and "连不上" in r["error"]
