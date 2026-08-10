#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`python3 -m rloopgui <子命令>`。

一般不用手敲：`rloop web` 会拉起它，并把 RLOOP_BIN 指回核心自己。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .client import Client, find_core
from .errors import GuiError


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="rloopgui", description="rloop 的显示层。数据全部来自 rloop 的 CLI。")
    p.add_argument("command", choices=["web", "check"],
                   help="web 起网页面板；check 只做一次握手然后退出")
    p.add_argument("-C", "--directory", default=os.getcwd(), help="项目目录")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-open", action="store_true", help="不要自动开浏览器")
    args = p.parse_args(argv)

    try:
        client = Client()
        contract = client.handshake()
    except GuiError as e:
        print(e.render(), file=sys.stderr)
        return 1

    if not contract.matches:
        # 版本对不上不是崩溃：说清楚，然后照常起 —— 只读的部分多半还能用。
        print(contract.mismatch_note, file=sys.stderr)

    if args.command == "check":
        print(f"rloop {contract.core_version}（api {contract.core_api}）")
        print(f"能力：{', '.join(contract.meta.get('methods') or [])}")
        return 0 if contract.matches else 2

    from .web import serve
    return serve(client, Path(args.directory).resolve(),
                 port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
