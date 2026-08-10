#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页面板的 HTTP 层。

这个文件取代了 `test_web_events.py`。上一版面板自己实现事件流，所以那边要测
`Last-Event-ID` 裁剪、全局 seq、代次隔离、订阅注册与历史快照的原子性 ——
那些机制现在整个不存在了：事件流归核心，面板只是把 HTTP 翻译成 CLI 调用。
对应的契约测试在 `test_api_events.py`。

这里剩下的就三件事：**鉴权**、**转发**、**别把异常甩给浏览器**。
用假 client，不起真 rloop。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rloopgui import web                       # noqa: E402
from rloopgui.contract import Contract         # noqa: E402
from rloopgui.errors import CoreFailed, CoreNotFound  # noqa: E402


class FakeClient:
    """记下每一次调用，返回预先摆好的东西。"""

    def __init__(self):
        self.calls = []
        self.contract = Contract({"api": 1, "rloop_version": "9.9.9"})
        self.events = [{"kind": "note", "seq": 1, "text": "第一条"},
                       {"kind": "run.end", "seq": 2, "text": "收工"}]
        self.raise_with = None

    def _note(self, name, *a, **kw):
        self.calls.append((name, a, kw))
        if self.raise_with:
            raise self.raise_with

    def loops(self, project=None):
        self._note("loops", project)
        return {"loops": [{"id": "L1", "status": "needs_work"}], "any_running": False}

    def loop(self, lid, rnd=None):
        self._note("loop", lid, rnd)
        return {"loop": {"id": lid}, "round": rnd or 1}

    def file(self, lid, what, rnd=None):
        self._note("file", lid, what, rnd)
        return {"what": what, "text": "内容", "round": rnd}

    def run(self, project, **opts):
        self._note("run", project, **opts)
        return {"started": True, "loop": "L1"}

    def stop(self, lid):
        self._note("stop", lid)
        return {"was_running": False, "killed": []}

    def follow(self, lid=None, rnd=None, since=0, state=True, project=None):
        self._note("follow", lid, rnd, since, state, project)
        yield from self.events


