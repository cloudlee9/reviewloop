#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板认得的契约版本，以及一份兜底的枚举表。

**运行时一律以 `api meta` 为准**，这里的表只在 meta 都拿不到时用（那种情况下
面板其实已经废了，但至少别再抛第二个异常）。

为什么要有兜底表而不是直接崩：面板启动时先 `api meta`，那一步失败会走
`CoreNotFound` 的人话路径；这里的表是给「meta 拿到了但少几个键」准备的 ——
契约允许加字段，老面板遇到新核心时缺的那几个键不该让整个界面白屏。
"""

from __future__ import annotations

# 这个面板照着写的契约版本。核心报别的数字就说清楚让两边一起升级。
API = 1

# 兜底枚举。**不要**在这里做任何判断逻辑（status→class 那种），
# 判断归核心，面板只做 class→颜色这一层薄映射。
FALLBACK_CLASSES = ["normal", "dim", "title", "ok", "warn", "err", "accent"]

FALLBACK_STATUS_CLASS = {
    "converged": "ok", "pass": "ok", "fixed": "ok",
    "needs_work": "warn", "partially_fixed": "warn", "open": "warn",
    "running": "accent",
    "stalled": "err", "exhausted": "err", "failed": "err",
    "inconsistent": "err", "not_fixed": "err", "aborted": "dim",
    "pinned_scope": "dim", "rebutted_and_accepted": "ok",
}

FALLBACK_SEVERITY_CLASS = {"critical": "err", "high": "err",
                           "medium": "warn", "low": "dim"}

FALLBACK_VERDICT_CLASS = {"pass": "ok", "needs_work": "warn"}

FALLBACK_EVENT_LEVELS = ["info", "note", "cmd", "warn", "err", "highlight"]


class Contract:
    """一次 `api meta` 的结果，加上「拿不到就退回兜底表」的取值方式。"""

    def __init__(self, meta: dict | None = None):
        self.meta = meta or {}
        self.unknown: set = set()      # 遇到过的、表里没有的值

    # --- 版本 ---

    @property
    def core_api(self) -> int:
        return self.meta.get("api", 0)

    @property
    def core_version(self) -> str:
        return self.meta.get("rloop_version", "?")

    @property
    def matches(self) -> bool:
        return self.core_api == API

    @property
    def mismatch_note(self) -> str:
        if self.matches:
            return ""
        return (f"这个面板按 api {API} 写的，rloop {self.core_version} "
                f"提供的是 api {self.core_api}。两边一起升级。")

    # --- 能力 ---

    def has_method(self, name: str) -> bool:
        methods = self.meta.get("methods")
        return True if not methods else name in methods

    def feature(self, path: str, default=None):
        """点号取 features 里的值，比如 `progress_for_reviewer.codex`。"""
        node = self.meta.get("features", {})
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def has_fine_progress(self, reviewer: str | None) -> bool:
        """这个 reviewer 有没有细粒度进度。

        只有 codex 吐 JSONL 事件流；reviewer 是 claude 时中间那段是真的空白。
        面板据此显示「这个 reviewer 没有细粒度进度」，而不是留一片让人以为
        面板坏了的空白。
        """
        if not reviewer:
            return False
        return bool(self.feature(f"progress_for_reviewer.{reviewer}", False))

    # --- 枚举：一律先问 meta ---

    def _lookup(self, table: str, key, fallback: dict) -> str:
        if key is None:
            return "normal"
        src = self.meta.get(table) or fallback
        if key in src:
            return src[key]
        # 不静默降级：记下来，界面上说一声「未知状态 xxx」
        self.unknown.add(f"{table}:{key}")
        return "normal"

    def status_class(self, status) -> str:
        return self._lookup("status_class", status, FALLBACK_STATUS_CLASS)

    def severity_class(self, sev) -> str:
        return self._lookup("severity_class", sev, FALLBACK_SEVERITY_CLASS)

    def verdict_class(self, verdict) -> str:
        return self._lookup("verdict_class", verdict, FALLBACK_VERDICT_CLASS)

    @property
    def classes(self) -> list:
        return self.meta.get("classes") or FALLBACK_CLASSES

    @property
    def event_levels(self) -> list:
        return self.meta.get("event_levels") or FALLBACK_EVENT_LEVELS

    @property
    def artifacts(self) -> dict:
        return self.meta.get("artifacts") or {}

    @property
    def defaults(self) -> dict:
        return self.meta.get("defaults") or {}
