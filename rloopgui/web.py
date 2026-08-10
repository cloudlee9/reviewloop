#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页面板。

只绑 127.0.0.1，随机端口 + 随机 token。页面自包含（零外部引用），
所以在 Claude Code 的 Browser 面板里也能直接开。

**这一版比上一版少了一大堆东西**，都是因为进度和进程管理都归核心了：

| 没有了 | 因为 |
|---|---|
| `Popen` / `pump` 线程 / `kill_pgid` | 起进程归 `api run`，停归 `api stop` |
| 全局 `_seq` + `Last-Event-ID` 裁剪补发 | 核心的事件自带 seq，`--since` 幂等 |
| `gen` 代次隔离 | 每个 SSE 连接自己一个 follower，天然不串 |
| `_run_lock` / `_sub_lock` 与它们的锁序 | 没有共享可变状态了 |
| `_running["lines"]` 无上限内存缓冲 | 进度在磁盘上，要多少读多少 |
| `STAT` / `SEV` / 退出码映射表 | 配色随数据一起来（`status_class` 等） |

剩下的就是一个把 HTTP 翻译成 CLI 调用的转接头。
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import CoreFailed, GuiError

TOKEN = secrets.token_urlsafe(18)
PAGE_FILE = Path(__file__).resolve().parent / "page.html"

# serve() 填进去，Handler 里用
_client = None
_project = None


def page_html() -> str:
    return PAGE_FILE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass            # 别把每个请求都刷到终端上

    # --- 基础 ---

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _authed(self, qs: dict) -> bool:
        return qs.get("t", [None])[0] == TOKEN

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def _guarded(self, fn):
        """把核心的错误翻成 HTTP，不要把 traceback 甩给浏览器。"""
        try:
            self._json({"ok": True, "data": fn()})
        except CoreFailed as e:
            self._json({"ok": False, "code": e.code, "msg": e.message,
                        "hint": e.hint, "detail": e.detail}, 200)
        except GuiError as e:
            self._json({"ok": False, "code": "gui", "msg": e.message,
                        "hint": e.hint}, 200)

    # --- 路由 ---

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/":
            if not self._authed(qs):
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, page_html().encode(), "text/html; charset=utf-8")
            return

        if not self._authed(qs):
            self._send(404, b"not found", "text/plain")
            return

        if u.path == "/api/meta":
            self._guarded(lambda: {
                "contract": _client.contract.meta,
                "matches": _client.contract.matches,
                "note": _client.contract.mismatch_note,
                "project": str(_project),
            })
        elif u.path == "/api/loops":
            self._guarded(lambda: _client.loops(str(_project)))
        elif u.path == "/api/loop":
            lid = qs.get("id", [""])[0]
            rnd = qs.get("round", [None])[0]
            self._guarded(lambda: _client.loop(lid, int(rnd) if rnd else None))
        elif u.path == "/api/file":
            lid = qs.get("id", [""])[0]
            what = qs.get("what", ["diff"])[0]
            rnd = qs.get("round", [None])[0]
            self._guarded(lambda: _client.file(lid, what, int(rnd) if rnd else None))
        elif u.path == "/api/events":
            self._sse(qs)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authed(qs):
            self._send(404, b"not found", "text/plain")
            return
        body = self._body()

        if u.path == "/api/run":
            self._guarded(lambda: _client.run(str(_project), **body))
        elif u.path == "/api/stop":
            self._guarded(lambda: _client.stop(body.get("id") or ""))
        else:
            self._send(404, b"not found", "text/plain")

    # --- SSE ---

    def _sse(self, qs: dict):
        """一条 SSE = 一个 follower 子进程。

        断线重连时浏览器把自己见过的最大 seq 作为 `since` 带上来，核心只补
        没见过的那些。这就是上一版整套 `Last-Event-ID` + 全局序号 + 代次隔离
        想做的事 —— 现在是核心的一个参数。
        """
        lid = qs.get("loop", [""])[0] or None
        rnd = qs.get("round", [None])[0]
        since = int(qs.get("since", ["0"])[0] or 0)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()

        try:
            for ev in _client.follow(lid, int(rnd) if rnd else None, since,
                                     state=True, project=str(_project)):
                payload = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass        # 浏览器关了标签页，follower 会在 finally 里被收掉


def serve(client, project: Path, port: int = 0, open_browser: bool = True) -> int:
    global _client, _project
    _client, _project = client, project

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    real_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{real_port}/?t={TOKEN}"

    # flush=True：输出被重定向时 stdout 是块缓冲的，`rloop web > log` 会看不到
    # 地址 —— 而地址里带着 token，看不到就进不去。
    print(f"rloop 面板：{url}", flush=True)
    print(f"项目：{project}", flush=True)
    if not client.contract.matches:
        print(client.contract.mismatch_note, flush=True)
    print("Ctrl-C 退出", flush=True)

    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
