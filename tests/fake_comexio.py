"""A minimal in-process Comexio IO-Server for client tests (real HTTP, real RSA login)."""

import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.common import load_fixture

USERNAME = "admin"
PASSWORD = "sécret"  # non-ASCII but ISO-8859-1, like the real login form allows
SALT = "Salz"
SESSION_COOKIE = "PHPSESSID"
SESSION_VALUE = "logged-in"

LOGIN_PATH = "/board/home/login/"
ADMIN_PATH = "/admin/"
FUNCTION_MODULE_PATH = "/admin/function_function_module/home"
KNX_CATALOG_PATH = "/admin/knx_one_wire/knx/"
DASHBOARD_REFRESH_PATH = "/board/dashboard/refresh/"
LOAD_ELEMENTS_PATH = "/admin/function_function_module/loadelements/"
LOAD_ALL_ELEMENTS_PATH = "/admin/function_function_module/loadallelements"
BUS_WORKLOAD_PATH = "/admin/in_output/inoutputinfo"
EXTENSION_FIRMWARE_PATH = "/admin/extension/checkextension_fwupdate/"

_LOGIN_PAGE = "<html><body><form>Anmeldung</form></body></html>"
_ADMIN_PAGE = """<html><body><script>
var $ioTypes = {"1": {"binary": true, "min": 0, "max": 1, "unit": ""}};
var $IOInputTypes = {"1": {"input": true}};
</script></body></html>"""

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# One key for the whole test run: generating a 2048-bit key per test would slow the suite down.
_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def json_response(payload: Any, status: int = 200) -> web.Response:
    """A JSON body served as text/html, like Comexio's own XHR endpoints."""
    return web.Response(text=json.dumps(payload), status=status, content_type="text/html")


class FakeComexio:
    """Serves the Comexio endpoints the client reads; tests override single routes via .routes."""

    def __init__(self) -> None:
        self._key = _RSA_KEY
        self.logins: list[dict[str, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.decrypted_blocks: list[str] = []
        self.routes: dict[tuple[str, str], Handler] = {
            ("POST", LOGIN_PATH): self._login,
            ("GET", ADMIN_PATH): self._admin,
            ("GET", FUNCTION_MODULE_PATH): self._page(load_fixture("function_module_page.html")),
        }
        self.server = TestServer(self._app(), host="127.0.0.1")

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.port}"

    def serve(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def serve_json(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        async def handler(_request: web.Request) -> web.Response:
            return json_response(payload, status)

        self.serve(method, path, handler)

    def serve_text(self, method: str, path: str, text: str, status: int = 200) -> None:
        self.serve(method, path, self._page(text, status))

    def _app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        return app

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path))
        handler = self.routes.get((request.method, request.path))
        if handler is None:
            raise web.HTTPNotFound
        return await handler(request)

    @staticmethod
    def _page(text: str, status: int = 200) -> Handler:
        async def handler(_request: web.Request) -> web.Response:
            return web.Response(text=text, status=status, content_type="text/html")

        return handler

    async def _login(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        if form.get("login_keys") == "true":
            numbers = self._key.public_key().public_numbers()
            return json_response(
                {
                    "salt": base64.b64encode(SALT.encode("iso-8859-1")).decode(),
                    "modulus": f"{numbers.n:x}",
                    "exponent": f"{numbers.e:x}",
                }
            )
        self.logins.append(form)
        self.decrypted_blocks = [
            self._key.decrypt(bytes.fromhex(block), padding.PKCS1v15()).decode("iso-8859-1")
            for block in form["password"].split(" ")
        ]
        response = web.Response(text="", content_type="text/html")
        with_password = self.decrypted_blocks[0]
        if form.get("username") == USERNAME and with_password.startswith(SALT) and with_password.endswith(PASSWORD):
            response.set_cookie(SESSION_COOKIE, SESSION_VALUE)
        return response

    async def _admin(self, request: web.Request) -> web.Response:
        logged_in = request.cookies.get(SESSION_COOKIE) == SESSION_VALUE
        return web.Response(text=_ADMIN_PAGE if logged_in else _LOGIN_PAGE, content_type="text/html")
