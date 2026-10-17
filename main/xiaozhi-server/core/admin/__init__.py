"""轻量管理后台：挂在 HTTP 服务（server.http_port）的 /admin 下，读写 data/.config.yaml。"""

from core.admin.admin_handler import AdminHandler


def register_admin_routes(app, config, ws_server=None):
    """在 aiohttp 应用上注册管理后台路由。"""
    AdminHandler(config, ws_server).register(app)