@pytest.fixture
def panel(tmp_path):
    client = FakeClient()
    web._client = client
    web._project = tmp_path
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, client
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(base, path, token=True):
    sep = "&" if "?" in path else "?"
    url = f"{base}{path}{sep}t={web.TOKEN}" if token else f"{base}{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def post(base, path, body):
    req = urllib.request.Request(
        f"{base}{path}?t={web.TOKEN}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


# ─────────── 鉴权 ───────────

@pytest.mark.parametrize("path", ["/", "/api/loops", "/api/loop", "/api/file",
                                  "/api/events", "/api/meta"])
def test_every_route_needs_the_token(panel, path):
    """本机上别的进程也能访问 127.0.0.1，所以每条路由都要挡。"""
    base, _ = panel
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{base}{path}", timeout=10)
    assert e.value.code == 404


def test_post_routes_need_the_token_too(panel):
    base, client = panel
    req = urllib.request.Request(f"{base}/api/run", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 404
    assert not client.calls, "没通过鉴权却已经调了核心"


# ─────────── 转发 ───────────

def test_it_forwards_the_project_it_was_started_with(panel, tmp_path):
    """面板起在哪个项目上，就只问那个项目 —— 不做任何隐式 fallback。"""
    base, client = panel
    got = get(base, "/api/loops")
    assert got["ok"] and got["data"]["loops"][0]["id"] == "L1"
    assert client.calls[0] == ("loops", (str(tmp_path),), {})


def test_round_is_passed_through_as_an_int(panel):
    base, client = panel
    get(base, "/api/loop?id=L1&round=3")
    assert client.calls[0] == ("loop", ("L1", 3), {})


def test_run_passes_the_whole_form(panel, tmp_path):
    base, client = panel
    post(base, "/api/run", {"focus": "只看并发", "max_rounds": 3, "new": True})
    name, args, kw = client.calls[0]
    assert name == "run" and args == (str(tmp_path),)
    assert kw == {"focus": "只看并发", "max_rounds": 3, "new": True}


def test_a_malformed_body_does_not_crash_the_handler(panel):
    """浏览器发来的东西不可信，但也不该让面板 500。"""
    base, client = panel
    req = urllib.request.Request(f"{base}/api/run?t={web.TOKEN}",
                                 data="{ 这不是 JSON".encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read().decode())
    assert body["ok"] is True        # 当成空表单处理
    assert client.calls[0][2] == {}


# ─────────── 出错 ───────────

def test_a_core_error_becomes_a_payload_not_a_traceback(panel):
    """核心报错时给浏览器一个能显示的东西，不是 500 加一堆栈。"""
    base, client = panel
    client.raise_with = CoreFailed("找不到 loop：X", code="not_found",
                                   hint="先看列表", detail={"id": "X"})
    got = get(base, "/api/loop?id=X")
    assert got["ok"] is False
    assert got["code"] == "not_found"
    assert got["msg"] == "找不到 loop：X"
    assert got["hint"] == "先看列表"
    assert got["detail"]["id"] == "X"


def test_a_missing_core_is_also_a_payload(panel):
    base, client = panel
    client.raise_with = CoreNotFound("找不到 rloop", hint="设 RLOOP_BIN")
    got = get(base, "/api/loops")
    assert got["ok"] is False and got["hint"] == "设 RLOOP_BIN"


# ─────────── 事件流 ───────────

def sse(base, path, wait=1.5) -> str:
    """连一次 SSE，读到静默就断开 —— 这个流不会自己结束。"""
    host, port = base.removeprefix("http://").split(":")
    req = (f"GET {path}&t={web.TOKEN} HTTP/1.1\r\nHost: {host}:{port}\r\n"
           f"Connection: close\r\n\r\n")
    buf = b""
    s = socket.create_connection((host, int(port)), timeout=5)
    try:
        s.sendall(req.encode())
        s.settimeout(wait)
        while len(buf) < 65536:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return buf.decode("utf-8", errors="replace")


def test_events_are_streamed_as_they_come(panel):
    base, _ = panel
    body = sse(base, "/api/events?loop=L1")
    assert ": connected" in body
    assert "第一条" in body and "收工" in body


def test_since_is_handed_to_the_core_untouched(panel):
    """重连时浏览器带上自己见过的最大 seq，核心据此裁剪。

    上一版这一步是面板自己做的（`Last-Event-ID` + 全局序号 + 200 行窗口），
    栽过两次。现在它只是一个转发的参数。
    """
    base, client = panel
    sse(base, "/api/events?loop=L1&round=2&since=7")
    name, args, kw = client.calls[0]
    assert name == "follow"
    assert args[0] == "L1" and args[1] == 2 and args[2] == 7


def test_no_loop_still_gets_the_state_stream(panel):
    """没选中 loop 时也要订阅 —— 面板的初始列表就是从这条流来的。"""
    base, client = panel
    sse(base, "/api/events?")
    name, args, kw = client.calls[0]
    assert name == "follow" and args[0] is None
    assert args[3] is True, "没给 loop 时也该要 state"


# ─────────── 页面 ───────────

def test_the_page_is_self_contained_and_carries_the_token(panel):
    """页面必须零外部引用 —— Browser 面板里没有网。"""
    base, _ = panel
    with urllib.request.urlopen(f"{base}/?t={web.TOKEN}", timeout=10) as r:
        html = r.read().decode()
    assert web.TOKEN in html, "token 没注入，页面调不动任何接口"
    assert "__TOKEN__" not in html, "占位符没替换干净"
    for bad in ("http://", "https://", "//cdn", "<script src"):
        assert bad not in html.replace("http://127.0.0.1", ""), \
            f"页面引用了外部资源：{bad}"


def test_the_page_has_no_way_to_write_a_response():
    """面板是观察者：没有输入框、没有提交回应的按钮。"""
    html = (REPO_ROOT / "rloopgui" / "page.html").read_text(encoding="utf-8")
    assert "<textarea" not in html, "页面上有编辑框——处理 findings 归有上下文的那一方"
    assert "/api/response" not in html
