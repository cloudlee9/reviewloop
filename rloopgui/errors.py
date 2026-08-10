#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板这一侧的错误。

一条规矩：**给用户看的话必须能照着做**。「出错了」不算，「找不到 rloop」也不算，
得说清去哪儿找、设什么变量。
"""

from __future__ import annotations


class GuiError(Exception):
    """面板起不来或者调不通核心。message 直接展示给用户。"""

    def __init__(self, message: str, hint: str = "", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail or {}

    def render(self) -> str:
        out = self.message
        if self.hint:
            out += f"\n{self.hint}"
        return out


class CoreNotFound(GuiError):
    """找不到 rloop 可执行文件。"""


class CoreFailed(GuiError):
    """rloop 跑了，但返回的是错误负载。`code` 是契约里那个稳定的 ASCII 串。"""

    def __init__(self, message: str, code: str = "", exit_code: int = 1,
                 hint: str = "", detail: dict | None = None):
        super().__init__(message, hint, detail)
        self.code = code
        self.exit_code = exit_code


class CoreUnintelligible(GuiError):
    """rloop 的输出不是能认的 JSON —— 版本不对，或者根本不是 rloop。"""
