"""管理后台的 HTTP 接口。

安全约定：
- 首次设置密码只允许在本机（127.0.0.1 / ::1）访问时进行，避免局域网里的其他人抢先设置。
- 登录后用 HttpOnly + SameSite=Strict 的 cookie 保持会话；写操作只接受 JSON 请求。
- API Key 只写不读：接口永远不返回完整的 Key。

页面使用的接口（/admin/api/llm、asr、tts、intent 等）只暴露后台要管理的内容，并做取值校验。
"""

import asyncio
import functools
import ipaddress
import json
import time
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging
from core.admin import auth, catalog, config_store, connections, pages, pipeline
from core.admin.status import _claude_version, collect_status
from core.utils.modules_initialize import initialize_modules
from core.utils.textUtils import EMOJI_MAP

TAG = __name__
logger = setup_logging()
COOKIE_NAME = "xz_admin"
STATIC_DIR = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
EMOTION_EMOJI = {name: emoji for emoji, name in EMOJI_MAP.items()}


class AdminHandler:
    def __init__(self, config: dict, ws_server=None, config_path: Path = None):
        self.config = config
        self.ws_server = ws_server
        self.config_path = config_path
        self.sessions = auth.SessionStore()
        self._emotion_runs = {}  # session_id -> {"task", "index", "total", "emotion"}
        self._voice_samples = {}  # 音色 -> mp3 字节（试听缓存）
        self.logger = logger

    def register(self, app: web.Application) -> None:
        app.add_routes(
            [
                web.get("/admin", self.handle_index),
                web.get("/admin/", self.handle_index),
                web.get("/admin/api/state", self.handle_state),
                web.post("/admin/api/setup", self.handle_setup),
                web.post("/admin/api/login", self.handle_login),
                web.post("/admin/api/logout", self.handle_logout),
                web.get("/admin/api/status", self.handle_status),
                web.get("/admin/api/devices", self.handle_devices),
                web.post("/admin/api/devices/{session_id}/emotion", self.handle_emotion_one),
                web.post("/admin/api/devices/{session_id}/emotion-test", self.handle_emotion_test),
                web.post("/admin/api/devices/{session_id}/emotion-test/stop", self.handle_emotion_stop),
                web.get("/admin/api/llm", self.handle_llm),
                web.post("/admin/api/llm/claude", self.handle_llm_claude),
                web.post("/admin/api/llm/vendors/{vendor}", self.handle_llm_vendor),
                web.post("/admin/api/llm/vendors/{vendor}/test", self.handle_llm_vendor_test),
                web.post("/admin/api/llm/use", self.handle_llm_use),
                web.get("/admin/api/pipeline", self.handle_pipeline),
                web.get("/admin/api/asr", self.handle_asr),
                web.get("/admin/api/tts", self.handle_tts),
                web.post("/admin/api/tts", self.handle_tts_save),
                web.get("/admin/api/tts/preview", self.handle_tts_preview),
                web.get("/admin/api/intent", self.handle_intent),
                web.post("/admin/api/intent", self.handle_intent_save),
            ]
        )

    # ---------- 工具方法 ----------

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, headers=SECURITY_HEADERS)

    def _error(self, message, status):
        return self._json({"ok": False, "error": message}, status)

    @staticmethod
    def _is_local(request: web.Request) -> bool:
        try:
            return ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            return False

    @staticmethod
    async def _blocking(func, *args, **kwargs):
        """在线程池里执行同步操作（配置文件读写、密码哈希），不阻塞负责音频下发的主事件循环。"""
        return await asyncio.get_running_loop().run_in_executor(None, functools.partial(func, *args, **kwargs))

    def _logged_in(self, request: web.Request) -> bool:
        return self.sessions.valid(request.cookies.get(COOKIE_NAME))

    @staticmethod
    async def _read_json(request: web.Request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="JSON 格式错误")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        return body

    def _login_response(self, request: web.Request, data: dict):
        response = self._json(data)
        response.set_cookie(
            COOKIE_NAME,
            self.sessions.create(),
            httponly=True,
            samesite="Strict",
            path="/admin",
            max_age=self.sessions.ttl,
        )
        return response

    def _require_login(self, request: web.Request):
        if not self._logged_in(request):
            raise web.HTTPUnauthorized(
                text='{"ok": false, "error": "未登录"}', content_type="application/json"
            )

    # ---------- 页面与登录 ----------

    async def handle_index(self, request: web.Request):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers=SECURITY_HEADERS)

    async def handle_state(self, request: web.Request):
        return self._json(
            {
                "needs_setup": not config_store.get_password_hash(self.config_path),
                "is_local": self._is_local(request),
                "logged_in": self._logged_in(request),
            }
        )

    async def handle_setup(self, request: web.Request):
        if config_store.get_password_hash(self.config_path):
            return self._error("密码已设置，请直接登录", 409)
        if not self._is_local(request):
            return self._error("首次设置密码只能在运行服务的电脑上操作", 403)
        body = await self._read_json(request)
        password = str(body.get("password") or "")
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            return self._error(f"密码至少 {auth.MIN_PASSWORD_LENGTH} 位", 400)
        password_hash = await self._blocking(auth.hash_password, password)
        await self._blocking(config_store.set_password_hash, password_hash, self.config_path)
        self.logger.bind(tag=TAG).info("管理后台密码已设置")
        return self._login_response(request, {"ok": True})

    async def handle_login(self, request: web.Request):
        body = await self._read_json(request)
        stored = await self._blocking(config_store.get_password_hash, self.config_path)
        if not stored:
            return self._error("尚未设置密码", 409)
        if not await self._blocking(auth.verify_password, str(body.get("password") or ""), stored):
            await asyncio.sleep(1)  # 减缓暴力尝试
            self.logger.bind(tag=TAG).warning(f"管理后台登录失败，来源 {request.remote}")
            return self._error("密码错误", 401)
        return self._login_response(request, {"ok": True})

    async def handle_logout(self, request: web.Request):
        self.sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = self._json({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/admin")
        return response

    # ---------- 配置读取、切换与热加载 ----------

    def _merged(self) -> dict:
        if self.config_path:
            return config_store.load_merged(custom_path=self.config_path)
        return config_store.load_merged()

    def _running(self, section: str):
        return (self.config.get("selected_module") or {}).get(section)

    async def _apply_if_selected(self, section: str, name: str, changed: list) -> dict:
        """保存条目后：改的是正在使用的条目才热加载，否则只保存。"""
        if not changed:
            return {"applied": True, "note": "没有修改"}
        merged = await self._blocking(self._merged)
        if (merged.get("selected_module") or {}).get(section) != name:
            return {"applied": True, "note": "已保存"}
        return await self._apply(section)

    async def _switch(self, section: str, name: str) -> dict:
        """切换模块：先写配置再热加载；加载失败时把配置恢复成原来的选择，避免下次启动出问题。

        返回 {"ok": bool, "applied": bool, "note": str}。
        """
        merged = await self._blocking(self._merged)
        if name not in (merged.get(section) or {}):
            raise KeyError(name)
        old = (merged.get("selected_module") or {}).get(section)
        if old == name and self._running(section) == name and \
                (self.config.get(section) or {}).get(name) == merged[section][name]:
            return {"ok": True, "applied": True, "note": "已经在使用"}
        await self._blocking(config_store.select_entry, section, name, merged, self.config_path)
        result = await self._apply(section)
        if result.get("failed"):
            if old and old != name:
                await self._blocking(config_store.select_entry, section, old, merged, self.config_path)
            result["note"] = f"切换失败：{result['error']}。已恢复为原来的 {old or '配置'}"
            self.logger.bind(tag=TAG).warning(f"管理后台切换 {section} 为 {name} 失败，已恢复为 {old}")
            return {"ok": False, **result}
        self.logger.bind(tag=TAG).info(f"管理后台切换 {section} 为: {name}")
        return {"ok": True, **result}

    async def _apply(self, section: str) -> dict:
        """把磁盘上的配置热加载到运行中的服务。

        直接修改共享的 config 字典，再按模块重建实例：
        - LLM、Intent：立即重建；
        - ASR：后台重新加载（本地模型需要几秒）；
        - TTS：每个连接各自创建，不用重建。
        已建立的设备连接继续使用旧实例，设备重新连接后才会用上新配置。
        """
        merged = await self._blocking(self._merged)
        new_section = merged.get(section, {})
        new_selected = merged["selected_module"][section]

        def commit():
            self.config[section] = new_section
            self.config.setdefault("selected_module", {})[section] = new_selected

        if section == "TTS" or self.ws_server is None:
            commit()
            if section == "TTS":
                return {"applied": True, "note": "已保存，设备重新连接后生效"}
            return {"applied": False, "note": "已保存，重启服务后生效"}
        flags = {"LLM": "init_llm", "Intent": "init_intent", "ASR": "init_asr"}
        attr = {"LLM": "_llm", "Intent": "_intent", "ASR": "_asr"}[section]
        key = {"LLM": "llm", "Intent": "intent", "ASR": "asr"}[section]
        # 先用"新配置"创建实例；成功后才写入运行中的配置并替换实例，失败时服务保持原来的状态
        candidate = {**self.config, section: new_section,
                     "selected_module": {**self.config.get("selected_module", {}), section: new_selected}}
        try:
            async with self.ws_server.config_lock:
                modules = await self._blocking(initialize_modules, self.logger, candidate, **{flags[section]: True})
                commit()
                old = getattr(self.ws_server, attr, None)
                setattr(self.ws_server, attr, modules[key])
            self._retire(old, modules[key])
            return {"applied": True, "note": "已生效，设备重新连接后使用新配置"}
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"热加载 {section} 失败: {e}")
            error = str(e).strip().rstrip("。.") or type(e).__name__
            return {"applied": False, "failed": True, "error": error,
                    "note": f"已保存，但加载失败：{error}。服务仍在使用原来的配置，请检查后重试"}

    def _retire(self, old, new) -> None:
        """通知被替换的旧实例退役（如 claude_cli 在连接都关闭后结束后台线程）。
        已连接的设备仍在用旧实例，所以不能直接关掉；出错也不影响这次切换。"""
        if old is None or old is new or not callable(getattr(old, "shutdown", None)):
            return
        try:
            old.shutdown()
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"旧实例退役出错: {e}")

    # ---------- 通用条目编辑（脚本和测试使用） ----------

    async def handle_status(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        status = await self._blocking(collect_status, self.config, merged)
        return self._json({"ok": True, **status})

    # ---------- 大模型 ----------

    def _claude_install(self, merged: dict) -> dict:
        from core.providers.llm.claude_cli.claude_cli import find_claude_cli

        entry = (merged.get("LLM") or {}).get(pages.CLAUDE_CLI_ENTRY) or {}
        cli = find_claude_cli(entry.get("cli_path") or None)
        return {"installed": bool(cli), "path": cli or "", "version": _claude_version(cli) if cli else ""}

    async def handle_llm(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        data = pages.describe_llm(merged)
        data["claude"]["install"] = await self._blocking(self._claude_install, merged)
        data["running"] = self._running("LLM")
        return self._json({"ok": True, **data})

    async def handle_llm_claude(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_claude_cli, body, merged, self.config_path)
        except KeyError:
            return self._error("配置里没有 ClaudeCLI 条目", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        self.logger.bind(tag=TAG).info(f"管理后台更新了 Claude CLI 设置: {', '.join(changed) or '无变化'}")
        return self._json({"ok": True, "changed": changed,
                           **await self._apply_if_selected("LLM", pages.CLAUDE_CLI_ENTRY, changed)})

    async def handle_llm_vendor(self, request: web.Request):
        self._require_login(request)
        vendor = request.match_info["vendor"]
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_vendor, vendor, body, merged, self.config_path)
        except KeyError:
            return self._error("不支持这个厂商", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        self.logger.bind(tag=TAG).info(f"管理后台更新了 {vendor} 的 API 设置: {', '.join(changed) or '无变化'}")
        entry = catalog.VENDOR_BY_ID[vendor]["entry"]
        return self._json({"ok": True, "changed": changed, **await self._apply_if_selected("LLM", entry, changed)})

    @staticmethod
    def _test_llm(config: dict) -> dict:
        import openai
        from core.providers.llm.openai.openai import LLMProvider

        started = time.time()
        try:
            reply = LLMProvider(config).response_no_stream("你是语音助手，回答尽量简短。", "只回复一个字：好")
        except openai.AuthenticationError:
            return {"ok": False, "error": "API Key 不对或已失效"}
        except openai.NotFoundError:
            return {"ok": False, "error": "找不到这个模型或接口地址，请检查模型名称和接口地址"}
        except openai.RateLimitError:
            return {"ok": False, "error": "请求太频繁或额度用完了"}
        except (openai.APIConnectionError, openai.APITimeoutError):
            return {"ok": False, "error": "连不上接口地址，请检查地址和网络"}
        except openai.APIStatusError as e:
            return {"ok": False, "error": f"接口返回错误 {e.status_code}：{str(e.message)[:200]}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        return {"ok": True, "seconds": round(time.time() - started, 1), "reply": reply.strip()[:60]}

    async def handle_llm_vendor_test(self, request: web.Request):
        self._require_login(request)
        vendor = request.match_info["vendor"]
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            config = pages.vendor_test_config(vendor, body, merged)
        except KeyError:
            return self._error("不支持这个厂商", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        result = await self._blocking(self._test_llm, config)
        return self._json(result) if result["ok"] else self._error(result["error"], 400)

    async def handle_llm_use(self, request: web.Request):
        """切换到本地 CLI 或某个 API 厂商；意图识别自动跟着切。"""
        self._require_login(request)
        body = await self._read_json(request)
        merged = await self._blocking(self._merged)
        if body.get("kind") == "cli":
            name = pages.CLAUDE_CLI_ENTRY
        else:
            vendor = catalog.VENDOR_BY_ID.get(str(body.get("vendor") or ""))
            if vendor is None:
                return self._error("不支持这个厂商", 404)
            name = vendor["entry"]
            if not pages.describe_vendor(vendor, merged)["configured"]:
                return self._error(f"{vendor['label']} 还没配置好，请先填写接口地址、模型和 API Key 并保存", 400)
        try:
            result = await self._switch("LLM", name)
        except KeyError:
            return self._error(f"配置里没有 {name}", 404)
        if not result["ok"]:
            return self._error(result["note"], 400)

        merged = await self._blocking(self._merged)
        intent = await self._blocking(pages.desired_intent, merged, name, self.config_path)
        if config_store.entry_type(merged, "Intent") != intent:
            try:
                intent_result = await self._switch("Intent", pages.intent_entry_name(merged, intent))
                note = "意图识别已自动改为" + ("“不用插件”" if intent == "nointent" else "“用插件”")
                result["note"] += "；" + (note if intent_result["ok"] else intent_result["note"])
            except KeyError:
                result["note"] += f"；配置里没有 {intent}，请检查意图识别"
        return self._json(result)

    # ---------- 工作原理 ----------

    async def handle_pipeline(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._blocking(pipeline.describe, merged)})

    # ---------- 语音识别、语音合成 ----------

    async def handle_asr(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._blocking(pages.describe_asr, merged),
                           "running": self._running("ASR")})

    async def handle_tts(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **pages.describe_tts(merged), "running": self._running("TTS")})

    async def handle_tts_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_tts_voice, str(body.get("voice") or ""), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        name = (merged.get("selected_module") or {}).get("TTS")
        self.logger.bind(tag=TAG).info(f"管理后台更新了音色: {body.get('voice')}")
        return self._json({"ok": True, "changed": changed, **await self._apply_if_selected("TTS", name, changed)})

    async def handle_tts_preview(self, request: web.Request):
        """用 EdgeTTS 合成一句示例，返回 mp3 给浏览器播放。"""
        self._require_login(request)
        voice = request.query.get("voice", "")
        info = next((v for v in catalog.EDGE_VOICES if v["value"] == voice), None)
        if info is None:
            return self._error("不支持这个音色", 400)
        audio = self._voice_samples.get(voice)
        if audio is None:
            import edge_tts

            chunks = []
            try:
                async def synth():
                    async for chunk in edge_tts.Communicate(catalog.voice_sample(info["group"]), voice).stream():
                        if chunk["type"] == "audio":
                            chunks.append(chunk["data"])
                await asyncio.wait_for(synth(), timeout=20)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"试听合成失败 {voice}: {e}")
                return self._error("试听合成失败，请检查电脑能否访问微软的语音服务", 502)
            audio = b"".join(chunks)
            self._voice_samples[voice] = audio
        return web.Response(body=audio, content_type="audio/mpeg",
                            headers={**SECURITY_HEADERS, "Cache-Control": "private, max-age=86400"})

    # ---------- 意图识别 ----------

    async def handle_intent(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **pages.describe_intent(merged), "running": self._running("Intent")})

    async def handle_intent_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        mode = str(body.get("mode") or "")
        if mode not in catalog.INTENT_MODES:
            return self._error("意图识别只能选“不用插件”或“用插件”", 400)
        merged = await self._blocking(self._merged)
        if pages.llm_kind(merged) == "cli" and mode != "nointent":
            return self._error("使用 Claude Code CLI 时只能选“不用插件”", 400)
        notes = []
        try:
            if "functions" in body:
                changed = await self._blocking(pages.save_intent_plugins, body["functions"], merged, self.config_path)
                if changed and mode == "function_call":
                    merged = await self._blocking(self._merged)
                    name = pages.intent_entry_name(merged, "function_call")
                    if (merged.get("selected_module") or {}).get("Intent") == name:
                        notes.append((await self._apply("Intent"))["note"])
            if pages.llm_kind(merged) != "cli":
                await self._blocking(pages.remember_api_intent, mode, self.config_path)
            result = await self._switch("Intent", pages.intent_entry_name(merged, mode))
        except KeyError:
            return self._error(f"配置里没有 {mode}", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        if not result["ok"]:
            return self._error(result["note"], 400)
        if result["note"] == "已经在使用" and notes:
            result["note"] = notes[-1]
        return self._json(result)

    # ---------- 在线设备与表情测试 ----------

    def _emotion_progress(self, session_id: str):
        run = self._emotion_runs.get(session_id)
        if not run:
            return None
        task = run["task"]
        return {"running": not task.done(), "stopped": task.cancelled(), "index": run["index"], "total": run["total"],
                "emotion": run["emotion"], "emoji": EMOTION_EMOJI.get(run["emotion"], "")}

    async def handle_devices(self, request: web.Request):
        self._require_login(request)
        devices = connections.snapshot()
        for d in devices:
            d["emotion_test"] = self._emotion_progress(d["session_id"])
        return self._json({"ok": True, "devices": devices, "now": int(time.time()),
                           "emotions": [{"emotion": name, "emoji": emoji, "label": catalog.EMOTION_LABELS.get(name, name)}
                                        for emoji, name in EMOJI_MAP.items()]})

    @staticmethod
    async def _send_emotion(conn, emotion: str):
        await conn.websocket.send(json.dumps(
            {"type": "llm", "text": EMOTION_EMOJI[emotion], "emotion": emotion, "session_id": conn.session_id}
        ))

    def _online(self, session_id: str):
        conn = connections.get(session_id)
        return conn if conn is not None and getattr(conn, "websocket", None) is not None else None

    def _stop_emotion_test(self, session_id: str) -> bool:
        run = self._emotion_runs.get(session_id)
        if run and not run["task"].done():
            run["task"].cancel()
            return True
        return False

    async def handle_emotion_one(self, request: web.Request):
        """给设备单独发一个表情。"""
        self._require_login(request)
        session_id = request.match_info["session_id"]
        conn = self._online(session_id)
        if conn is None:
            return self._error("设备不在线", 404)
        body = await self._read_json(request)
        emotion = str(body.get("emotion") or "")
        if emotion not in EMOTION_EMOJI:
            return self._error("不支持这个表情", 400)
        self._stop_emotion_test(session_id)
        try:
            await self._send_emotion(conn, emotion)
        except Exception as e:
            return self._error(f"发送失败：{e}", 502)
        return self._json({"ok": True})

    async def handle_emotion_test(self, request: web.Request):
        """依次给设备发送全部情绪消息，用来查看固件实际支持哪些表情。"""
        self._require_login(request)
        session_id = request.match_info["session_id"]
        conn = self._online(session_id)
        if conn is None:
            return self._error("设备不在线", 404)
        body = await self._read_json(request)
        try:
            interval = min(max(float(body.get("interval") or 2), 0.5), 10)
        except (TypeError, ValueError):
            return self._error("interval 需要是数字（秒）", 400)
        self._stop_emotion_test(session_id)
        sequence = [{"emoji": e, "emotion": name} for e, name in EMOJI_MAP.items()]
        run = {"index": 0, "total": len(sequence), "emotion": ""}

        async def play():
            try:
                for i, item in enumerate(sequence, 1):
                    run["index"], run["emotion"] = i, item["emotion"]
                    await self._send_emotion(conn, item["emotion"])
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 测试中途设备断开等
                self.logger.bind(tag=TAG).info(f"表情测试已中止：{e}")

        run["task"] = asyncio.get_running_loop().create_task(play())
        self._emotion_runs = {sid: r for sid, r in self._emotion_runs.items() if connections.get(sid) is not None}
        self._emotion_runs[session_id] = run
        self.logger.bind(tag=TAG).info(f"开始表情测试：设备 {conn.device_id}，共 {len(sequence)} 种，间隔 {interval} 秒")
        return self._json({"ok": True})

    async def handle_emotion_stop(self, request: web.Request):
        self._require_login(request)
        self._stop_emotion_test(request.match_info["session_id"])
        return self._json({"ok": True})
