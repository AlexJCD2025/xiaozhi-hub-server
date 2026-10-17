"""管理后台的 HTTP 接口。

安全约定：
- 首次设置密码只允许在本机（127.0.0.1 / ::1）访问时进行，避免局域网里的其他人抢先设置。
- 登录后用 HttpOnly + SameSite=Strict 的 cookie 保持会话；写操作只接受 JSON 请求。
- API Key 只写不读：接口永远不返回完整的 Key。
"""

import asyncio
import ipaddress
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging
from core.admin import auth, config_store
from core.admin.status import collect_status
from core.utils.modules_initialize import initialize_modules

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


class AdminHandler:
    def __init__(self, config: dict, ws_server=None, config_path: Path = None):
        self.config = config
        self.ws_server = ws_server
        self.config_path = config_path
        self.sessions = auth.SessionStore()
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
                web.get("/admin/api/modules/{section}", self.handle_module_list),
                web.post("/admin/api/modules/{section}/select", self.handle_module_select),
                web.post("/admin/api/modules/{section}/entries/{name}", self.handle_module_update),
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
        config_store.set_password_hash(auth.hash_password(password), self.config_path)
        self.logger.bind(tag=TAG).info("管理后台密码已设置")
        return self._login_response(request, {"ok": True})

    async def handle_login(self, request: web.Request):
        body = await self._read_json(request)
        stored = config_store.get_password_hash(self.config_path)
        if not stored:
            return self._error("尚未设置密码", 409)
        if not auth.verify_password(str(body.get("password") or ""), stored):
            await asyncio.sleep(1)  # 减缓暴力尝试
            self.logger.bind(tag=TAG).warning(f"管理后台登录失败，来源 {request.remote}")
            return self._error("密码错误", 401)
        return self._login_response(request, {"ok": True})

    async def handle_logout(self, request: web.Request):
        self.sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = self._json({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/admin")
        return response

    # ---------- 模块配置（LLM / ASR / TTS / Intent） ----------

    def _merged(self) -> dict:
        if self.config_path:
            return config_store.load_merged(custom_path=self.config_path)
        return config_store.load_merged()

    def _section(self, request: web.Request) -> str:
        section = request.match_info["section"]
        if section not in config_store.SECTIONS:
            raise web.HTTPNotFound(text='{"ok": false, "error": "未知模块"}', content_type="application/json")
        return section

    async def handle_status(self, request: web.Request):
        self._require_login(request)
        loop = asyncio.get_running_loop()
        merged = self._merged()
        status = await loop.run_in_executor(None, collect_status, self.config, merged)
        return self._json({"ok": True, **status})

    async def handle_module_list(self, request: web.Request):
        self._require_login(request)
        section = self._section(request)
        data = config_store.describe_section(self._merged(), section)
        data["running"] = (self.config.get("selected_module") or {}).get(section)
        return self._json({"ok": True, **data})

    async def handle_module_update(self, request: web.Request):
        self._require_login(request)
        section = self._section(request)
        name = request.match_info["name"]
        body = await self._read_json(request)
        try:
            changed = config_store.update_entry(section, name, body, self._merged(), self.config_path)
        except KeyError:
            return self._error(f"没有名为 {name} 的配置", 404)
        except ValueError as e:
            return self._error(f"格式不对：{e}", 400)
        self.logger.bind(tag=TAG).info(f"管理后台更新了 {section}.{name}: {', '.join(changed) or '无变化'}")
        result = await self._apply(section) if changed else {"applied": True, "note": "没有修改"}
        return self._json({"ok": True, "changed": changed, **result})

    async def handle_module_select(self, request: web.Request):
        self._require_login(request)
        section = self._section(request)
        body = await self._read_json(request)
        name = str(body.get("name") or "")
        try:
            config_store.select_entry(section, name, self._merged(), self.config_path)
        except KeyError:
            return self._error(f"没有名为 {name} 的配置", 404)
        self.logger.bind(tag=TAG).info(f"管理后台切换 {section} 为: {name}")
        return self._json({"ok": True, **await self._apply(section)})

    async def _apply(self, section: str) -> dict:
        """把磁盘上的配置热加载到运行中的服务。

        直接修改共享的 config 字典，再按模块重建实例：
        - LLM、Intent：立即重建；
        - ASR：后台重新加载（本地模型需要几秒）；
        - TTS：每个连接各自创建，不用重建。
        已建立的设备连接继续使用旧实例，设备重新连接后才会用上新配置。
        """
        merged = self._merged()
        self.config[section] = merged.get(section, {})
        self.config.setdefault("selected_module", {})[section] = merged["selected_module"][section]
        if section == "TTS":
            return {"applied": True, "note": "设备重新连接后生效"}
        if self.ws_server is None:
            return {"applied": False, "note": "已保存，重启服务后生效"}
        flags = {"LLM": "init_llm", "Intent": "init_intent", "ASR": "init_asr"}
        attr = {"LLM": "_llm", "Intent": "_intent", "ASR": "_asr"}[section]
        key = {"LLM": "llm", "Intent": "intent", "ASR": "asr"}[section]
        try:
            loop = asyncio.get_running_loop()
            async with self.ws_server.config_lock:
                modules = await loop.run_in_executor(
                    None, lambda: initialize_modules(self.logger, self.config, **{flags[section]: True})
                )
                setattr(self.ws_server, attr, modules[key])
            return {"applied": True, "note": "已生效，设备重新连接后使用新配置"}
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"热加载 {section} 失败: {e}")
            return {"applied": False, "note": f"已保存，但加载失败：{e}。请检查配置后重试，或重启服务"}
