#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rloop CLI 的客户端。

**这是整个包里唯一知道 rloop 存在的文件。** 别处不许 import rloop、不许拼
`.review-loops` 路径、不许读 `loop.json`。腐烂总是从「负载里既然给了绝对路径，
我 Path(x).read_text() 一下更省事」开始的，那种捷径任何 import 检查都抓不到，
所以 tests/test_gui_isolation.py 直接扫这些字样。

依赖方向：面板 --（子进程）--> rloop。反过来核心也以进程方式拉起面板
（`rloop web` → `os.execve(python -m rloopgui web)`），和 `cmd_logs -f` 走
`os.execvp("tail", ...)` 是同一个手法，不构成 import 依赖。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .contract import API, Contract
from .errors import CoreFailed, CoreNotFound, CoreUnintelligible


def find_core() -> list:
    """找到 rloop，返回可以直接 Popen 的 argv 前缀。

    顺序：`$RLOOP_BIN`（核心自己拉起面板时会设）→ PATH 上的 `rloop` →
    同一个仓库里相邻的 `rloop.py`。
    """
    env_bin = os.environ.get("RLOOP_BIN")
    if env_bin:
        p = Path(env_bin)
        if not p.exists():
            raise CoreNotFound(
                f"RLOOP_BIN 指向的文件不存在：{env_bin}",
                hint="改成 rloop.py 的真实路径，或者把这个变量删掉让面板自己找")
        return [sys.executable, str(p)] if p.suffix == ".py" else [str(p)]

    on_path = shutil.which("rloop")
    if on_path:
        return [on_path]

    sibling = Path(__file__).resolve().parent.parent / "rloop.py"
    if sibling.exists():
        return [sys.executable, str(sibling)]

    raise CoreNotFound(
        "找不到 rloop 可执行文件。",
        hint="把它放进 PATH，或者设 RLOOP_BIN=/path/to/rloop.py")


class Client:
    """对 rloop CLI 的每一次调用都从这里过。

    调用总表就是下面这些方法，没有第八个。契约里**没有任何写产物的 verb**，
    尤其没有写 response.md 的 —— 那不是遗漏，是「面板是观察者」这条约束的执行
    方式：能力不存在，任何语言写的面板都长不出「处理 findings」的按钮。
    """

    def __init__(self, argv_prefix: list | None = None, timeout: float = 120.0):
        self.prefix = argv_prefix or find_core()
        self.timeout = timeout
        self.contract = Contract()
        self._lock = threading.Lock()

    # --- 底层 ---

    def _run(self, argv: list, timeout: float | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run(self.prefix + argv, capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except FileNotFoundError as e:
            raise CoreNotFound(f"起不来 rloop：{e}",
                               hint="设 RLOOP_BIN=/path/to/rloop.py") from e
        except subprocess.TimeoutExpired as e:
            raise CoreFailed(f"rloop 超过 {timeout or self.timeout:.0f} 秒没有返回",
                             code="timeout") from e
        return p.returncode, p.stdout, p.stderr

    def call(self, verb: str, *args: str, timeout: float | None = None) -> dict:
        """跑一个 api verb，返回 envelope 里的 data。失败抛 CoreFailed。"""
        argv = ["api"]
        if verb != "meta":
            argv += ["--api", str(API)]
        argv += [verb, *args]
        rc, out, err = self._run(argv, timeout)

        if not out.strip():
            raise CoreUnintelligible(
                f"rloop api {verb} 什么都没输出（退出码 {rc}）",
                hint="这多半不是 rloop，或者版本太老没有 api 子命令",
                detail={"stderr": err[:2000]})
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as e:
            raise CoreUnintelligible(
                f"rloop api {verb} 的输出不是 JSON",
                hint="核对一下 RLOOP_BIN 指的是不是 rloop",
                detail={"stdout": out[:2000]}) from e

        if not payload.get("ok"):
            e = payload.get("error") or {}
            raise CoreFailed(e.get("message") or f"rloop api {verb} 失败",
                             code=e.get("code", ""), exit_code=rc,
                             hint=e.get("hint", ""), detail=e.get("detail") or {})
        return payload.get("data") or {}

    # --- 调用总表 ---

    def handshake(self) -> Contract:
        """启动第一件事：问核心是什么版本、有什么能力。"""
        with self._lock:
            self.contract = Contract(self.call("meta"))
        return self.contract

    def loops(self, project: str | None = None) -> dict:
        return self.call("loops", *(["--project", project] if project else []))

    def loop(self, loop_id: str, rnd: int | None = None) -> dict:
        args = [loop_id]
        if rnd is not None:
            args += ["--round", str(rnd)]
        return self.call("loop", *args)

    def file(self, loop_id: str, what: str, rnd: int | None = None,
             max_bytes: int | None = None) -> dict:
        args = [loop_id, "--what", what]
        if rnd is not None:
            args += ["--round", str(rnd)]
        if max_bytes is not None:
            args += ["--max-bytes", str(max_bytes)]
        return self.call("file", *args)

    def run(self, project: str, **opts) -> dict:
        """起一轮。**面板不持有这个进程** —— 核心自己 detach，起完就返回。"""
        args = ["--project", project]
        if opts.get("new"):
            args.append("--new")
        for flag, key in (("--focus", "focus"), ("--label", "label"), ("--base", "base"),
                          ("--commit", "commit"), ("--reviewer", "reviewer"),
                          ("--reviewer-model", "reviewer_model"),
                          ("--reviewer-effort", "reviewer_effort")):
            if opts.get(key):
                args += [flag, str(opts[key])]
        for flag, key in (("-n", "max_rounds"), ("--min-score", "min_score")):
            if opts.get(key) not in (None, ""):
                args += [flag, str(opts[key])]
        return self.call("run", *args, timeout=60)

    def stop(self, loop_id: str) -> dict:
        return self.call("stop", loop_id)

    def follow(self, loop_id: str | None = None, rnd: int | None = None,
               since: int = 0, state: bool = True, project: str | None = None):
        """长活订阅，逐条 yield 事件。

        面板只需要**一个** follower。换选中的 loop 或起新一轮 = 杀掉重起；
        `--since` 幂等，重起不会重复补。
        """
        argv = self.prefix + ["api", "--api", str(API), "events"]
        if loop_id:
            argv.append(loop_id)
            if rnd is not None:
                argv += ["--round", str(rnd)]
            argv += ["--since", str(since)]
        if state:
            argv.append("--state")
        if project:
            argv += ["--project", project]
        argv.append("--follow")

        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, bufsize=1)
        try:
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue          # 契约要求读者跳过解析失败的行
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
