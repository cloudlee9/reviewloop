#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rloop — 用另一个模型独立审当前工作区的改动

跑一轮就返回：起一个无头、无状态的 reviewer 子进程审当前改动（默认能跑测试，但动了被审代码这一轮就作废），
给出结构化 findings 与双评分，用退出码表达判定。

循环由调用方驱动——通常是你正在用的那个开发会话：它读 findings、动手改、
写回应，再跑一次 rloop。rloop 自己不改任何代码。这样改动始终发生在一个
你看得见、有完整上下文的会话里，不需要额外的交接。
"""

from __future__ import annotations

import argparse
import calendar
import unicodedata
import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.4.1"

# 对外契约版本。GUI 和任何第三方面板按它判断能不能对话；改字段语义要 +1。
API_VERSION = 1

RLOOP_HOME = Path(os.environ.get("RLOOP_HOME", Path.home() / ".rloop"))
REGISTRY = RLOOP_HOME / "registry.json"

LOOP_DIRNAME = ".review-loops"

DEFAULT_REVIEWER = "codex"
DEFAULT_EFFORT = "medium"   # 别默认 xhigh，每轮都那么跑很贵
DEFAULT_NOTIFY = "macos"
DEFAULT_MAX_ROUNDS = 5
DEFAULT_MIN_SCORE = 8.0
DEFAULT_TIMEOUT = 2400  # 每个 agent 单轮上限，秒
STALL_ROUNDS = 2        # 连续多少轮无进展视为停滞
STALL_EPSILON = 0.1     # 分数提升小于该值视为没提升

# 退出码就是判定契约，驱动循环的会话按它决定下一步。
EXIT_PASS = 0           # 达标，停
EXIT_ERROR = 1          # 出错
EXIT_NEEDS_WORK = 2     # 未达标，有 findings 要处理
EXIT_INCONSISTENT = 3   # reviewer 自相矛盾，判定不可信，别当成达标

# --json 时日志改走 stderr，保证 stdout 上只有那一个 JSON 对象。
JSON_MODE = False


# ─────────────────────────── 评分契约 ───────────────────────────

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "deliverable_maturity": {
            "type": "number",
            "description": "0-10 分。写出来的东西本身：代码结构、文档、契约、示例、测试覆盖、脚本、内部一致性。",
        },
        "production_readiness": {
            "type": "number",
            "description": "0-10 分。真实运行系统的就绪度：安全、基础设施、数据、运维、可观测性，以及对真实依赖跑通过的端到端证据。",
        },
        "blocking_findings": {
            "type": "integer",
            "description": "上线前必须修掉的 critical + high 级 findings 的条数。",
        },
        "verdict": {"type": "string", "enum": ["pass", "needs_work"]},
        "summary": {"type": "string", "description": "2 到 4 句话，中文。开头先用一句说清你理解这次改动想做什么。"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "简短稳定的编号，如 F1。这条问题若在之前某轮已经出现过，"
                                       "必须沿用那一轮的编号。",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "description": {"type": "string", "description": "问题是什么，中文。"},
                    "suggested_fix": {"type": "string", "description": "怎么改，中文。"},
                },
                "required": [
                    "id",
                    "severity",
                    "category",
                    "file",
                    "line",
                    "description",
                    "suggested_fix",
                ],
            },
        },
        "prior_findings_status": {
            "type": "array",
            "description": "上一轮提出的每一条 finding 对应一条，全都要有，不多不少。第一轮为空数组。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "description": "上一轮那条 finding 的编号。"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["fixed", "partially_fixed", "not_fixed", "rebutted_and_accepted"],
                    },
                    "note": {"type": "string", "description": "凭什么这么判，中文。"},
                },
                "required": ["id", "description", "status", "note"],
            },
        },
        "positive_evidence": {
            "type": "array",
            "description": "确实做得好的地方。中文。",
            "items": {"type": "string"},
        },
        "validation_commands": {
            "type": "array",
            "description": "你为了验证实际执行过的命令，以及各自的结果。",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["pass", "fail", "not_run"]},
                    "note": {"type": "string"},
                },
                "required": ["command", "outcome", "note"],
            },
        },
        "next_priorities": {
            "type": "array",
            "description": "建议接下来优先做什么。中文。",
            "items": {"type": "string"},
        },
    },
    "required": [
        "deliverable_maturity",
        "production_readiness",
        "blocking_findings",
        "verdict",
        "summary",
        "findings",
        "prior_findings_status",
        "positive_evidence",
        "validation_commands",
        "next_priorities",
    ],
}


RUBRIC = """\
## 评分标准

分两个维度，各自 0 到 10 分。**不要把它们揉成一个数。**

**deliverable_maturity（交付物成熟度）** —— 写出来的东西本身的质量：代码结构、
文档、契约、示例、测试覆盖、脚本、内部一致性。

**production_readiness（生产就绪度）** —— 真实运行的那套系统的就绪度：安全、
基础设施、数据处理、运维、可观测性，以及**它对着真实依赖跑通过**的端到端证据。

**硬规则，没有例外**：mock、demo、fixture、本地跑绿的检查，只能抬高
deliverable_maturity。真实依赖一次都没被跑通过时，production_readiness 封顶 5 分，
代码写得再漂亮也一样。

打分纪律：

- 每个分数都要有你**亲眼看到的**证据撑着。引文件、行号、命令输出。没验证过的推测不算证据。
- 不要为了显得好说话而抬分，也不要为了显得严谨而压分。
- 这一轮的改动如果确实修好了东西，分数**就该往上走**。该动不动，和虚抬一样是失职。
- blocking_findings 只数 critical + high 两级。
- verdict 填 "pass" 的条件：两个分数都达到门槛**且** blocking_findings 为 0。
"""


REVIEW_CHECKLIST = """\
## 审什么

### 改动的代码（重点，先读 diff）

- **正确性**：逻辑错误、差一错误、走错分支、没处理的 nil / 空值 / 错误路径。
- **并发**：竞态、死锁、没同步的共享状态、非原子的读-改-写。
- **资源**：泄漏的文件 / 连接 / 协程，无界增长，漏掉的清理。
- **错误处理**：被吞掉的错误、丢了上下文的错误、会把用户困住的失败模式。
- **测试**：每个新行为都有测试吗？边界覆盖了吗（空、null、边界值、错误路径）？
  修 bug 的话，有没有一条**原本能抓住这个 bug** 的回归用例？
- **安全**：输入校验、每条受保护路径上的鉴权、注入（SQL / XSS / 命令 / 路径穿越）、
  硬编码或被打进日志的密钥、把内部细节漏给用户的错误信息。

### 整个项目（次要）

- 结构好不好找东西？关注点分开了吗？有没有该拆的巨型文件？
- 配置是否集中？环境变量有没有文档、有没有在启动时校验？
- 错误处理在整个代码库里是否一致？
- 外部集成有没有藏在接口后面？
- 可观测性（日志、指标）够不够在生产上排查问题？

### 验证

**真的去跑点什么。** 测试、构建、lint、冒烟命令 —— 这个项目有什么就跑什么。
把跑过的命令和结果如实填进 validation_commands。一个都没跑就老实标 "not_run"，
不要声称你没做过的验证。

跑不动的时候如实记成 fail 或 not_run，写清楚为什么，然后往下走 —— 你这一轮
具体能做什么，「你的权限」那节说了算。
"""


# ─────────────────────────── 小工具 ───────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def die(msg: str, code: int = 1):
    print(f"rloop: {msg}", file=sys.stderr)
    raise SystemExit(code)


def read_text_safe(path: Path) -> str:
    """读 agent 产出的文件。模型写出的东西不保证是干净 UTF-8，也可能带 BOM。"""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def run_git(project: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(project), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ─────────────────────────── 送审范围的 diff ───────────────────────────
#
# 范围由两个不可变端点界定，全程只从 state 里读，不再各处重算：
#   diff_base   —— diff 起点，一个 sha
#   diff_target —— diff 终点。None 表示「当前工作树」，此时后续改动会
#                  自然进入下一轮送审 diff；是 sha 时表示终点被钉死在那个
#                  提交上（`--commit` 审历史提交），后续提交和工作区内容都
#                  不算数。

# 补丁大到这个数就提醒 reviewer：它读不完，得说清自己抽查了哪儿。
# 500 KB 的代码约 145K token，已经吃掉主流模型 context 的一大半，而 reviewer
# 还要留出读周边文件和推理的余量。**不截断**补丁 —— 截断等于悄悄丢掉一部分
# 改动，比让 reviewer 自己挑着看更糟。
DIFF_LARGE_BYTES = 500_000


UNTRACKED_MAX_FILES = 100        # 超出的只进清单，不进补丁
UNTRACKED_MAX_BYTES = 400_000    # 未跟踪内容拼进补丁的总量上限
UNTRACKED_MAX_HASH_BYTES = 100_000_000   # 再大就不算 sha256 了，别为了记账读一个 G
UNTRACKED_NAMES_IN_PROMPT = 40   # prompt 里最多点名多少个没内联的文件，其余看清单


def scope_diff(project: Path, base: str, target: str | None, *, stat: bool = False) -> str:
    """base→target 的 diff。target 为 None 时 diff 到工作树。"""
    args = ["diff"]
    if stat:
        args.append("--stat")
    args.append(base)
    if target:
        args.append(target)
    args += ["--", ".", f":(exclude){LOOP_DIRNAME}"]
    return run_git(project, *args)


def list_untracked(project: Path) -> list[str]:
    """未跟踪且未被 ignore 的文件。用 -z，免得带特殊字符的文件名被 git 加引号转义。"""
    out = run_git(
        project, "ls-files", "--others", "--exclude-standard", "-z",
        "--", ".", f":(exclude){LOOP_DIRNAME}",
    )
    return [p for p in out.split("\0") if p]


def file_fingerprint(project: Path, path: str) -> tuple[int | None, str | None]:
    """(字节数, sha256)。文件读不了就给 (None, None)，太大就不算哈希。

    **只读普通文件。** 用 lstat 不跟符号链接：一个指向 `/dev/zero` 的链接会让
    下面那个读循环永远读下去，而 workspace_fingerprint 没有超时兜着 —— 一个
    未跟踪的符号链接就能把整个 rloop 挂死。
    """
    full = project / path
    try:
        st = os.lstat(full)
    except OSError:
        return None, None
    if not stat.S_ISREG(st.st_mode):
        return None, None
    size = st.st_size
    if size > UNTRACKED_MAX_HASH_BYTES:
        return size, None
    h = hashlib.sha256()
    try:
        with full.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return size, None
    return size, h.hexdigest()


def untracked_patch(project: Path, paths: list[str]) -> tuple[str, list[dict]]:
    """把未跟踪文件也变成补丁，返回 (补丁文本, 逐文件账目)。

    新文件的内容如果不进 diff.patch，只含新文件的工作区会得到 0 字节补丁：
    reviewer 什么都看不到，replay 复现不出当时的内容，轮次之间也无法比较作者
    对新文件做了什么。用 `--no-index` 对 /dev/null 生成补丁，因为它不碰用户的
    索引（`git add -N` 会）。二进制文件由 git 自己渲染成 "Binary files ... differ"。

    上限之外的文件不进补丁，但**每一个**都要留下账目：路径、字节数、sha256、
    有没有内联、以及没内联的原因。只在 prompt 里报个数字、或者只列前 40 个名字，
    等于把剩下的悄悄丢掉——reviewer 定位不到，下一轮也没法比对它们变没变。
    """
    chunks, records, budget = [], [], UNTRACKED_MAX_BYTES
    for i, path in enumerate(paths):
        size, digest = file_fingerprint(project, path)
        rec = {"path": path, "size": size, "sha256": digest, "inlined": False, "reason": None}
        records.append(rec)
        if i >= UNTRACKED_MAX_FILES:
            rec["reason"] = f"too_many_files (>{UNTRACKED_MAX_FILES})"
            continue
        piece = run_git(project, "diff", "--no-index", "--", os.devnull, path)
        if not piece:
            rec["reason"] = "git produced no diff for this path"
            continue
        cost = len(piece.encode("utf-8", errors="replace"))
        if cost > budget:
            rec["reason"] = f"byte_budget_exhausted (>{UNTRACKED_MAX_BYTES} bytes total)"
            continue
        budget -= cost
        rec["inlined"] = True
        chunks.append(piece)
    return "".join(chunks), records


def build_scope_patch_detailed(project: Path, base: str, target: str | None) -> tuple[str, list[dict]]:
    """本轮送审的完整补丁：跟踪文件的 diff + 未跟踪文件的内容。

    返回 (补丁文本, 未跟踪文件逐条账目)。账目里每个文件都有 inlined / reason，
    不需要再在别处重算"哪些进了补丁"。终点被钉死在某个提交上时，工作区里的
    未跟踪文件本就不在范围内，账目为空。
    """
    patch = scope_diff(project, base, target)
    if target:
        return patch, []
    extra, records = untracked_patch(project, list_untracked(project))
    if extra:
        patch = patch + extra
    return patch, records


def build_scope_patch(project: Path, base: str, target: str | None) -> tuple[str, list[str], list[str]]:
    """同上，但只给路径：(补丁文本, 全部未跟踪文件, 其中没能内联进补丁的)。

    要 size / sha256 / 未内联原因用 build_scope_patch_detailed。
    """
    patch, records = build_scope_patch_detailed(project, base, target)
    return (patch,
            [r["path"] for r in records],
            [r["path"] for r in records if not r["inlined"]])


def untracked_manifest(records: list[dict]) -> dict:
    inlined = [r for r in records if r["inlined"]]
    return {
        "generated_at": now_iso(),
        "count": len(records),
        "inlined": len(inlined),
        "not_inlined": len(records) - len(inlined),
        "limits": {
            "max_files": UNTRACKED_MAX_FILES,
            "max_bytes": UNTRACKED_MAX_BYTES,
            "max_hash_bytes": UNTRACKED_MAX_HASH_BYTES,
        },
        "note": "size/sha256 是生成这一轮补丁时的快照，用来判断文件事后有没有被改动或删除。",
        "files": records,
    }


# ─────────────────────────── Loop 状态 ───────────────────────────


class Loop:
    def __init__(self, root: Path):
        self.root = root
        self.state_file = root / "loop.json"
        self.log_file = root / "loop.log"
        # 跑起来之后由 run_one_round 换成真的 ProgressWriter。默认是个黑洞，
        # 于是查询类命令（list/status/report）不会凭空往轮次目录里写东西。
        self.progress = NullProgress()

    # --- 持久化 ---

    @property
    def state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def save(self, state: dict):
        tmp = self.state_file.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def update(self, **kw):
        s = self.state
        s.update(kw)
        s["updated_at"] = now_iso()
        self.save(s)
        return s

    # --- 日志 ---

    def log(self, msg: str, echo: bool = True):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if echo:
            print(line, flush=True, file=sys.stderr if JSON_MODE else sys.stdout)
        # 同一句话也进事件流，面板才看得到。`!` 开头的是警告。
        self.progress.emit("note", msg.strip(),
                           "warn" if msg.strip().startswith("!") else "info")

    def round_path(self, n: int) -> Path:
        """第 n 轮的目录路径，**只拼不建**。

        读路径一律走这个。`round_dir` 会顺手 mkdir，拿它去读不存在的轮次会凭空
        造出一个空目录 —— `rloop replay 99` 报「没有这一轮」的同时留下 round-99/，
        面板扫目录时又把它当成真轮次。
        """
        return self.root / f"round-{n:02d}"

    def round_dir(self, n: int) -> Path:
        """第 n 轮的目录，不存在就建。**只在准备往里写东西时用。**"""
        d = self.round_path(n)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rounds_available(self) -> list[int]:
        """磁盘上实际存在的轮次，升序。纯扫描，不建任何目录。"""
        out = []
        with contextlib.suppress(OSError):
            for p in self.root.iterdir():
                if p.is_dir() and p.name.startswith("round-"):
                    with contextlib.suppress(ValueError):
                        out.append(int(p.name[len("round-"):]))
        return sorted(out)


# ─────────────────────────── 注册表 ───────────────────────────


@contextlib.contextmanager
def registry_lock():
    """全局注册表的读-改-写要互斥。

    它是所有项目共用的一个文件，每-loop 锁保护不到。无锁读改写会丢条目；
    固定名的 .tmp 还会让两个进程互相 replace 掉对方的临时文件。
    """
    RLOOP_HOME.mkdir(parents=True, exist_ok=True)
    f = (RLOOP_HOME / ".registry.lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)   # 操作很快，这里阻塞等而不是报错
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def registry_read() -> dict:
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def registry_write(data: dict):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名带 pid：固定名会让并发的两个进程互相 replace 掉对方的中间文件，
    # 后到的那个 replace 时源文件已经不在了，抛异常并留下一个孤立的 running loop。
    tmp = REGISTRY.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY)


def registry_put(loop_id: str, root: Path, project: Path, label: str):
    with registry_lock():
        data = registry_read()
        data[loop_id] = {
            "root": str(root),
            "project": str(project),
            "label": label[:200],
            "started_at": now_iso(),
        }
        registry_write(data)


def resolve_loop(loop_id: str | None, project: Path | None) -> Loop:
    """按 id 找 loop；不给 id 就在当前项目里找最近的一个。"""
    data = registry_read()
    if loop_id:
        entry = data.get(loop_id)
        if not entry:
            die(f"unknown loop id: {loop_id}")
        return Loop(Path(entry["root"]))

    project = project or Path.cwd()
    candidates = sorted(
        (Path(e["root"]) for e in data.values() if Path(e["project"]) == project),
        key=lambda p: p.name,
        reverse=True,
    )
    local = project / LOOP_DIRNAME
    if not candidates and local.is_dir():
        candidates = sorted(
            (d for d in local.iterdir() if (d / "loop.json").exists()),
            key=lambda p: p.name,
            reverse=True,
        )
    if not candidates:
        die(f"no loop found for {project}. start one with: rloop")
    return Loop(candidates[0])


# ─────────────────────────── Prompt 构造 ───────────────────────────


def render_findings_for_author(prev: dict) -> str:
    if not prev.get("findings"):
        return "（上一轮没有提出 findings）"
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items = sorted(prev["findings"], key=lambda f: order.get(f.get("severity", "low"), 9))
    out = []
    for i, f in enumerate(items, 1):
        loc = f.get("file", "?")
        if f.get("line", 0):
            loc += f":{f['line']}"
        out.append(
            f"{i}. [{f.get('id', '?')}] [{f.get('severity', '?').upper()}] "
            f"({f.get('category', '-')}) {loc}\n"
            f"   问题：{f.get('description', '')}\n"
            f"   建议：{f.get('suggested_fix', '')}"
        )
    return "\n\n".join(out)


def render_score_history(history: list) -> str:
    if not history:
        return "（首轮）"
    # 只在真有用量数据时加那两列 —— reviewer 是 claude 时拿不到，
    # 凭空多两列空的比不显示更碍眼
    has_usage = any(h.get("usage") for h in history)
    head = "| 轮次 | 交付物成熟度 | 生产就绪度 | 阻塞项 | 判定 |"
    sep = "|---:|---:|---:|---:|---|"
    if has_usage:
        head += " 实付输入 | 输出 |"
        sep += "---:|---:|"
    rows = [head, sep]
    for h in history:
        row = (f"| {h['round']} | {h['deliverable_maturity']} | {h['production_readiness']} "
               f"| {h['blocking_findings']} | {h['verdict']} |")
        if has_usage:
            u = h.get("usage")
            # 报「实付」而不是 input 总数：后者九成是缓存命中，直接看会以为贵十倍
            row += f" {u['fresh']:,} | {u['output']:,} |" if u else " — | — |"
        rows.append(row)
    if has_usage:
        tot = {k: sum(h["usage"][k] for h in history if h.get("usage"))
               for k in ("input", "cached", "fresh", "output")}
        rows.append(f"\n合计实付 **{tot['fresh']:,}** 输入 / **{tot['output']:,}** 输出"
                    f"（输入总量 {tot['input']:,}，其中 "
                    f"{tot['cached'] * 100 // max(1, tot['input'])}% 是缓存命中）")
    return "\n".join(rows)


def is_ledger_path(path: str) -> bool:
    """这条路径属于 rloop 自己的账本吗。

    按**路径段**判断，不是子串匹配 —— `notes-about-.review-loops.md` 这种普通
    文件不该因为名字里带了这几个字就整个被排除在指纹之外。
    """
    return path == LOOP_DIRNAME or path.startswith(LOOP_DIRNAME + "/") \
        or f"/{LOOP_DIRNAME}/" in path


def workspace_fingerprint(project: Path) -> dict:
    """给整个工作区拍一张指纹，用来核对 reviewer 有没有动过代码。

    放开写权限之后，「rloop 从不改你的代码」这条承诺就不能只靠 prompt 里
    嘱咐一句。跑之前拍一张、跑完再拍一张，对不上就说明 reviewer 越界了，
    这一轮的判断不可信。

    四个维度各自能抓到不同的越界：`head` 抓 commit / reset，`diff` 抓已跟踪文件
    的内容，`status` 抓文件的增删，`untracked` 抓未跟踪文件的内容 —— 最后这个
    不是补充而是必需：**未跟踪文件的内容一样会进送审补丁**，而改一个已经存在的
    未跟踪文件，前三个维度全都不动（`?? path` 还是那行、`git diff HEAD` 根本不
    看它）。这个洞是 reviewer 自己在第一次实跑时抓出来并当场复现的。

    **排除 .review-loops/** —— 那是 rloop 自己每轮都在写的账本。ignored 的文件
    本来就不在 porcelain / ls-files --exclude-standard 的输出里，所以
    `__pycache__` 这类被仓库忽略掉的测试产物不会惊动指纹。

    **全程走 bytes，不解码。** 早先 `git diff HEAD` 用 `text=True` 拿输出，仓库里
    只要有一个二进制改动或非 UTF-8 编码的源文件，解码就抛 UnicodeDecodeError，
    被这里的 suppress 一吞，`diff` 这个键干脆不存在 —— 而 `fingerprint_changed`
    的规则是「缺的键不参与比较」，于是整条最重要的维度**静默消失**，reviewer 改
    任何已跟踪文件都抓不到。文件名同理：非 UTF-8 的路径会让 status / ls-files
    整维度丢掉。指纹要的只是「变没变」，本来就不需要认字。
    """
    out = {}
    with contextlib.suppress(Exception):
        r = subprocess.run(["git", "-C", str(project), "status", "--porcelain=v1", "-z"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            out["status"] = sorted(
                x for x in (v.decode("utf-8", "surrogateescape")
                            for v in r.stdout.split(b"\0") if v)
                if not is_ledger_path(x[3:] if len(x) > 3 else x))
    with contextlib.suppress(Exception):
        r = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            # 失败时**不设这个键**。设成空串的话，两次都失败就变成「head 没变」，
            # 而 note_incomplete_fingerprint 也看不出这一维其实没拍到。
            out["head"] = r.stdout.strip()
    # 已跟踪文件的内容摘要：git status 只报「变没变」，同一个文件被改两次
    # 状态字符串是一样的，光看它抓不到 reviewer 在作者改动之上又动了一笔。
    with contextlib.suppress(Exception):
        r = subprocess.run(["git", "-C", str(project), "diff", "HEAD"],
                           capture_output=True, timeout=120)
        if r.returncode == 0:
            out["diff"] = hashlib.sha256(r.stdout).hexdigest()
    # 未跟踪文件：**真的把内容 hash 掉**。先前只记大小和 mtime，理由是「改内容不可能
    # 既不改大小又不动 mtime」—— reviewer 当场证伪了：把同长度的 aaaa 改成 bbbb 再
    # os.utime 复原时间戳，三个维度全静止。
    #
    # 算不算 hash **只看这个文件自己有多大**，不看它排第几。按名次切（前 100 个算
    # hash）会让记录方式取决于邻居：reviewer 新建一个排序靠前的文件，就能把原来的
    # 第 100 名挤成第 101 名，那条记录从 sha256 变成 mtime —— 内容一个字节没动，
    # 却看着像被改了。这是 reviewer 自己算出来的误作废路径。
    with contextlib.suppress(Exception):
        r = subprocess.run(["git", "-C", str(project), "ls-files", "--others",
                            "--exclude-standard", "-z"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            rows = []
            for name in sorted(x for x in (v.decode("utf-8", "surrogateescape")
                                           for v in r.stdout.split(b"\0") if v)
                               if not is_ledger_path(x)):
                size, digest = file_fingerprint(project, name)
                if digest is None:      # 读不了 / 太大：退回元数据，并标明这维降级了
                    st = None
                    with contextlib.suppress(OSError):
                        st = (project / name).stat()
                    digest = f"~{st.st_mtime_ns}" if st else "~?"
                # meta 在前、路径在后：路径里可能有 TAB（git 不会替你转义），放前面
                # 会让 partition 从文件名中间断开，两个同前缀的文件互相掩盖。
                rows.append(f"{size}\t{digest}\t{name}")
            out["untracked"] = rows
    return out


FINGERPRINT_KEYS = ("status", "head", "diff", "untracked")


def fingerprint_changed(before: dict, after: dict) -> list[str]:
    """指纹对不上的地方。空列表 = 工作区没被动过。"""
    if not before or not after:
        return []           # 拍不到指纹时不诬告
    return [k for k in FINGERPRINT_KEYS
            if k in before and k in after and before[k] != after[k]]


def _untracked_map(rows) -> dict:
    """未跟踪指纹行 → {路径: 内容摘要}。

    行的格式是 `size\tdigest\t路径`，摘要在前、路径在后 —— 路径里可能带 TAB
    （git 的 `-z` 输出不做转义），放前面会让解析从文件名中间断开。
    """
    out = {}
    for row in rows or []:
        size, _, rest = row.partition("\t")
        digest, _, name = rest.partition("\t")
        if name:
            out[name] = f"{size}\t{digest}"
    return out


def tampered_dimensions(before: dict, after: dict) -> list[str]:
    """指纹里能说明**被审的代码被动过**的那些维度。

    和 `fingerprint_changed` 的区别全在未跟踪文件上：多出几个文件是跑测试的
    常态副产品，不算动代码；但**动基线里已经有的那些**算 —— 改它、删它，改的
    都是这一轮送审补丁里的内容，和改已跟踪文件是一回事。测试删掉的是自己刚
    生成的产物，那些不在基线里。
    """
    dims = [k for k in ("head", "diff") if k in before and k in after and before[k] != after[k]]
    if before.get("untracked") is not None and after.get("untracked") is not None:
        b, a = _untracked_map(before["untracked"]), _untracked_map(after["untracked"])
        if any(name not in a or a[name] != meta for name, meta in b.items()):
            dims.append("untracked")
    return dims


# reviewer 动手改代码的痕迹。**保守到近乎苛刻**：命中的后果是整轮作废，误报一次
# 就白烧一轮的时间和配额，所以只认那些除了改文件之外没有别的解释的东西。
# `>` 重定向单独限定在源码类后缀上 —— `> /tmp/x.log` 是跑测试的日常，不算。
#
# 写命令必须出现在**命令位置**（行首，或 `;` `&&` `|` 之后，或 `zsh -lc "` 之后），
# 不能只是某条命令的参数。第一版漏了这一层，于是 reviewer 自己实测出误判：
# `rg -n apply_patch rloop.py` —— 一条纯检索命令 —— 被当成了动手证据。搜自己的
# 源码里有没有某个词，是评审时最正常不过的动作。
#
# git 那一支还得再细分：`git apply --check`、`git stash list`、`git commit --dry-run`
# 都是**只读**的，只看子命令名会把它们连坐进来。这条也是实测出来的误判。
_CMD_POS = r"(?:^|[;&|]\s*|\b(?:ba|z)?sh\s+-lc\s+[\"']?)"
WRITE_EVIDENCE_RE = re.compile(
    _CMD_POS + r"(?:apply_patch\b"
    r"|sed\s+-i\b"
    r"|patch\s+-p\d"
    r"|git\s+(?:commit|am|revert|restore|reset)\b"
    r"|git\s+apply\b"
    r"|git\s+stash\s+(?:push|save|pop|apply|drop|clear|create\s+-)"
    r"|git\s+stash\s*$"
    r"|git\s+checkout\s+--"
    # 项目自带的工具链改起代码来一样是改代码，而且这不是「刻意规避」，是日常操作：
    # reviewer 顺手跑个 formatter 就能把被审代码重写一遍。带 --check / --dry-run 的
    # 那些由 _DRYRUN_RE 排掉。
    r"|ruff\s+[^|;&]*--fix"
    r"|(?:gofmt|goimports)\s+[^|;&]*-w\b"
    r"|(?:black|isort|autopep8|yapf|clang-format|rustfmt|cargo\s+fmt|dart\s+format)\b"
    r"|prettier\s+[^|;&]*--write"
    r"|(?:npm|pnpm|yarn)\s+run\s+\S*(?:fix|format)"
    r"|eslint\s+[^|;&]*--fix)"
    r"|>>?\s*\S*\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|c|h|cc|cpp|swift|kt|php|sh|sql"
    r"|toml|cfg|ini|gradle)\b")

# 这些 flag 一出现，那条 git 命令就什么都不会改。放在正则之外单独判，是因为
# 「命令里出现了 --dry-run」这件事跟它匹配到哪一支无关。
_DRYRUN_RE = re.compile(r"--(?:dry-run|check|stat|numstat|summary|help)\b|\s-h\b")

# 临时目录的痕迹。reviewer **应该**在 /tmp 里建临时仓库复现 finding —— 前几轮它
# 正是这么干的，`cd /tmp/probe && git commit -am probe` 是评审动作而不是越界。
# 这类命令一律不算证据。代价是这也成了一条逃生阀（脚本里写个 tempfile 就免疫），
# 但误作废整轮的代价比漏判高得多，这个方向的偏保守是有意的。
_TMPWORK_RE = re.compile(r"\bmktemp\b|\bmkdtemp\b|\btempfile\b|\bTemporaryDirectory\b|\btmp_path\b")


def _outside_project(path: str, project: Path | None) -> bool:
    root = str(project) if project else None
    return not root or not (path == root or path.startswith(root + "/"))


def _leaves_the_project(cmd: str, project: Path | None) -> bool:
    """这条命令写的是项目外的东西吗。"""
    if _TMPWORK_RE.search(cmd):
        return True
    # 切到项目外的目录去操作
    for m in re.finditer(r"\b(?:cd|git\s+-C|pushd)\s+([\"']?)(/[^\s\"';&|]+)\1", cmd):
        if _outside_project(m.group(2), project):
            return True
    # 重定向到项目外的绝对路径。`echo x > /tmp/probe/foo.py` 一样是 reviewer 复现
    # 问题的常规动作 —— 它没有 cd、也没提 mktemp，光看前一条规则漏得干干净净。
    for m in re.finditer(r">>?\s*([\"']?)(/[^\s\"';&|]+)\1", cmd):
        if _outside_project(m.group(2), project):
            return True
    return False


def reviewer_write_evidence(log_file: Path, project: Path | None = None) -> list[str]:
    """从 reviewer 自己的日志里找它动手改**这个项目**的痕迹。

    指纹只能证明「工作区变了」，证明不了「是 reviewer 变的」—— 评审要跑好几分钟，
    你在这期间接着改自己的代码是 rloop 支持的用法，不是异常。所以作废需要第二个
    信号：它自己的执行记录。codex 的 `--json` 事件流里，改文件走 `file_change`
    item；用 shell 绕过去的那些则要靠命令本身认出来。

    **在临时目录里的写操作不算。** reviewer 自己实测出了这个误判：它为复现 finding
    在 `/tmp/probe` 建临时仓库、`echo x > sample.py`、`git commit` —— 全是正当的
    评审动作；要是作者恰好同时改了真工作区，两个互不相干的信号会凑成一次误作废。

    claude 那边没有等价的事件流，这里只能扫到空 —— 也就是说 claude 当 reviewer 时
    只会得到提醒，不会被作废。这个不对称是真的，README 里写明了。
    """
    if not log_file.exists():
        return []
    hits = []
    for line in read_text_safe(log_file).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item") or {}
        if item.get("type") in ("file_change", "patch_apply"):
            hits.append(f"{item['type']}: {json.dumps(item, ensure_ascii=False)[:200]}")
            continue
        if item.get("type") == "command_execution" and ev.get("type") == "item.completed":
            cmd = item.get("command") or ""
            if (WRITE_EVIDENCE_RE.search(cmd)
                    and not _DRYRUN_RE.search(cmd)
                    and not _leaves_the_project(cmd, project)):
                hits.append(cmd[:200])
    return hits


def new_paths(before: dict, after: dict) -> list[str]:
    """跑完之后多出来的未跟踪路径。

    多出文件够不上作废 —— 绝大多数时候就是没被 gitignore 的测试产物。但也不能
    不说：未跟踪文件**会进下一轮的送审范围**，不点名的话作者会在下一轮看见一堆
    自己没写过的"改动"。
    """
    old = set(_untracked_map(before.get("untracked")))
    return sorted(set(_untracked_map(after.get("untracked"))) - old)


def reviewer_permission_note(agent: str, verify: bool) -> str:
    """告诉 reviewer 它到底能做什么。

    说错的代价是具体的：以为自己只读，它就不去跑那些本可以跑的命令；以为自己
    能跑，它会声称跑过根本跑不了的东西。两种都会污染 validation_commands。
    """
    if not verify:
        if agent == "codex":
            return """\
## 你的权限

你跑在一个只读沙箱里。这个工作区里的任何文件你都能读，命令也能跑，但**所有往文件系统
的写入都会被拦掉**，包括你启动的命令自己要写的那些。不要试图修任何东西 —— 你是评审者，
findings 由作者去处理。那些非写不可的工具（pytest 的缓存、构建产物）会卡在写入上；
用不需要写的方式跑（比如 `pytest -p no:cacheprovider`），或者如实把结果记成 `fail` /
`not_run` 并写明原因。

**同一个测试运行器失败两次就别再试了**：换什么参数都绕不过去，每试一次都白费一轮
本可以用来读代码的机会。这一档下真正管用的是：读源码、`git diff`、`ast.parse`、
以及直接 import 模块调纯函数（`python3 -c 'import x; ...'`）。
"""
        return """\
## 你的权限

你跑在 plan 模式下：只有只读工具。文件能读能搜，但**写不了文件系统，也执行不了 shell
命令**。不要声称你跑过任何东西 —— 把你本来想跑的命令填进 `validation_commands`，
outcome 记 `not_run`，并在 `note` 里说明评审方没有 shell。你的判断建立在读代码上。
不要试图修任何东西，findings 由作者去处理。
"""

    # 后果这句必须按 agent 说实话。codex 那边真会作废（有事件流可查，两个信号齐了
    # 就退 1）；claude 这边扫不到执行记录，作废触发不了 —— 把"会作废"原样说给它，
    # 就是拿一句执行不了的威慑当保险。
    sandbox_blame = """\
**先分清失败是谁的错。** 沙箱挡掉的东西会以测试失败的样子出现，那是评审环境的限制，
不是被审代码的缺陷 —— 拿它开 finding 就是误报。实测撞见过这两类：

- **绑端口 / 起本地服务 / 联网**：`PermissionError: [Errno 1] Operation not permitted`
  落在 `socket.bind`、或者连接被拒。
- **看别的进程**：`ps` 之类的输出为空或缺行，于是依赖「进程还活着吗 / 什么时候起的」
  的用例断在空字符串上。

判断方法很直接：看报错是不是权限、网络、进程可见性这几类，以及**同一个仓库里不碰
这些的测试是不是全绿**。这类失败在 `validation_commands` 里记 `fail` 并在 `note` 里
写明是沙箱所致；打分时不因此扣分，但也不能反过来当成「跑通了」。

""" if agent == "codex" else ""
    consequence = ("被改过、而且你的执行记录里留下了动手的痕迹，这一轮会被判为不可信、整轮作废"
                   if agent == "codex" else
                   "被改过的话会记进本轮报告 —— 你这边没有可供核查的执行事件流，所以触发不了\n"
                   "自动作废，但那不是许可：一份建立在你自己动过的代码上的判断，本来就不作数")
    common = f"""\
**但你绝不能改这个项目的代码。** 你是评审者，findings 交给作者去处理。
这条不是靠自觉：rloop 在起你之前给整个工作区拍了指纹，你退出后会逐一核对，
{consequence}。所以：

- 跑测试、装临时依赖、生成构建产物、写临时文件 —— 都可以，那正是我们放开写权限的原因。
- **改被审的源码、改测试让它变绿、动 git 状态** —— 不行。想验证某个修法可行，
  就在 `suggested_fix` 里把它写清楚，别自己动手。
- 跑完把命令和真实结果填进 `validation_commands`。**跑过的就记 pass/fail，
  别把没跑的记成 not_run 之外的任何值。**

{sandbox_blame}打分那条硬规则**没有变**：真实依赖一次都没跑通过时，`production_readiness` 仍然封顶
5 分。变的是你现在**有能力自己去确认它到底通没通**，而不是只能记 `not_run` 然后按
最坏情况估。跑通了就拿着输出给分，跑挂了就如实压分 —— 跑都没跑还给高分，站不住。
"""
    if agent == "codex":
        return """\
## 你的权限

你跑在 `workspace-write` 沙箱里：**能读全盘，能在这个工作区和临时目录里写，能执行命令**。
写不到 HOME 之类的地方（内核会拒绝），这是操作系统层面挡着的，不用你操心。

""" + common
    return """\
## 你的权限

你能读文件、能执行 shell 命令、能写文件。**注意：这里没有操作系统层面的沙箱兜住你** ——
不像另一侧的 reviewer 有内核强制的写入边界，你这边全靠自觉。所以下面这条要格外当真。

""" + common


LABEL_MAX_CHARS = 26
# 测试和文档几乎每轮都动，说不出这一轮到底在做什么，所以按行数计权时压一压
LABEL_QUIET_WEIGHT = 0.15
LABEL_QUIET = {"tests", "test", "docs", "doc"}


def infer_label(diff_text: str, focus: str | None = None, parts: int = 2) -> str:
    """给一个 loop 起个人看得懂的名字。

    列表里全是 `20260810-010527-c-6610` 这样的时间戳，看不出哪个是哪个。
    优先用作者自己写的侧重（那最能说明「在做什么」），没有就看这一轮实际
    动了哪儿 —— 按**改动行数**给顶层路径排序，不是按文件数：测试文件个数多，
    按个数排的话每个 loop 都叫「tests」，一点区分度都没有。
    """
    if focus:
        # 侧重通常是一整段话，取第一个句子当名字。冒号也算断点 —— 中文里
        # 「做了什么：具体怎么做的」很常见，冒号前那半句正好是标题。
        head = re.split(r"[。；;：:\n]", focus.strip(), maxsplit=1)[0].strip()
        if head:
            return head[:LABEL_MAX_CHARS] + ("…" if len(head) > LABEL_MAX_CHARS else "")

    weight: dict = {}
    cur = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            with contextlib.suppress(IndexError):
                f = line.split(" a/", 1)[1].split(" b/")[0]
                bits = f.split("/")
                cur = bits[0] if len(bits) > 1 else f
        elif cur and line[:1] in "+-" and not line.startswith(("+++", "---")):
            w = LABEL_QUIET_WEIGHT if (cur in LABEL_QUIET or cur.endswith(".md")) else 1.0
            weight[cur] = weight.get(cur, 0.0) + w
    top = sorted(weight, key=lambda k: -weight[k])[:parts]
    return "、".join(top)[:LABEL_MAX_CHARS]


def build_context_pack(loop: Loop, rnd: int) -> str:
    """每轮为 reviewer 重建全部上下文。reviewer 是无状态的。"""
    s = loop.state
    project = Path(s["project"])
    rd = loop.round_dir(rnd)

    diff, untracked = build_scope_patch_detailed(
        project, s["diff_base"], s.get("diff_target"))
    (rd / "diff.patch").write_text(diff, encoding="utf-8")
    if len(diff.encode("utf-8", errors="replace")) > DIFF_LARGE_BYTES:
        loop.log(f"  ! 送审补丁 {len(diff.encode('utf-8', errors='replace')) // 1024} KB / "
                 f"{len(diff.splitlines())} 行，超出 reviewer 能通读的量 —— "
                 f"它会挑着看并在小结里交代覆盖范围。想审得细就缩小范围"
                 f"（--base / --commit 分批）。")
    if not s.get("label"):
        # 只在还没有名字时补：创建 loop 那会儿还没有 diff 可看。补过就不再改 ——
        # 名字变来变去比没名字更难认。
        with contextlib.suppress(Exception):
            guess = infer_label(diff, s.get("focus"))
            if guess:
                loop.update(label=guess)
    manifest_path = rd / "untracked-manifest.json"
    if s.get("diff_target") is None:
        manifest_path.write_text(
            json.dumps(untracked_manifest(untracked), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    diff_note = (
        f"送审的补丁在 `{rd / 'diff.patch'}`（{len(diff.splitlines())} 行）：{s['scope_desc']}。"
    )
    if rnd > 1:
        diff_note += (
            f"\n\n第 2 轮起，补丁里还叠着作者后续的修改。上一轮的补丁在 "
            f"`{loop.round_path(rnd - 1) / 'diff.patch'}` —— 把这两个文件 diff 一下，"
            f"就能单独看出你上次看过之后又变了什么。"
        )
    diff_note += (
        "\n\n先读补丁，然后直接去看文件本身 —— 光看 diff 判断不了周围的代码"
        "是否让这个改动是安全的。"
    )
    diff_bytes = len(diff.encode("utf-8", errors="replace"))
    if diff_bytes > DIFF_LARGE_BYTES:
        # 不截断，但必须让 reviewer 知道自己面对的量，并且**在结果里交代**
        # 覆盖了多少。否则它读了个开头就打分，而没有任何人知道这件事。
        diff_note += (
            f"\n\n**这个补丁很大（{diff_bytes // 1024} KB，{len(diff.splitlines())} 行），"
            f"你多半读不完。** 不要假装读完了：\n"
            f"- 按风险挑着看 —— 优先并发、错误处理、外部输入、权限、数据写入，"
            f"跳过成片的样板和生成物；\n"
            f"- 在 `summary` 里**明确写出你实际覆盖了哪些文件/哪几块**，"
            f"以及哪些部分没看；\n"
            f"- 打分时把这件事算进去：只看了一部分就给不出高的 "
            f"`production_readiness`。"
        )
    if untracked:
        inlined = [r["path"] for r in untracked if r["inlined"]]
        missing = [r for r in untracked if not r["inlined"]]
        diff_note += (
            f"\n\n范围内有 {len(untracked)} 个未跟踪的新文件，其中 {len(inlined)} 个"
            f"的完整内容已经内联进补丁（相对 /dev/null 的新增）"
            f"{'' if not missing else f'，另外 {len(missing)} 个没有'}。完整账目 —— "
            f"每个路径、字节数、sha256、有没有内联、没内联的原因 —— 在 `{manifest_path}`。"
        )
        if missing:
            shown = missing[:UNTRACKED_NAMES_IN_PROMPT]
            diff_note += (
                f"\n\n这 {len(missing)} 个未跟踪文件**不在**补丁里，需要你自己去磁盘上读：\n"
                + "\n".join(f"{r['path']}  ({r['size']} bytes, {r['reason']})" for r in shown)
            )
            if len(missing) > len(shown):
                diff_note += (
                    f"\n…… 另有 {len(missing) - len(shown)} 个，全都列在 "
                    f"`{manifest_path}` 里 —— 不要当它们不存在。"
                )

    log = run_git(project, "log", "--oneline", "-10", "--no-decorate").strip()
    intent = f"""\
## 作者说了想让你侧重什么

{s['focus']}
""" if s.get("focus") else """\
## 意图

没人告诉你这次改动想干什么。你自己从 diff 和下面的近期提交信息里推断，
在 `summary` 开头用一句话说出你的理解，然后**照着这个理解**去审代码。
如果这堆改动本身就前后不搭 —— 像是在同时干两件不相干的事，或者只干了一半 ——
直接说出来，那本身就是一条 finding。
"""
    if log:
        intent += f"\n近期提交（供参考，不一定属于本次送审范围）：\n\n```\n{log}\n```\n"

    prior = ""
    if rnd > 1:
        prev_path = loop.round_path(rnd - 1) / "review.json"
        if prev_path.exists():
            prev = json.loads(read_text_safe(prev_path))
            prior = f"""\
## 你自己上一轮提的 findings

这些是你提的，作者已经处理过了。下面每一个编号**都要**在 `prior_findings_status` 里
对应恰好一条 —— 全都要有，不多不少 —— 说明它现在是修好了、只修了一半、没修，
还是作者的反驳让你认为它本来就不成立。漏掉一条，那个问题就从账本上悄悄消失了，
所以有漏项的结果会被整轮拒收。

下面的问题如果**仍然存在**，还要用**同一个编号**在 `findings` 里再列一次。

{render_findings_for_author(prev)}
"""
        resp = loop.round_path(rnd - 1) / "response.md"
        if resp.exists():
            prior += (f"\n## 作者对这些 findings 的逐条交代与反驳\n\n"
                      f"{read_text_safe(resp)}\n")
        else:
            prior += ("\n（作者没有为上一轮的 findings 留下任何书面交代。"
                      "只能就代码本身来判断。）\n")

    return f"""\
你是独立评审者。这个工作区里的改动是别人写的，可能有另一个模型参与。
现在是第 {rnd} 轮，最多 {s['max_rounds']} 轮。

你的任务是**如实打分**，并找出真正有问题的地方。这些代码不是你写的，你不欠它任何情面；
反过来，你也不欠它任何刻意的挑剔 —— 东西做得好就说好、分数就给到位。为了显得严谨而
编造 findings，和漏掉真问题一样是失职。

{reviewer_permission_note(s['reviewer'], s.get('verify', True))}
{intent}

## 分数走势

{render_score_history(s.get('history', []))}

## 这次送审的是什么

{diff_note}

{prior}

{REVIEW_CHECKLIST}

{RUBRIC}

## 门槛

两个分数都 >= {s['min_score']}，**并且** blocking_findings 为 0，这个 loop 才算通过。
不要为了让它通过、或者为了不让它通过而调整分数。看到什么就打什么。

## 输出

按 output schema 要求返回结构化对象。所有给人读的文字（summary、description、
suggested_fix、note、positive_evidence、next_priorities）一律用中文。文件路径、命令、
标识符、日志原文保持原样，不要翻译。
"""

def parse_codex_event(line: str) -> dict | None:
    """把 codex 的一行 JSONL 事件解析成结构化进度事件。不值得显示的返回 None。

    没有它，reviewer 那几分钟对调用方是完全黑的 —— 只能干等，也分不清它是在
    认真读代码还是已经卡死。

    这里只产出**语义**（kind/level/data）和不带任何前缀符号的正文（text）；
    终端上那几个 `$` `↳` `·` `!` 前缀是 `format_event` 的事。分开是因为进度
    还要落盘给别的进程读 —— 面板从此按 kind 判断语义，不再去匹配前缀符号。
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    kind = ev.get("type")
    item = ev.get("item") or {}
    itype = item.get("type")

    if kind == "item.started" and itype == "command_execution":
        cmd = " ".join((item.get("command") or "").split())
        return {"kind": "cmd.start", "level": "cmd", "text": cmd[:110],
                "data": {"command": cmd}}
    if kind == "item.completed":
        if itype == "command_execution":
            rc = item.get("exit_code")
            # 成功的命令不用报，否则刷屏；失败的必须让人看见
            if rc == 0:
                return None
            return {"kind": "cmd.end", "level": "err", "text": f"exit {rc}",
                    "data": {"command": " ".join((item.get("command") or "").split()),
                             "exit_code": rc}}
        if itype == "agent_message":
            text = " ".join((item.get("text") or "").split())
            if text.startswith("{"):     # 结构化结果本身，不是说给人听的
                return None
            return {"kind": "agent.msg", "level": "note", "text": text[:120], "data": {}}
        if itype == "error":
            msg = " ".join((item.get("message") or "").split())
            return {"kind": "agent.error", "level": "err", "text": msg[:120], "data": {}}
    if kind == "turn.completed":
        u = ev.get("usage") or {}
        tok = u.get("output_tokens", "?")
        return {"kind": "agent.turn", "level": "note",
                "text": f"本轮完成（输出 {tok} tokens）",
                "data": {"output_tokens": u.get("output_tokens")}}
    return None


# 终端上的前缀符号。**只在这里出现一次**——以前 TUI 和 web 各存一份匹配表，
# 核心改一个符号两边同时静默变灰。现在面板拿的是 kind，不是符号。
EVENT_PREFIX = {
    "cmd.start":   "    $ ",
    "cmd.end":     "      ↳ ",
    "agent.msg":   "    · ",
    "agent.error": "    ! ",
    "agent.turn":  "    · ",
}


def format_event(ev: dict | None) -> str | None:
    """结构化事件 → 终端上那一行。不该往终端打的返回 None。"""
    if not ev:
        return None
    prefix = EVENT_PREFIX.get(ev.get("kind"))
    return None if prefix is None else prefix + (ev.get("text") or "")


def render_codex_event(line: str) -> str | None:
    """codex 的一行 JSONL → 终端上那一行。解析与渲染的组合。"""
    return format_event(parse_codex_event(line))


# ─────────────────────────── 进度事件落盘 ───────────────────────────

PROGRESS_FILE = "progress.ndjson"
EVENT_TEXT_CHARS = 512        # text 截断长度
EVENT_LINE_BYTES = 3500       # 单行硬上限，见 ProgressWriter.emit
PROGRESS_MAX_BYTES = 8 << 20  # 单轮进度文件上限


class ProgressWriter:
    """把进度事件追加到 `round-NN/progress.ndjson`。

    存在的理由：`format_event` 的结果以前只 `print` 给自己的 stdout，一个字节都
    不落盘 —— 于是**谁不拥有那个 reviewer 进程，谁就看不见进度**。两个面板因此
    都被迫自己 Popen 起 rloop 才能有进度可看，进程管理散在界面层。落盘之后进度
    变成可回放、可多消费者的东西，面板只要读文件。

    写失败一律吞掉：进度是附属品，绝不能因为磁盘满了就把一轮 review 带崩。
    """

    def __init__(self, root: Path, rnd: int, loop_id: str = "", run_id: str = ""):
        self.path = root / f"round-{rnd:02d}" / PROGRESS_FILE
        self.loop_id = loop_id
        self.round = rnd
        self.run_id = run_id
        self.seq = self._resume_seq()
        self.stopped = False
        self.lock = threading.Lock()

    def _resume_seq(self) -> int:
        """接着上次的 seq 往下写。

        同一轮可能被接管重跑（比如上一次 runner 被 kill 了），seq 从 1 重来会让
        读者的 `--since` 裁剪把新事件当成旧的丢掉。
        """
        last = 0
        with contextlib.suppress(OSError, ValueError):
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                    with contextlib.suppress(json.JSONDecodeError, AttributeError):
                        s = json.loads(line).get("seq")
                        if isinstance(s, int) and s > last:
                            last = s
        return last

    def emit(self, kind: str, text: str = "", level: str = "info", data: dict | None = None):
        """追加一条事件。任何失败都静默。

        `run.end` 是唯一在停止追加之后**仍然允许写**的事件：读者靠它收工，
        丢了它 follower 就一直挂着等一个不会来的收尾。所以撞上限时先写告警、
        再放这一条过去，之后才真正闭嘴。
        """
        final = kind == "run.end"
        if self.stopped and not final:
            return
        with contextlib.suppress(OSError, ValueError, TypeError):
            with self.lock:
                if not self.path.parent.is_dir():
                    return           # 轮次目录还没建，这条丢掉
                if self.stopped and not final:
                    return
                if not self.stopped and self.path.exists() \
                        and self.path.stat().st_size >= PROGRESS_MAX_BYTES:
                    # 告警也要占一个**新**序号：复用上一条的 seq，按 seq 去重的
                    # 读者（`--since` 就是）会把这条告警整个丢掉，于是日志静默截断。
                    self.seq += 1
                    self._append_raw(self._pack(
                        "note", "进度日志超上限，后续进度不再落盘", "warn",
                        {"limit_bytes": PROGRESS_MAX_BYTES}))
                    self.stopped = True
                    if not final:
                        return
                self.seq += 1
                self._append_raw(self._pack(kind, text, level, data or {}))

    def _pack(self, kind: str, text: str, level: str, data: dict) -> str:
        ev = {
            "api": API_VERSION, "seq": self.seq, "ts": now_iso(),
            "loop": self.loop_id or None, "round": self.round,
            "run": self.run_id or None,
            "kind": kind, "level": level,
            "text": (text or "")[:EVENT_TEXT_CHARS], "data": data,
        }
        line = json.dumps(ev, ensure_ascii=False)
        if len(line.encode("utf-8")) > EVENT_LINE_BYTES:
            # O_APPEND 的单次 write() 只在 < PIPE_BUF 时原子，超了并发读者会读到半行。
            ev["data"] = {"oversized": True}
            ev["text"] = (text or "")[:200]
            line = json.dumps(ev, ensure_ascii=False)
        return line

    def _append_raw(self, line: str):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


class NullProgress:
    """不落盘的占位，省得每个调用点都判空。"""

    seq = 0
    stopped = True

    def emit(self, *a, **kw):
        pass


def parse_reviewer_usage(log_path: Path) -> dict | None:
    """从 reviewer 的原始日志里刨出这一轮的 token 用量。

    只有 codex 会吐（它的 JSONL 事件流里 `turn.completed` 带 usage）；claude
    那条路径拿不到，返回 None，账本里就没有这个字段。

    取**最后一条**：那是整个 turn 的累计。reviewer 每调一次工具就要把整个
    上下文重发一遍，所以 input 会涨得很快 —— 而其中绝大部分是缓存命中，
    单独记下来，不然只看 input 总数会以为贵得离谱。
    """
    last = None
    with contextlib.suppress(OSError):
        for line in read_text_safe(log_path).splitlines():
            if '"usage"' not in line:
                continue
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                u = json.loads(line).get("usage")
                if isinstance(u, dict) and "input_tokens" in u:
                    last = u
    if not last:
        return None
    inp = last.get("input_tokens") or 0
    cached = last.get("cached_input_tokens") or 0
    return {
        "input": inp,
        "cached": cached,
        "fresh": max(0, inp - cached),      # 真正按新 token 计费的那部分
        "output": last.get("output_tokens") or 0,
        "reasoning": last.get("reasoning_output_tokens") or 0,
    }


def effort_args(agent: str, effort: str | None) -> list:
    """两家 CLI 的推理档位开关不一样，收敛到一处。"""
    if not effort:
        return []
    return ["--effort", effort] if agent == "claude" else ["-c", f"model_reasoning_effort={effort}"]


def reviewer_cmd(agent: str, project: Path, pack: str, schema_file: Path,
                 out_file: Path, model: str | None = None,
                 effort: str | None = None, verify: bool = True) -> list:
    """reviewer 的完整命令行。

    `verify` 决定放开到哪一档。默认放开，因为不放开的代价是实打实的：只读沙箱下
    连 `.pytest_cache` 都写不了，任何用临时目录的测试都跑不起来，`production_readiness`
    只能建立在读代码上。放开之后它能真的把测试跑一遍，findings 带得上实证。

    放开的边界卡在「够跑测试」那一档，不再往上：codex 用 workspace-write（工作区和
    临时目录可写、可执行命令，HOME 由内核挡着），不用 danger-full-access，更不给
    `--dangerously-bypass-*`；claude 用 `auto`，不给 `--dangerously-skip-permissions`。
    reviewer 不该改代码这件事另有两道保险：prompt 里的明令，加上跑完的工作区指纹核对。

    **无论哪一档，仓库自己的定制都关着。** 两家 CLI 都会加载仓库里的配置并执行其中
    定义的生命周期 hook，那是在模型说第一句话之前就跑掉的 `command`，不受 plan 模式 /
    沙箱档位管辖。实测（本机 claude 2.1.226）：临时仓库里放一个 `.claude/settings.json`
    的 SessionStart hook，`-p --permission-mode plan` 照样把它执行了（`-p` 还会跳过
    workspace trust 询问）。所以 `--safe-mode` / `--ignore-rules` 这一侧一直堵着。
    另外两家都不需要 reviewer 自己写文件来交付结果 —— 结果走 schema。
    """
    if agent == "codex":
        # 沙箱由 codex 自己执行；--output-schema / -o 是 codex 进程写的，不受沙箱限制。
        # --ignore-rules 丢掉仓库里的 .rules execpolicy（那是仓库能控制的执行策略），
        # --ephemeral 不落会话文件。codex 的 hook 另有 trust 机制，这里不给
        # --dangerously-bypass-hook-trust，就不会替仓库把它绕过去。
        # `-s/--sandbox` 和 `-C` 是 codex 的**全局选项**，放在子命令之前。
        # 实测过它确实是沙箱的开关，不是摆设：带 -s read-only 时子进程写文件被拒
        # （operation not permitted），不带时同一条命令写成功。
        # --ephemeral 不落会话文件；reviewer 每轮都是全新的，没有会话要留。
        # workspace-write：能在工作区和临时目录里写、能执行命令，但内核挡着 HOME。
        # 这是让 reviewer 真能跑测试的最小放开 —— 不用 danger-full-access，
        # 更不用 --dangerously-bypass-approvals-and-sandbox。
        cmd = ["codex", "-s", "workspace-write" if verify else "read-only",
               "-C", str(project),
               "exec",
               "--json",                 # 事件流，用来显示进度
               "--ignore-rules",
               "--ephemeral",
               "--output-schema", str(schema_file),
               "-o", str(out_file), pack]
        if model:
            cmd += ["-m", model]
    else:
        # 放开档用 `auto` 而不是 `acceptEdits` —— 这是实测出来的：同一句
        # 「跑 python3 -c 'print(6*7)'」，`acceptEdits` 和 `dontAsk` 都答 BLOCKED
        # （它们只自动批准文件编辑，Bash 仍要人点头，而 -p 模式下没人可问），
        # 只有 `auto` 真的跑出了 42。给错档位比不放开更糟：reviewer 以为自己能跑，
        # 结果 validation_commands 里全是「被权限层拒绝」。
        # plan 模式没有写工具也没有 shell，所以结果只能走 stdout：--json-schema 让
        # claude 把符合 schema 的 JSON 直接打出来，落盘由 rloop 自己做。
        # --safe-mode 关掉全部可定制项（hook、MCP、插件、自定义命令与 agent、
        # CLAUDE.md），认证与内置工具不受影响；--no-session-persistence 不落会话。
        # claude 这边没有操作系统层面的沙箱：权限是工具层的，放开就是真放开。
        # 所以 verify 模式下靠的是 prompt 里的禁令 + 跑完的工作区指纹核对。
        cmd = ["claude", "-p", pack,
               "--permission-mode", "auto" if verify else "plan",
               "--safe-mode",
               "--json-schema", json.dumps(REVIEW_SCHEMA, ensure_ascii=False)]
        cmd.append("--no-session-persistence")
        if model:
            cmd += ["--model", model]
    return cmd + effort_args(agent, effort)


def note_incomplete_fingerprint(loop: "Loop", fp: dict, when: str) -> bool:
    """指纹没拍全就说出来，返回是否完整。

    这条保险是**失败开放**的：git 挂了、或者这儿压根不是仓库时，reviewer 照样
    拿着写权限跑，只是没人核对它。不吭声的话，「有指纹兜着」会变成一句你以为
    成立、实际没成立的话。两张快照都要查 —— 只查起跑那张的话，评审期间 git 挂掉
    会让 after 缺维度，而「缺的键不参与比较」会让它安静地看起来像没变过。
    """
    missing = [k for k in FINGERPRINT_KEYS if k not in fp]
    if missing:
        loop.log(f"  ! {when}的工作区指纹缺了 {'、'.join(missing)}，"
                 f"这一轮的越界核对不完整")
    return not missing


def run_reviewer(loop: Loop, rnd: int) -> int:
    s = loop.state
    project = Path(s["project"])
    rd = loop.round_dir(rnd)
    pack = build_context_pack(loop, rnd)
    (rd / "review-prompt.md").write_text(pack, encoding="utf-8")

    # 每轮都对齐当前 schema。早先只在文件不存在时写，于是 schema 一升级，
    # 已经开着的 loop 会一直拿第一轮落盘的旧版去要求 reviewer —— 然后被按新版
    # 写的自洽校验判成不合规，卡死在退出码 3。
    schema_file = loop.root / "review-schema.json"
    payload = json.dumps(REVIEW_SCHEMA, ensure_ascii=False, indent=2)
    if not schema_file.exists() or read_text_safe(schema_file) != payload:
        schema_file.write_text(payload, encoding="utf-8")

    agent = s["reviewer"]
    log_file = rd / "reviewer.log"
    verify = s.get("verify", True)
    # 放开写权限的那道保险：跑之前拍一张工作区指纹，跑完核对。
    # 只读模式下不用拍 —— 内核已经挡住了。
    before = workspace_fingerprint(project) if verify else {}
    if verify:
        note_incomplete_fingerprint(loop, before, "起跑前")

    cmd = reviewer_cmd(agent, project, pack, schema_file, rd / "review.json",
                       s.get("reviewer_model"), s.get("reviewer_effort"), verify=verify)

    rc = stream_subprocess(loop, cmd, project, log_file, s["timeout"],
                           on_event=parse_codex_event if agent == "codex" else None)

    if verify:
        after = workspace_fingerprint(project)
        # 跑完这张也要查完整性。只查起跑那张是不够的：git 在评审期间挂掉、仓库被
        # 挪走，after 就缺维度，而「缺的键不参与比较」会让它安静地看起来像没变过。
        note_incomplete_fingerprint(loop, after, "跑完后")
        moved = fingerprint_changed(before, after)
        # 指纹只知道工作区变没变，**不知道是谁变的** —— 这五分钟里你多半也在改自己的
        # 代码，那正是 rloop 支持的用法。所以作废要有第二个信号：reviewer 自己的日志
        # 里留下的动手证据。两个都有才作废，只有指纹动了就仅仅提醒。
        touched = tampered_dimensions(before, after)
        evidence = reviewer_write_evidence(log_file, project) if touched else []
        if touched and evidence:
            # 落进账本，报告里才看得到。给 claude 的 prompt 就是拿「会记进本轮报告」
            # 当约束的（那条路径触发不了作废），不写下来那句话又是空的。
            loop.update(fingerprint_note=f"**这一轮作废**：reviewer 动了被审的代码"
                                         f"（{'、'.join(touched)}）；证据：`{evidence[0][:160]}`")
            loop.log(f"  ! reviewer 动了被审的代码（{'、'.join(touched)}）—— 这一轮作废")
            loop.log(f"    证据：{evidence[0][:100]}")
            loop.log("    它只该评审不该动手。用 --no-verify 把它关回只读再跑。")
            loop.progress.emit("agent.error", "reviewer 改动了工作区，这一轮作废", "err",
                               {"changed": moved, "evidence": evidence[:5]})
            return EXIT_ERROR
        if touched:
            loop.update(fingerprint_note=f"工作区在评审期间变过（{'、'.join(touched)}），"
                                         f"reviewer 日志里没有动手痕迹 —— 结果照留")
            loop.log(f"  ! 工作区在评审期间变过（{'、'.join(touched)}）")
            # 这里不能说「它看的是起跑那一刻的快照」—— reviewer 直接跑在工作区上，
            # 读的是实时文件。补丁是起跑时定的，文件却可能是你刚改过的，两边对不上。
            loop.log("    reviewer 的日志里没有动手的痕迹，多半是你自己在改 —— 结果照留，"
                     "但它读的是实时文件，本轮判断可能落在改前改后的混合状态上。")
            loop.progress.emit("note", "工作区在评审期间变过", "warn", {"changed": moved})
        elif "status" in moved:
            # git 索引动了但文件内容没动 —— `git add` / `git rm --cached` 这类。
            # prompt 里明写着「动 git 状态 —— 不行」，那就不能一声不吭。
            loop.log("  ! git 索引在评审期间变过（暂存状态），文件内容没动")
        # 多出来的未跟踪文件单独说 —— 跑测试掉产物是常态，够不上作废，但它们**会进
        # 下一轮的送审范围**，不点名的话下一轮你会看见一堆自己没写过的"改动"。
        added = new_paths(before, after) if moved else []
        if added:
            loop.log(f"  ! reviewer 跑完后工作区多了 {len(added)} 个未跟踪文件"
                     f"（{'、'.join(added[:5])}{' …' if len(added) > 5 else ''}）")
            # 范围钉死在历史提交上时根本没有「下一轮的送审范围」这回事。
            loop.log("    这些文件会进下一轮的送审范围，该 gitignore 的记得加上。"
                     if s.get("diff_target") is None else
                     "    范围钉在历史提交上，它们进不了送审 diff，但会留在你的工作区里。")
            # 用现成的 note kind，不为这件事往对外契约里加第十四种 kind。
            loop.progress.emit("note", f"工作区多了 {len(added)} 个未跟踪文件", "warn",
                               {"added": added[:20]})
    if agent == "claude" and log_file.exists():
        (rd / "review.json").write_text(read_text_safe(log_file), encoding="utf-8")
    return rc


def process_group_alive(pgid: int) -> bool:
    """组里还有**没死透**的进程吗。

    不能只用 `killpg(pgid, 0)`：已退出但还没被 wait 的僵尸仍算组成员，信号 0
    照样成功，于是组看起来永远活着，TERM→KILL 的升级判定就卡死在那儿。所以
    先用 ps 看进程状态，把 Z 排除掉；ps 不可用时再退回信号检测（偏保守）。
    """
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-g", str(pgid)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return any(st.strip() and not st.strip().upper().startswith("Z")
                       for st in r.stdout.splitlines())
        # 非零通常就是「这个组没有进程」
        return False
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # 还在，只是不归我管


def kill_pgid(pgid: int, first=signal.SIGKILL, grace: float = 5.0) -> bool:
    """收掉整个进程组，返回是否确认组已空。

    要等的是**整个组**没了，不是直接子进程没了 —— 后者先退出时，忽略 TERM 的
    孙进程（shell、pytest）还在跑，早先那版在这里就 break 掉、SIGKILL 根本发
    不出去。所以每一档信号都轮询到组真空，不空就升级到 SIGKILL。
    """
    sigs = [first] if first == signal.SIGKILL else [first, signal.SIGKILL]
    for sig in sigs:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.time() + grace
        while time.time() < deadline:
            if not process_group_alive(pgid):
                return True
            time.sleep(0.05)
    return not process_group_alive(pgid)


_ACTIVE_CHILD = None    # 当前正在跑的子进程，给 SIGTERM handler 兜底用


def _terminate_with_child(signum, frame):
    """被 stop（或任何人）SIGTERM 时，先把自己起的 reviewer 收掉再走。

    补的是这个窗口：Popen 已经返回、child_pid 还没写进 loop.json 的那一瞬，
    stop 只看得到 runner，杀完它 reviewer 就成了孤儿继续烧配额。runner 自己
    手里一直握着 Popen 对象，不依赖账本，所以由它来兜底最可靠。

    handler 里只做 killpg 和 _exit，不碰文件、不起子进程 —— 信号上下文里做
    IO 不安全。用 os._exit 是刻意的：跳过 finally，状态原样停在 running，
    正好走「进程崩了」那条接管路径。
    """
    p = _ACTIVE_CHILD
    if p is not None:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    os._exit(143)


def kill_process_group(p, first=signal.SIGKILL) -> None:
    try:
        pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        return
    kill_pgid(pgid, first)
    with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
        p.wait(timeout=5)


def stream_subprocess(loop: Loop, cmd: list, cwd: Path, logfile: Path, timeout: int,
                      on_event=None) -> int:
    """跑子进程，stdout 落盘。返回 exit code；超时返回 -1。

    读 stdout 交给守护线程，超时由 p.wait(timeout=) 判定 —— 子进程静默挂起
    （既不输出也不退出）时超时依然生效。

    信号掩码和 _ACTIVE_CHILD 的恢复统统放在**一个** finally 里，不在各个分支
    手工清理：分支会漏。账本写失败这种路径带着异常退出时，早先会同时留下
    「SIGTERM 仍被屏蔽」和「_ACTIVE_CHILD 指着一个没人管的子进程」。
    """
    start = time.time()
    out = sys.stderr if JSON_MODE else sys.stdout
    p = None
    prev_mask = None
    rc = EXIT_ERROR

    def pump(stream, sink):
        try:
            for line in stream:
                sink.write(line)
                sink.flush()
                if on_event:
                    ev = on_event(line)
                    if ev:
                        # 先落盘再打印：别的进程（面板）看的是文件，自己的终端次之。
                        loop.progress.emit(ev["kind"], ev["text"], ev["level"],
                                           ev.get("data"))
                        msg = format_event(ev)
                        if msg:
                            print(msg, flush=True, file=out)
        except (ValueError, OSError):
            pass  # 进程被 kill 后管道提前关闭，不是错误

    try:
        # 从 Popen 到登记完成之间屏蔽 SIGTERM：子进程可能已经 fork 出来而
        # _ACTIVE_CHILD 还没赋值，这个窗口里收到信号，handler 找不到孩子就直接
        # 退出了，reviewer 就此成为孤儿。屏蔽期间的信号会挂起，解除后照常送达。
        with contextlib.suppress(AttributeError, ValueError, OSError):
            prev_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})

        with logfile.open("w", encoding="utf-8") as lf:
            try:
                p = subprocess.Popen(
                    cmd, cwd=str(cwd), text=True, errors="replace",
                    # 关掉 stdin：codex exec 即使给了 prompt 参数也会去读 stdin，
                    # 继承一个不会 EOF 的 stdin 时它就那么挂着 —— 静默 hang 的一种真实成因。
                    stdin=subprocess.DEVNULL,
                    # 独立进程组：reviewer 会 fork 出 shell、pytest 等后代，只对直接
                    # 子进程发信号杀不掉它们，超时之后会留下继续吃 CPU 的孤儿。
                    start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    bufsize=1,
                )
            except FileNotFoundError:
                loop.log(f"  ! 找不到可执行文件: {cmd[0]}")
                return 127

            globals()["_ACTIVE_CHILD"] = p      # 先挂上，再写账本
            loop.update(child_pid=p.pid, child_started=pid_field(p.pid, "lstart="))

            # 登记完就**立刻**恢复掩码，别拖到 finally —— 屏蔽要是盖住整个
            # p.wait()，直接发给 runner 的 SIGTERM（stop、关机、手工 kill）就得
            # 等 reviewer 自己退出或者跑满 timeout 才生效，_terminate_with_child
            # 形同虚设。屏蔽只为盖住「子进程已存在但还没登记」那一小段。
            if prev_mask is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.pthread_sigmask(signal.SIG_SETMASK, prev_mask)
                prev_mask = None            # 已恢复，finally 不必再动

            reader = threading.Thread(target=pump, args=(p.stdout, lf), daemon=True)
            reader.start()
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_process_group(p)
                reader.join(timeout=2)
                loop.log(f"  ! 超时 {timeout}s，已终止（含派生进程）")
                return -1
            except KeyboardInterrupt:
                kill_process_group(p, first=signal.SIGTERM)
                raise
            finally:
                loop.update(child_pid=None)
            reader.join(timeout=5)
    finally:
        # 兜底：正常路径在登记后就已经恢复过掩码并把 prev_mask 置空了，这里只
        # 收拾异常路径。全局句柄和活着的子进程则是无条件清理。
        # 正常路径已经 wait 过它了，poll() 不会是 None；能走到这还活着的，
        # 说明是异常退出，那它就是个没人管的孤儿。
        if prev_mask is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.pthread_sigmask(signal.SIG_SETMASK, prev_mask)
        globals()["_ACTIVE_CHILD"] = None
        if p is not None and p.poll() is None:
            kill_process_group(p)

    elapsed = int(time.time() - start)
    loop.log(f"  {cmd[0]} 退出码={rc} 耗时={elapsed}s 日志={logfile.name}")
    return rc


# ─────────────────────────── 判定 ───────────────────────────


def load_review(loop: Loop, rnd: int) -> dict | None:
    path = loop.round_path(rnd) / "review.json"
    if not path.exists():
        return None
    raw = read_text_safe(path).strip()
    if not raw:
        return None
    # 容错：模型可能包 markdown fence、带语言标注、或在前后加几句废话。
    # 直接截取最外层的 {...}，比按行剥 fence 稳 —— 后者遇到整段挤在一行的
    # fence 会 IndexError，绕过本函数「脏输入返回 None」的契约。
    lo, hi = raw.find("{"), raw.rfind("}")
    if lo == -1 or hi <= lo:
        return None
    raw = raw[lo:hi + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for k in ("deliverable_maturity", "production_readiness", "blocking_findings", "verdict"):
        if k not in data:
            return None
    return data


def review_consistency_errors(review: dict, min_score: float,
                              prior_findings: list | None = None) -> list[str]:
    """检查 reviewer 自报的判定是否自洽。

    schema 能保证字段齐全、类型正确，但表达不了字段之间的一致性：模型完全可以
    报 blocking_findings=0 的同时在 findings 里列一条 critical。真发生时，只信
    自报数字的门禁会把它当成达标、以退出码 0 放行。所以这里交叉核对，对不上就
    拒绝给出可信判定，把矛盾原样交回去让人看。
    """
    errs = []
    try:
        dm = float(review["deliverable_maturity"])
        pr = float(review["production_readiness"])
        blocking = int(review["blocking_findings"])
    except (TypeError, ValueError, KeyError) as e:
        return [f"分数字段解析不了：{e}"]

    for name, v in (("deliverable_maturity", dm), ("production_readiness", pr)):
        if not (0.0 <= v <= 10.0):
            errs.append(f"{name}={v} 超出 0-10")

    findings = review.get("findings") or []
    actual = sum(1 for f in findings
                 if (f.get("severity") or "").lower() in ("critical", "high"))
    if blocking != actual:
        errs.append(
            f"blocking_findings 自报 {blocking}，但 findings 里实际有 {actual} 条 critical/high"
        )

    ids = [f.get("id") for f in findings]
    if len(set(ids)) != len(ids) and all(ids):
        errs.append(f"finding id 有重复：{[i for i in set(ids) if ids.count(i) > 1]}")

    # 无状态 reviewer 的连续性全靠这份逐条裁决。漏一条，那条问题就悄悄消失了。
    got = [p.get("id") for p in (review.get("prior_findings_status") or [])]
    want = [f.get("id") for f in (prior_findings or []) if f.get("id")]
    if prior_findings and not want:
        # 上一轮是加 id 之前产生的，没有可比对的键。跳过覆盖校验而不是把整轮判成
        # 不自洽——否则一个跨版本的 loop 会永远续不下去。
        pass
    elif want:
        missing = [i for i in want if i not in got]
        unknown = [i for i in got if i not in want]
        dupes = sorted({i for i in got if got.count(i) > 1})
        if missing:
            errs.append(f"上轮 findings {missing} 没有给出裁决（prior_findings_status 漏项）")
        if unknown:
            errs.append(f"prior_findings_status 里有上轮不存在的 id：{unknown}")
        if dupes:
            errs.append(f"prior_findings_status 里 id 重复：{dupes}")
    elif got:
        errs.append(f"这是第一轮，不该有 prior_findings_status，却给了 {len(got)} 条")

    # 判成没修好的问题必须仍出现在 findings 里。否则它从活跃清单上消失，
    # 下一轮没人再管它，甚至可能就这么通过门禁。
    unresolved = {p.get("id") for p in (review.get("prior_findings_status") or [])
                  if p.get("status") in ("not_fixed", "partially_fixed") and p.get("id")}
    still_listed = {i for i in ids if i}
    dropped = sorted(unresolved - still_listed)
    if dropped:
        errs.append(
            f"{dropped} 被判为没修好，却没有出现在本轮 findings 里 —— "
            f"仍然存在的问题必须沿用同一个 id 重新列出，否则它就从账本上消失了"
        )

    verdict = (review.get("verdict") or "").lower()
    should_pass = dm >= min_score and pr >= min_score and actual == 0
    if verdict == "pass" and not should_pass:
        errs.append(
            f"verdict=pass，但双评分 {dm}/{pr}（门槛 {min_score}）与 {actual} 条阻塞项对不上"
        )
    if verdict == "needs_work" and should_pass:
        errs.append(
            f"verdict=needs_work，但双评分 {dm}/{pr} 已达门槛 {min_score} 且无阻塞项"
        )
    return errs


def gate_pass(review: dict, min_score: float) -> bool:
    return (
        float(review["deliverable_maturity"]) >= min_score
        and float(review["production_readiness"]) >= min_score
        and int(review["blocking_findings"]) == 0
    )


def detect_stall(history: list) -> bool:
    """连续 STALL_ROUNDS 轮两个分数都没提升且 blocking 没下降 → 停滞。"""
    if len(history) < STALL_ROUNDS + 1:
        return False
    window = history[-(STALL_ROUNDS + 1):]
    for a, b in zip(window, window[1:]):
        d_up = b["deliverable_maturity"] - a["deliverable_maturity"] > STALL_EPSILON
        p_up = b["production_readiness"] - a["production_readiness"] > STALL_EPSILON
        blk_down = b["blocking_findings"] < a["blocking_findings"]
        if d_up or p_up or blk_down:
            return False
    return True


# ─────────────────────────── 报告与通知 ───────────────────────────


def fingerprint_note(s: dict) -> str:
    """报告抬头里那句「这个 reviewer 当时是什么档位」。"""
    return "能跑测试，不能改代码" if s.get("verify", True) else "只读子进程"


def render_report(loop: Loop) -> str:
    s = loop.state
    lines = [
        f"# rloop 报告 — {s['id']}",
        "",
        f"- 项目：`{s['project']}`",
        f"- 范围：{s['scope_desc']}",
        *([f"- 侧重：{s['focus']}"] if s.get("focus") else []),
        f"- 审阅：**{s['reviewer']}**（{fingerprint_note(s)}）；改动由调用方的开发会话完成",
        *([f"- 工作区核对：{s['fingerprint_note']}"] if s.get("fingerprint_note") else []),
        f"- 结果：**{s.get('outcome', '进行中')}**（{s.get('outcome_reason', '')}）",
        f"- 轮次：{s.get('round', 0)} / {s['max_rounds']}，阈值 {s['min_score']}",
        "",
        "## 分数走势",
        "",
        render_score_history(s.get("history", [])),
        "",
    ]
    last = s.get("history", [])
    if last:
        rnd = last[-1]["round"]
        rev = load_review(loop, rnd)
        if rev:
            lines += ["## 最终评审意见", "", rev.get("summary", ""), ""]
            if rev.get("findings"):
                title = ("### 非阻塞 findings（未达阻塞级，可选处理）"
                         if s.get("outcome") == "converged" else "### 遗留 findings")
                lines += [title, "", render_findings_for_author(rev), ""]
            if rev.get("next_priorities"):
                lines += ["### 下一步优先级", ""]
                lines += [f"{i}. {p}" for i, p in enumerate(rev["next_priorities"], 1)]
                lines.append("")
            if rev.get("validation_commands"):
                lines += ["### 验证", "", "| 命令 | 结果 | 备注 |", "|---|---|---|"]
                for v in rev["validation_commands"]:
                    lines.append(f"| `{v['command']}` | {v['outcome']} | {v.get('note', '')} |")
                lines.append("")
    return "\n".join(lines)


def notify(loop: Loop, title: str, body: str):
    s = loop.state
    mode = s.get("notify", "macos")
    if mode == "none":
        return
    if mode == "macos":
        safe_t = title.replace('"', "'")
        safe_b = body.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_b}" with title "{safe_t}" sound name "Glass"'],
            check=False, stderr=subprocess.DEVNULL,
        )
    elif mode == "cmd" and s.get("notify_cmd"):
        subprocess.run(
            s["notify_cmd"], shell=True, check=False,
            env={**os.environ, "RLOOP_TITLE": title, "RLOOP_BODY": body,
                 "RLOOP_ROOT": str(loop.root)},
        )


# ─────────────────────────── 主循环 ───────────────────────────


def default_branch(project: Path) -> str | None:
    """猜这个仓库的主干分支名，优先返回远端引用形式（`origin/main`）。

    不能把 `refs/remotes/origin/HEAD` 退化成本地的 `main`：本地主干经常落后于远端，
    拿落后的本地分支求 merge-base，会把早已属于上游的提交也算进送审范围。
    """
    head = run_git(project, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    if head.startswith("refs/remotes/"):
        ref = head[len("refs/remotes/"):]            # refs/remotes/origin/main → origin/main
        if run_git(project, "rev-parse", "--verify", "--quiet", ref).strip():
            return ref
    for name in ("origin/main", "origin/master", "main", "master", "develop"):
        if run_git(project, "rev-parse", "--verify", "--quiet", name).strip():
            return name
    return None


def determine_scope(project: Path, args) -> tuple[str, str | None, str]:
    """决定评审范围，返回 (diff_base, diff_target, 人话描述)。

    diff_target 为 None 表示 diff 到当前工作树；为 sha 表示终点被钉死在那个提交上。

    显式 --base / --commit 优先；否则按「审你还没定稿的东西」逐级回退：
    工作区有改动 → 审未提交改动；干净 → 审相对主干分支的改动；仍没有 → 审最后一个 commit。
    """
    # 必须带 --verify --quiet：裸 `rev-parse HEAD` 在还没有提交的仓库里会把字面量
    # "HEAD" 打到 stdout（错误只走 stderr），空仓库的判断会因此整个失效。
    head = run_git(project, "rev-parse", "--verify", "--quiet", "HEAD").strip()
    if not head:
        die("repository has no commits yet — nothing to review.")

    if args.commit:
        sha = run_git(project, "rev-parse", "--verify", f"{args.commit}^{{commit}}").strip()
        if not sha:
            die(f"unknown commit: {args.commit}")
        parent = run_git(project, "rev-parse", "--verify", f"{sha}^").strip()
        if not parent:
            die(f"{args.commit} is a root commit; nothing to diff against.")
        subject = run_git(project, "log", "-1", "--format=%s", sha).strip()
        dirty = run_git(project, "status", "--porcelain",
                        "--", ".", f":(exclude){LOOP_DIRNAME}").strip()
        if sha == head and not dirty:
            # 终点就是工作树本身，不用钉；后续改动因此还能进下一轮 diff
            return parent, None, (
                f"the changes introduced by commit {sha[:12]} ({subject}), "
                f"which is HEAD, plus anything added on top"
            )
        return parent, sha, (
            f"only the changes introduced by commit {sha[:12]} ({subject}) — later commits "
            f"and the current working tree are deliberately excluded"
        )

    if args.base:
        merge_base = run_git(project, "merge-base", args.base, "HEAD").strip()
        if not merge_base:
            die(f"cannot find a merge base between {args.base} and HEAD")
        return merge_base, None, (
            f"everything on this branch since it diverged from `{args.base}` "
            f"at {merge_base[:12]}, including uncommitted work"
        )

    dirty = run_git(project, "status", "--porcelain",
                    "--", ".", f":(exclude){LOOP_DIRNAME}").strip()
    if dirty:
        n = len(dirty.splitlines())
        return head, None, (
            f"the uncommitted changes in the working tree ({n} files), against HEAD {head[:12]}"
        )

    branch = default_branch(project)
    if branch:
        merge_base = run_git(project, "merge-base", branch, "HEAD").strip()
        if merge_base and merge_base != head:
            return merge_base, None, (
                f"everything committed on this branch since it diverged from `{branch}` "
                f"at {merge_base[:12]} (working tree is clean)"
            )

    parent = run_git(project, "rev-parse", "--verify", "HEAD^").strip()
    if not parent:
        die("working tree is clean and HEAD is the only commit — nothing to review.")
    subject = run_git(project, "log", "-1", "--format=%s").strip()
    return parent, None, f"the last commit {head[:12]} ({subject}) — working tree is clean"


@contextlib.contextmanager
def project_lock(project: Path):
    """项目级锁，只圈住「查找 active loop → 决定续还是新建」这一小段。

    per-loop 锁保护不了首次启动：两个进程可以同时看到"没有 active loop"，
    于是各自新建一个目录、各自拿到**不同**的 loop 锁，然后并行跑同一份范围。
    临界区很短（几次文件读 + 一次 mkdir），所以这里阻塞等而不是报 busy。
    """
    d = project / LOOP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    f = (d / ".project.lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


@contextlib.contextmanager
def loop_lock(root: Path):
    """对一个 loop 目录加跨进程排他锁。

    没有它，两个并发的裸 `rloop` 会从 find_open_loop() 拿到同一个 open loop，
    读到同一个 round，同时起 reviewer，然后互相覆盖 round-NN 目录、child_pid
    和 history —— 原子写只能保证单个 JSON 不半写，防不住丢失更新。
    """
    root.mkdir(parents=True, exist_ok=True)
    f = (root / ".lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        die(f"loop {root.name} 正在被另一个 rloop 进程跑着。\n"
            f"等它结束再续，或用 --new 另起一个。")
    try:
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def find_active_loop(project: Path) -> Loop | None:
    """当前项目里最近一个还没了结的 loop。

    `open`（上一轮跑完没达标，等着续）和 `running`（有进程正在跑，或者跑的那个
    进程死了没收尾）都算。**必须把 running 也算进来**：否则 A 一旦把状态改成
    running，B 就看不见任何 open loop，转头新建第二个 loop 并行跑同一份范围 ——
    每-loop 锁挡不住这条路，因为两边根本不在争同一个锁。
    """
    d = project / LOOP_DIRNAME
    if not d.is_dir():
        return None
    # 按 started_at 选最近的。loop id 只到秒，尾巴是随机后缀，字典序不等于创建顺序 ——
    # 同一秒用 --new 连开两个，按名字排会续到错误的那个。
    opens = []
    for cand in (x for x in d.iterdir() if (x / "loop.json").exists()):
        try:
            st = json.loads(read_text_safe(cand / "loop.json"))
        except (json.JSONDecodeError, OSError):
            continue
        if st.get("status") in ("open", "running"):
            # created_ns 是纳秒级单调序号，秒级的 started_at 只是它缺席时的退路
            opens.append((st.get("created_ns") or 0, st.get("started_at") or "",
                          cand.name, cand))
    if not opens:
        return None
    opens.sort(reverse=True)
    return Loop(opens[0][3])


def emit_json(loop: Loop, code: int):
    """把这一轮的结果打到 stdout 给调用方（你的会话）解析。"""
    s = loop.state
    rnd = s.get("round", 0)
    review = load_review(loop, rnd) or {}
    outcome = s.get("outcome") or s.get("status")
    # 作废那一轮的产物**一律不往外发**。reviewer 动过被审代码，它这一轮的 findings
    # 和评分就建立在一份自己改过的代码上 —— 而 review.json 是 codex 进程在作废判定
    # 之前就写好的，照常读得出来。交给驱动会话的话，它会拿着一份已经被判为不可信的
    # 清单去改代码，「作废」这两个字就白说了。
    voided = (s.get("fingerprint_note") or "").startswith("**这一轮作废**")
    if voided:
        review = {}
    payload = {
        "loop_id": s["id"],
        "project": s["project"],
        "round": rnd,
        "max_rounds": s["max_rounds"],
        "scope": s["scope_desc"],
        "focus": s.get("focus"),
        "min_score": s["min_score"],
        "exit_code": code,
        "outcome": outcome,
        "outcome_reason": s.get("outcome_reason"),
        # 还能不能接着跑下一轮。false 时不要再调 rloop，把结论交给人。
        "can_continue": s.get("status") == "open",
        # 范围钉死在历史提交上时，你改工作区也不会进入送审 diff —— 别改，只报告。
        "fix_allowed": s.get("diff_target") is None,
        "consistency_errors": s.get("consistency_errors") or [],
        # 这一轮 reviewer 是带写权限跑的还是只读跑的 —— 驱动会话据此判断
        # validation_commands 到底是真跑出来的还是静态推断的。
        "verify": bool(s.get("verify", True)),
        # 被保险作废了：下面的 scores / findings 全是空的，别拿它做判断。
        "voided": voided,
        "scores": {
            "deliverable_maturity": review.get("deliverable_maturity"),
            "production_readiness": review.get("production_readiness"),
            "blocking_findings": review.get("blocking_findings"),
            "verdict": review.get("verdict"),
        },
        "summary": review.get("summary"),
        "findings": review.get("findings") or [],
        "prior_findings_status": review.get("prior_findings_status") or [],
        "next_priorities": review.get("next_priorities") or [],
        "validation_commands": review.get("validation_commands") or [],
        "history": s.get("history", []),
        "report_path": str(loop.root / "report.md"),
        "response_path": str(loop.round_path(rnd) / "response.md") if rnd else None,
        "patch_path": str(loop.round_path(rnd) / "diff.patch") if rnd else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_review(args) -> int:
    global JSON_MODE
    JSON_MODE = args.json

    if args.max_rounds is not None and args.max_rounds < 1:
        die(f"--max-rounds 至少是 1，给的是 {args.max_rounds}")
    if args.timeout is not None and args.timeout < 1:
        die(f"--timeout 至少是 1 秒，给的是 {args.timeout}")
    if args.min_score is not None and not (0.0 <= args.min_score <= 10.0):
        # nan 会让这个比较为假，正好一并挡住 —— 否则门槛永远达不到，
        # loop 会一路跑到熔断才停，全程照常烧配额。
        die(f"--min-score 要在 0 到 10 之间，给的是 {args.min_score}")

    project = Path(args.directory or os.getcwd()).resolve()
    if not project.is_dir():
        die(f"not a directory: {project}")
    if not (project / ".git").exists():
        die(f"not a git repository: {project}\n"
            "rloop 用 git diff 界定评审范围。")

    scope_flags = bool(args.base or args.commit or args.focus)

    # 仅供测试：卡在这里直到屏障文件出现，好让两个进程在「都还没看到任何 loop」
    # 的状态下同时冲进下面的临界区。没有它，测试只能在 A 建完 loop 之后才放 B
    # 进来，那验证的是 per-loop 锁，根本碰不到首次创建竞态。
    _barrier = os.environ.get("RLOOP_TEST_BARRIER")
    if _barrier:
        _b, _deadline = Path(_barrier), time.time() + 30
        while not _b.exists() and time.time() < _deadline:
            time.sleep(0.02)

    # 从这里到「拿到某个 loop 的锁」为止都在项目锁里：否则两个首次裸启动会同时
    # 看到没有 active loop，各自建一个目录、各自拿到不同的 loop 锁，并行跑同一
    # 份范围。项目锁在拿到 loop 锁之后就释放，后来者于是能看见新建的那个。
    # 测试专用逃生口：关掉项目锁，用来自动验证那条竞态用例真的抓得住竞态
    # （F5 的判据：先证明删掉锁会让它稳定失败）。
    pl = (contextlib.nullcontext() if os.environ.get("RLOOP_TEST_NO_PROJECT_LOCK")
          else project_lock(project))
    pl.__enter__()
    loop = None if (args.new or scope_flags) else find_active_loop(project)

    # 仅供测试，且只在无锁变异模式下生效：读完 active loop 之后再卡一次，确保
    # 两个进程都已经读到"没有 active loop"才继续去建目录。没有它，变异版可能
    # 是一方建完另一方才被调度 —— 那样它只会看到 running loop 去争 per-loop 锁，
    # 于是只建出一个目录，变异测试假阴性。
    if os.environ.get("RLOOP_TEST_NO_PROJECT_LOCK"):
        _b2 = os.environ.get("RLOOP_TEST_BARRIER2")
        if _b2:
            _p2 = Path(_b2)
            # 先报到：测试靠数 ready 文件确认两边都跑完 find_active_loop 了，
            # 不用 sleep 去猜。猜不准的话，无锁版可能一方先建完目录，另一方
            # 只看到 running loop —— 变异测试就白通过了。
            _ready = _p2.parent / f"{_p2.name}.ready.{os.getpid()}"
            _ready.write_text("1", encoding="utf-8")
            _dl2 = time.time() + 30
            while not _p2.exists() and time.time() < _dl2:
                time.sleep(0.02)

    if loop is not None:
        # 续上一个还开着的 loop：范围、门槛、角色都沿用，轮次递增。
        # 从这里到本轮跑完全程持锁 —— 选中和执行之间要是没有互斥，两个并发的裸
        # rloop 会挑中同一个 loop、跑同一轮、互相覆盖账本。
        try:
            lock = loop_lock(loop.root)
            lock.__enter__()
        except SystemExit:
            pl.__exit__(None, None, None)   # 别把项目锁一起带走
            raise
        # 拿到锁才走到这里。拿不到说明真有进程在跑，loop_lock 已经报 busy 退出了。
        s = loop.state          # 持锁之后重读
        if s.get("status") == "running":
            # 状态是 running 却没人持锁 —— 上次那个进程死了没收尾。接管它。
            loop.log("")
            loop.log(f"loop {s['id']} 上次运行没有正常收尾（status=running），接管")
            # 遗留的 pid 属于一个已经不在了的进程，而 pid 会被系统回收复用。
            # 留着它们，之后一次 stop 就可能照着这些号码去杀无关进程。
            loop.update(child_pid=None, child_started=None,
                        runner_pid=None, runner_started=None)
            s = loop.state
            if s.get("round", 0) > 0 and load_review(loop, s["round"]) is None:
                # 那一轮的 reviewer 没留下可用产物，这轮重跑它
                loop.update(round=s["round"] - 1)
                loop.log(f"  第 {s['round']} 轮没有可用的 review.json，重跑该轮")
                s = loop.state
        elif s.get("status") != "open":
            lock.__exit__(None, None, None)
            pl.__exit__(None, None, None)
            die(f"loop {s['id']} 已经不是 open 了（现在是 {s.get('status')}），"
                f"多半是另一个 rloop 刚跑完它。\n裸调 rloop 会另起一个新 loop。")
        else:
            loop.log("")
            loop.log(f"续上 loop {s['id']}（已跑 {s['round']} 轮）")
        # 换 reviewer 或改门槛会让轮次之间不可比 —— 与其静默沿用旧值（CLI 上写着
        # 一套、实际跑着另一套），不如直接拦下来让人明确表态。
        for key, given, label in (("reviewer", args.reviewer, "--reviewer"),
                                  ("min_score", args.min_score, "-m/--min-score")):
            if given is not None and given != s.get(key):
                die(f"loop {s['id']} 的 {key} 是 {s.get(key)}，续轮改成 {given} 会让"
                    f"前后几轮不可比。\n"
                    f"要换就用 {label} ... --new 另起一个 loop。")

        # 这些不影响可比性，允许中途调
        for key, given, label in (
            ("reviewer_effort", args.effort or args.reviewer_effort, "推理档位"),
            ("reviewer_model", args.reviewer_model, "reviewer 模型"),
            ("max_rounds", args.max_rounds, "轮数上限"),
            ("timeout", args.timeout, "单轮超时"),
            ("notify", args.notify, "通知方式"),
            ("notify_cmd", args.notify_cmd, "通知命令"),
        ):
            if given is not None and given != s.get(key):
                loop.update(**{key: given})
                loop.log(f"  {label}改为 {given}")

        # `--no-verify` 续轮时也得算数。它先前只在新建 loop 的分支里读，于是
        # 「loop 开着的时候改主意想收紧权限」这条路上，开关是一句静默失效的咒语：
        # loop.json 里 verify 还是 true，reviewer 照旧拿 workspace-write 起跑，
        # 终端上一个字都不说。**只认收紧这一个方向** —— store_true 分不出「没给」
        # 和「给了 false」，而放开权限绝不能靠一个分不清的默认值推断出来。想从
        # 只读改回放开，用 --new 另起一个 loop。
        if args.no_verify and s.get("verify", True):
            loop.update(verify=False)
            loop.log("  reviewer 关回只读（--no-verify）")
            s = loop.state

        prev_resp = loop.round_path(s["round"]) / "response.md"
        if not prev_resp.exists():
            loop.log(f"  ! 上一轮没有 {prev_resp.name}，reviewer 将无从判断你做了什么，"
                     f"很可能把上轮 findings 全判成 not_fixed")
    else:
        reviewer = args.reviewer or DEFAULT_REVIEWER
        effort = args.effort or args.reviewer_effort or DEFAULT_EFFORT
        max_rounds = args.max_rounds if args.max_rounds is not None else DEFAULT_MAX_ROUNDS
        min_score = args.min_score if args.min_score is not None else DEFAULT_MIN_SCORE
        timeout = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT

        diff_base, diff_target, scope_desc = determine_scope(project, args)

        # loop 自己的产物不能进入待评审的 diff。用 .git/info/exclude 而不是
        # .gitignore，免得往用户的仓库里塞东西。
        exclude = project / ".git" / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            cur = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if LOOP_DIRNAME not in cur:
                with exclude.open("a", encoding="utf-8") as f:
                    f.write(f"\n# added by rloop\n{LOOP_DIRNAME}/\n")
        except OSError:
            pass

        probe = scope_diff(project, diff_base, diff_target, stat=True).strip()
        untracked = [] if diff_target else list_untracked(project)
        if not probe and not untracked:
            die(f"nothing to review — no diff against {diff_base[:12]}.")

        focus = " ".join(args.focus).strip() if args.focus else None
        # 加随机后缀：同一秒内连开两个 loop（--new、脚本连跑）不能撞同一个目录
        loop_id = f"{stamp()}-{reviewer[0]}-{os.urandom(2).hex()}"
        root = project / LOOP_DIRNAME / loop_id
        root.mkdir(parents=True, exist_ok=True)
        loop = Loop(root)
        loop.save({
            "id": loop_id,
            "version": VERSION,
            "project": str(project),
            "label": (args.label or "").strip() or None,
            # reviewer 能不能跑命令验证。默认能 —— 拿不到实证的评审只能靠读代码
            # 猜，production_readiness 也就永远封在 5 分。
            "verify": not args.no_verify,
            "focus": focus,
            "diff_base": diff_base,
            "diff_target": diff_target,
            "scope_desc": scope_desc,
            "reviewer": reviewer,
            "reviewer_model": args.reviewer_model,
            "reviewer_effort": effort,
            "max_rounds": max_rounds,
            "min_score": min_score,
            "timeout": timeout,
            "notify": args.notify or DEFAULT_NOTIFY,
            "notify_cmd": args.notify_cmd,
            "round": 0,
            "history": [],
            "status": "running",
            "runner_pid": None,     # 跑这个 loop 的 rloop 进程自己
            "child_pid": None,      # 它当前起的 reviewer
                "started_at": now_iso(),
            # started_at 只到秒。同一秒里连开两个 loop 时，秒级时间戳分不出先后，
            # 排序就退回按目录名 —— 而名字尾巴是随机的，于是可能续到更早那个。
            "created_ns": time.time_ns(),
            "updated_at": now_iso(),
        })
        registry_put(loop_id, root, project, focus or scope_desc)

        loop.log(f"rloop {VERSION} — loop {loop_id}")
        loop.log(f"  项目   {project}")
        loop.log(f"  范围   {scope_desc}")
        if focus:
            loop.log(f"  侧重   {focus}")
        loop.log(f"  审阅   {reviewer}（{'能跑测试，不能改代码' if not args.no_verify else '只读'}）")
        loop.log(f"  门槛   双评分 >= {min_score}，blocking_findings == 0")
        loop.log(f"  上限   {max_rounds} 轮，单轮 {timeout}s")
        if reviewer == "claude" and not args.no_verify:
            # codex 那边放开写权限还有内核兜底，claude 这边没有：权限是工具层的，
            # 放开就是真放开。不该让人从「默认值」里默默继承这个差别。
            loop.log("  注意   claude 侧没有操作系统沙箱，放开写权限就是真放开；"
                     "审不信任的代码请加 --no-verify")
        if diff_target:
            loop.log("  注意   范围钉在历史提交上，你改工作区不会进入送审 diff")
        loop.log("")
        lock = loop_lock(loop.root)
        lock.__enter__()

    pl.__exit__(None, None, None)      # 已经攥住具体 loop 了，放开项目锁

    try:
            code = run_one_round(loop)
    finally:
        lock.__exit__(None, None, None)

    # 结论比分数值钱得多：findings、上一轮的裁决、它到底跑了什么、下一步建议
    # 先做什么 —— 这些一律要打给人看。`--json` 时走 stderr，stdout 留给调用方。
    print_round_result(loop, sys.stderr if args.json else sys.stdout)
    if args.json:
        emit_json(loop, code)
    return code


def finish(loop: Loop, outcome: str, reason: str, code: int) -> int:
    """收尾：落状态、写报告、通知、返回退出码。"""
    loop.update(status="done", outcome=outcome, outcome_reason=reason)
    try:
        (loop.root / "report.md").write_text(render_report(loop), encoding="utf-8")
        loop.log("")
        loop.log(f"结果：{outcome} — {reason}")
        loop.log(f"报告：{loop.root / 'report.md'}")

        icons = {"converged": "✅", "needs_work": "📋", "inconsistent": "⁉️",
                 "stalled": "⚠️", "exhausted": "⚠️", "pinned_scope": "📌", "failed": "❌"}
        h = loop.state.get("history", [])
        tail = (f" 交付物 {h[-1]['deliverable_maturity']} / 生产就绪 {h[-1]['production_readiness']}"
                if h else "")
        notify(loop, f"{icons.get(outcome, '•')} rloop {outcome}",
               f"{loop.state['id']} — {reason}.{tail}")
        return code
    finally:
        # 走 finally：报告渲染或通知万一抛了，面板也不会永远挂在「运行中」等一个
        # 不会来的收尾事件。放在末尾则保证它确实是这一轮的最后一条 —— 面板见到
        # 它就可以停止 tail 这个文件。
        loop.progress.emit("run.end", f"{outcome} — {reason}", "highlight",
                           {"exit_code": code, "outcome": outcome,
                            "outcome_reason": reason})


def run_one_round(loop: Loop) -> int:
    """跑一轮 review。

    只跑一轮就返回——循环由调用方驱动（通常是你正在用的开发会话，它自带这份代码的
    完整上下文，处理 findings 的也是它）。rloop 自己不改任何代码。

    退出码：0 达标 / 2 未达标 / 3 reviewer 自相矛盾 / 1 出错。
    """
    s = loop.state
    rnd = s["round"] + 1
    with contextlib.suppress(ValueError):   # 非主线程装不了信号，忽略即可
        signal.signal(signal.SIGTERM, _terminate_with_child)
    started = pid_field(os.getpid(), "lstart=")
    # 同一轮可能被接管重跑，光看 round 分不出是哪一次运行；迟到的旧事件会污染新一次。
    run_id = f"{os.getpid()}-{time.time_ns()}"
    loop.update(round=rnd, status="running",
                runner_pid=os.getpid(), runner_started=started, run_id=run_id)
    loop.round_dir(rnd)                     # 先把轮次目录建出来，进度才有地方落
    loop.progress = ProgressWriter(loop.root, rnd, s["id"], run_id)
    loop.progress.emit("run.start", f"第 {rnd} 轮开始", "info", {
        "pid": os.getpid(), "run": run_id, "reviewer": s["reviewer"],
        "reviewer_model": s.get("reviewer_model"),
        "reviewer_effort": s.get("reviewer_effort"),
        "scope": s.get("scope_desc"), "focus": s.get("focus"),
        "round": rnd, "max_rounds": s.get("max_rounds"),
    })
    loop.log(f"── 第 {rnd} 轮 " + "─" * 40)

    loop.progress.emit("phase", "reviewer 开始评审", "info", {"name": "reviewer"})
    loop.log(f"  reviewer ({s['reviewer']}) 开始评审…")
    rc = run_reviewer(loop, rnd)
    if rc != 0:
        # 作废和「reviewer 自己崩了」都走这条路，但两者对调用方的含义完全不同 ——
        # 账本里只留一句 "reviewer exit 1" 的话，事后没人分得清这一轮是怎么没的。
        note = loop.state.get("fingerprint_note") or ""
        if note.startswith("**这一轮作废**"):
            loop.log("  ! 本轮作废：reviewer 动了被审的代码")
            return finish(loop, "failed", "reviewer 改动了被审代码，本轮判定不可信", EXIT_ERROR)
        loop.log(f"  ! reviewer 失败（退出码 {rc}）")
        return finish(loop, "failed", f"reviewer exit {rc}", EXIT_ERROR)

    review = load_review(loop, rnd)
    if review is None:
        loop.log("  ! reviewer 没有产出合法的 review.json")
        return finish(loop, "failed", "reviewer 没有产出合法的 review.json", EXIT_ERROR)

    missing_ids = [f for f in (review.get("findings") or []) if not f.get("id")]
    if missing_ids:
        # schema 要求了 id，但模型不总是给。补一个序号键，好歹能跨轮引用；
        # 只是它没法跟上一轮的同一个问题对上，所以要说出来。
        for n, f in enumerate(review.get("findings") or [], 1):
            f.setdefault("id", f"R{rnd}F{n}")
            if not f["id"]:
                f["id"] = f"R{rnd}F{n}"
        (loop.round_dir(rnd) / "review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        loop.log(f"  ! reviewer 有 {len(missing_ids)} 条 finding 没给 id，已补成 R{rnd}F* "
                 f"（跨轮追踪会断，下一轮它认不出这是同一个问题）")

    usage = parse_reviewer_usage(loop.round_path(rnd) / "reviewer.log")
    entry = {
        "round": rnd,
        "deliverable_maturity": float(review["deliverable_maturity"]),
        "production_readiness": float(review["production_readiness"]),
        "blocking_findings": int(review["blocking_findings"]),
        "verdict": review["verdict"],
        "at": now_iso(),
    }
    if usage:
        entry["usage"] = usage
    history = loop.state.get("history", []) + [entry]
    loop.update(history=history)
    write_round_markdown(loop, rnd, review)
    loop.progress.emit(
        "score",
        f"交付物={entry['deliverable_maturity']} 生产就绪={entry['production_readiness']} "
        f"阻塞项={entry['blocking_findings']} → {entry['verdict']}",
        "highlight", entry)

    loop.log(
        f"  评分   交付物={entry['deliverable_maturity']} "
        f"生产就绪={entry['production_readiness']} "
        f"阻塞项={entry['blocking_findings']} → {entry['verdict']}"
    )
    if usage:
        loop.log(f"  用量   输入 {usage['input']:,}（缓存命中 "
                 f"{usage['cached'] * 100 // max(1, usage['input'])}%，"
                 f"实付 {usage['fresh']:,}） 输出 {usage['output']:,}")
    if review.get("summary"):
        loop.log(f"  小结   {review['summary'][:200]}")

    # 自洽校验在门禁之前：自相矛盾的结果不配被信任，无论它说自己达没达标。
    prior = None
    if rnd > 1:
        prev = load_review(loop, rnd - 1)
        prior = (prev or {}).get("findings")
    errs = review_consistency_errors(review, s["min_score"], prior)
    if errs:
        for e in errs:
            loop.log(f"  ! 不自洽：{e}")
        loop.update(consistency_errors=errs)
        return finish(loop, "inconsistent", "; ".join(errs), EXIT_INCONSISTENT)
    loop.update(consistency_errors=[])

    if gate_pass(review, s["min_score"]):
        return finish(loop, "converged", f"第 {rnd} 轮达标", EXIT_PASS)

    if not review.get("findings"):
        # 未达标却一条 findings 都不给：没有任何可依据的改动方向。这一条必须排在
        # stall / max_rounds 之前 —— 否则最后一轮或停滞窗口里出现同样的矛盾，
        # 会被标成 exhausted/stalled 退 2，把"结果不可信"伪装成"没跑到点上"。
        reason = (f"未达标（阻塞项 {entry['blocking_findings']}）"
                  f"但 reviewer 一条 findings 都没给")
        loop.log(f"  ! {reason}")
        return finish(loop, "inconsistent", reason, EXIT_INCONSISTENT)

    if detect_stall(history):
        reason = f"连续 {STALL_ROUNDS} 轮无进展"
        loop.log(f"  ! {reason}，熔断")
        return finish(loop, "stalled", reason, EXIT_NEEDS_WORK)

    if rnd >= s["max_rounds"]:
        reason = f"跑满 {s['max_rounds']} 轮仍未达标"
        loop.log(f"  ! {reason}")
        return finish(loop, "exhausted", reason, EXIT_NEEDS_WORK)

    if s.get("diff_target") is not None:
        # 范围钉在历史提交上：改工作区也进不了送审 diff，下一轮 reviewer 只会看到
        # 一模一样的补丁。这种范围不存在"通过续轮收敛"这条路，必须就地关掉，
        # 否则调用方看到 can_continue=true 会一直跑下去，烧配额烧到熔断。
        reason = (f"范围钉死在 {s['diff_target'][:12]}，改动进不了送审 diff，"
                  f"无法通过续轮收敛 —— findings 请人工处理")
        loop.log(f"  ! {reason}")
        return finish(loop, "pinned_scope", reason, EXIT_NEEDS_WORK)

    # 还能继续：loop 保持打开，下次 rloop 自动接在同一个 loop 上，轮次递增。
    loop.update(status="open", outcome="needs_work",
                outcome_reason=f"第 {rnd} 轮未达标，等待处理")
    (loop.root / "report.md").write_text(render_report(loop), encoding="utf-8")
    resp = loop.round_path(rnd) / "response.md"
    loop.log("")
    loop.log(f"  处理完 findings 后，把逐条回应写到 {resp}")
    loop.log(f"  然后再跑一次 rloop，reviewer 会读它并逐条裁决")
    notify(loop, "📋 rloop 需要处理",
           f"{s['id']} 第 {rnd} 轮：{entry['blocking_findings']} 个阻塞项，"
           f"{len(review.get('findings') or [])} 条 findings")
    # 这条出口不走 finish()（loop 要留着开着给下一轮），但对面板来说这一轮同样
    # 结束了 —— 少了它，面板会一直挂在「运行中」等一个不会来的收尾事件。
    loop.progress.emit(
        "run.end", f"needs_work — 第 {rnd} 轮未达标，等待处理", "highlight",
        {"exit_code": EXIT_NEEDS_WORK, "outcome": "needs_work",
         "outcome_reason": f"第 {rnd} 轮未达标，等待处理",
         "response_path": str(resp)})
    return EXIT_NEEDS_WORK


# ─────────────────────────── 其余子命令 ───────────────────────────


def cmd_list(args) -> int:
    data = registry_read()
    if not data:
        print("没有记录在案的 loop。")
        return 0
    rows = []
    for lid, e in sorted(data.items(), reverse=True):
        root = Path(e["root"])
        st = "?"
        score = ""
        name = ""
        if (root / "loop.json").exists():
            try:
                s = json.loads((root / "loop.json").read_text(encoding="utf-8"))
                st = s.get("outcome") or s.get("status", "?")
                name = s.get("label") or ""
                h = s.get("history", [])
                if h:
                    score = (f"{h[-1]['deliverable_maturity']}/"
                             f"{h[-1]['production_readiness']} r{h[-1]['round']}")
            except Exception:
                pass
        # 名字优先于 registry 里那份「侧重或范围」：registry 只在创建时写一次，
        # 而名字要等第一轮 diff 出来才推得出来。
        tail = name or (e.get("label") or e.get("task") or "")
        rows.append((lid, st, score, Path(e["project"]).name, tail[:44]))
    w = max(len(r[0]) for r in rows)
    print(f"{'ID'.ljust(w)}  {'状态'.ljust(10)}  {'分数'.ljust(12)}  {'项目'.ljust(16)}  名字")
    for r in rows:
        print(f"{r[0].ljust(w)}  {r[1].ljust(10)}  {r[2].ljust(12)}  {r[3].ljust(16)}  {r[4]}")
    return 0


def cmd_status(args) -> int:
    loop = resolve_loop(args.id, Path(args.directory).resolve() if args.directory else None)
    s = loop.state
    print(f"loop     {s['id']}")
    print(f"项目     {s['project']}")
    print(f"范围     {s['scope_desc']}")
    if s.get("focus"):
        print(f"侧重     {s['focus']}")
    print("审阅     {}（{}）".format(
        s["reviewer"], "能跑测试，不能改代码" if s.get("verify", True) else "只读"))
    print(f"状态     {s.get('outcome') or s.get('status')}  {s.get('outcome_reason', '')}")
    print(f"轮次     {s.get('round', 0)} / {s['max_rounds']}   门槛 {s['min_score']}")
    print()
    print(render_score_history(s.get("history", [])))
    return 0


def cmd_logs(args) -> int:
    loop = resolve_loop(args.id, Path(args.directory).resolve() if args.directory else None)
    if not loop.log_file.exists():
        die("no log yet")
    if args.follow:
        os.execvp("tail", ["tail", "-f", str(loop.log_file)])
    print(loop.log_file.read_text(encoding="utf-8"), end="")
    return 0


def cmd_report(args) -> int:
    loop = resolve_loop(args.id, Path(args.directory).resolve() if args.directory else None)
    path = loop.root / "report.md"
    if not path.exists():
        print(render_report(loop))
    else:
        print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_replay(args) -> int:
    loop = resolve_loop(args.id, Path(args.directory).resolve() if args.directory else None)
    rd = loop.round_path(args.round)
    which = {"response": "response.md", "review": "review-prompt.md",
             "result": "review.json", "diff": "diff.patch"}[args.what]
    path = rd / which
    if not path.exists():
        die(f"missing: {path}")
    print(read_text_safe(path), end="")
    return 0


def pid_field(pid: int, fmt: str) -> str:
    """取 ps 的单个字段。多字段一次取回来解析不了 —— comm 里带路径、lstart 带
    空格，拼在一行没法可靠切分（试过，切出来是 'ana'、'env' 这种碎片）。"""
    try:
        r = subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def pid_started_exactly_at(pid: int, recorded: str | None) -> bool:
    """这个 pid 的启动时刻是否**正好**是当初记下的那个。

    上一版用的是下界（启动时间不早于本轮开始）。方向错了：pid 复用的真实场景
    是「旧进程没了，新进程占了同一个号」，而新进程启动得**更晚**，天然满足
    下界 —— 该挡的一个没挡住，挡住的全是不该来的。

    改成精确相等：同一个 pid 要被复用，必然对应另一个启动时刻。ps 的 lstart
    精确到秒，同秒内回收并复用同一个 pid 在实际系统上不会发生。

    没记到启动时刻（老 loop 留下的账本）就退回去只看命令行 —— 比误杀强。
    """
    if not recorded:
        # 旧账本没记启动时刻。此时唯一的依据只剩命令行子串，而那挡不住
        # "同类 CLI 复用了这个 pid" —— 宁可不发信号，也不能误杀别人的活。
        return False
    now = pid_field(pid, "lstart=")
    if not now:
        return False
    return " ".join(now.split()) == " ".join(recorded.split())


def pid_is_our_reviewer(pid: int, started: str | None = None) -> bool:
    """这个 pid 看起来还是我们起的那个 reviewer 吗。

    账本里的 pid 可能是陈旧的：rloop 被强杀时来不及清，而 pid 会被系统回收
    再分配给别人。直接照着它 killpg 会连累无关进程组 —— 停自己的 loop 把别人
    的活一起杀了，这是最不能接受的一类错误。
    """
    cmd = pid_field(pid, "command=")
    if not cmd or ("codex" not in cmd and "claude" not in cmd):
        return False
    return pid_started_exactly_at(pid, started)


def pid_is_our_runner(pid: int, started: str | None = None) -> bool:
    """还是那个在跑 loop 的 rloop 进程吗。同样是防 pid 复用后误杀。"""
    cmd = pid_field(pid, "command=")
    if "rloop" not in cmd:
        return False
    return pid_started_exactly_at(pid, started)


def cmd_stop(args, loop: "Loop | None" = None, collect: bool = False):
    """停掉正在跑的那一轮 —— 只发信号，不碰状态文件。

    `collect=True` 时不打印，返回 `(was_running, killed, msgs)` 给 `api stop` 组
    JSON。**故意不把这段逻辑抽成公共函数**：下面那个杀进程的顺序有个源码切片
    测试盯着，抽走它就没得盯了。

    早先这里还要负责把终态写成 aborted，于是 stop 和运行中的进程成了同一份
    loop.json 的两个写者：谁写、什么时候写、字条什么时候留，怎么排都有窗口。
    现在一个字节都不写，那一整类竞态就不存在了。

    被杀之后状态停在 `running`，下次裸调 rloop 会拿到锁、发现没人在跑、接管
    它并重跑那一轮 —— 这条路本来就为"进程崩了"准备着，被人停掉走同一条路即可。
    崩的还是停的，对下一步没有任何区别。
    """
    if loop is None:
        loop = resolve_loop(args.id,
                            Path(args.directory).resolve() if args.directory else None)
    msgs = []

    def say(m):
        msgs.append(m)
        if not collect:
            print(m)

    # 拿得到锁 = 没有任何进程在跑这个 loop。那么账本里的 child_pid 必然是陈旧的，
    # 而 pid 会被系统回收复用 —— 照着它开枪就是误杀别人的活。
    try:
        with loop_lock(loop.root):
            st = loop.state
            say(f"loop {st['id']} 当前没有进程在跑（status={st.get('status')}），"
                f"没什么可停的。")
            if st.get("status") == "running":
                say("它停在 running，说明上次没正常收尾；直接跑 rloop 会接管并重跑那一轮。")
            return (False, [], msgs) if collect else 0
    except SystemExit:
        pass    # 锁被占着，确实有进程在跑

    st = loop.state
    killed = []

    # 顺序要紧：**先收 runner，再收 reviewer**。
    # 反过来的话，reviewer 一退出，runner 的 p.wait() 立刻返回，它会一路走完
    # stream_subprocess → run_one_round → finish 把状态写成 done/failed —— 而
    # 这一切发生在 stop 还没轮到去杀它之前。runner 先死就不会再写任何状态，
    # 状态原样停在 running，正好走「进程崩了」那条接管路径。
    runner = st.get("runner_pid")
    if runner and runner != os.getpid() and pid_is_our_runner(runner, st.get("runner_started")):
        with contextlib.suppress(ProcessLookupError):
            os.kill(runner, signal.SIGTERM)
        killed.append(f"rloop 进程 {runner}")

    child = st.get("child_pid")
    if child and pid_is_our_reviewer(child, st.get("child_started")):
        # runner 死了，reviewer 就成了孤儿，必须显式收掉，连它派生的命令一起。
        # 它是独立进程组（start_new_session 起的），所以 killpg 一网打尽。
        with contextlib.suppress(ProcessLookupError):
            kill_pgid(os.getpgid(child), first=signal.SIGTERM)
        killed.append("reviewer 及其派生的命令")

    if not killed:
        say("loop 正在运行，但没找到可发信号的进程（可能正好在收尾）。")
        return (True, [], msgs) if collect else 0
    say("已收掉：" + "、".join(killed))
    say("状态停在 running；下次跑 rloop 会接管并重跑这一轮。")
    return (True, killed, msgs) if collect else 0




# ═══════════════════════════ 呈现 ═══════════════════════════
#
# 数据层 + 渲染层。这里不碰终端也不碰 HTTP —— 界面在 gui/ 下面，是可选的。
# 但这一节本身是核心：跑完往终端打的完整结果、每轮落盘的 review.md，都走它。

# ─────────────────────────── 宽字符 ───────────────────────────


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def clip(s: str, width: int) -> str:
    """裁到不超过 width 列。放不下时用 … 收尾。"""
    if disp_width(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(s: str, width: int) -> str:
    s = clip(s, width)
    return s + " " * max(0, width - disp_width(s))


# ─────────────────────────── 数据层 ───────────────────────────


def collect_loops() -> list[dict]:
    """所有还找得到的 loop，最近的排前面。

    以注册表为索引，但状态一律从 loop.json 现读 —— 注册表只在创建时写一次，
    拿它当状态源会显示成永远的初始值。
    """
    out = []
    for lid, entry in registry_read().items():
        root = Path(entry.get("root", ""))
        try:
            st = json.loads(read_text_safe(root / "loop.json"))
        except (OSError, json.JSONDecodeError):
            continue
        st.setdefault("id", lid)
        st.setdefault("project", entry.get("project", ""))
        out.append(loop_item(Loop(root), st))
    out.sort(key=lambda x: x["started_at"], reverse=True)
    return out


def loop_item(loop: Loop, state: dict | None = None, round_override: int | None = None) -> dict:
    """渲染层认的那个 item dict —— 全系统只此一处拼。

    以前 collect_loops、write_round_markdown、print_round_result 各手抄一份，
    字段名靠人眼对齐；漏一个键渲染层不会报错，只是那块内容默默不见。

    `state` 传进来是为了省一次读盘（调用方手上通常已经有了）；
    `round_override` 给 write_round_markdown 用 —— 它要渲染的是刚跑完的那一轮，
    未必等于 state 里已经写下的 round。
    """
    st = state if state is not None else loop.state
    hist = st.get("history") or []
    last = hist[-1] if hist else {}
    return {
        "id": st.get("id", ""),
        "root": loop.root,
        "project": Path(st.get("project", "")),
        "status": st.get("outcome") or st.get("status", "?"),
        "round": st.get("round", 0) if round_override is None else round_override,
        "max_rounds": st.get("max_rounds", 0),
        "scope": st.get("scope_desc", ""),
        "focus": st.get("focus"),
        "deliverable": last.get("deliverable_maturity"),
        "production": last.get("production_readiness"),
        "blocking": last.get("blocking_findings"),
        "started_at": st.get("started_at", ""),
        "state": st,
    }


def latest_review(item: dict) -> dict | None:
    rnd = item.get("round") or 0
    if rnd < 1:
        return None
    return load_review(Loop(item["root"]), rnd)


# ─────────────────────────── 渲染层 ───────────────────────────

# (样式, 文本)。样式名由界面层映射成颜色，纯文本输出时直接丢掉。
Line = tuple[str, str]

SEV_STYLE = {"critical": "err", "high": "err", "medium": "warn", "low": "dim"}
VERDICT_STYLE = {"pass": "ok", "needs_work": "warn"}
STATUS_STYLE = {
    "converged": "ok", "pass": "ok", "fixed": "ok",
    "needs_work": "warn", "partially_fixed": "warn", "open": "warn",
    "running": "accent",
    "stalled": "err", "exhausted": "err", "failed": "err",
    "inconsistent": "err", "not_fixed": "err", "aborted": "dim",
    "pinned_scope": "dim", "rebutted_and_accepted": "ok",
}


def score_line(item: dict) -> str:
    d, p, b = item.get("deliverable"), item.get("production"), item.get("blocking")
    if d is None:
        return "—"
    return f"{d}/{p}  阻塞 {b}"


def render_history(hist: list, min_score: float | None) -> list[Line]:
    if not hist:
        return [("dim", "  还没有分数")]
    has_usage = any(h.get("usage") for h in hist)
    head = "  轮次   交付物   生产就绪   阻塞项   判定"
    if has_usage:
        head += "        实付 token"
    out: list[Line] = [("dim", head)]
    prev = None
    for h in hist:
        d, p = h["deliverable_maturity"], h["production_readiness"]
        arrow = ""
        if prev is not None:
            dd, dp = d - prev[0], p - prev[1]
            if dd > 0.05 or dp > 0.05:
                arrow = "  ↑"
            elif dd < -0.05 or dp < -0.05:
                arrow = "  ↓"
            else:
                arrow = "  ="
        ok = min_score is not None and d >= min_score and p >= min_score
        style = "ok" if ok and h["blocking_findings"] == 0 else "normal"
        row = (f"   {h['round']:<5}  {d:<7}  {p:<9}  {h['blocking_findings']:<7}"
               f"  {h['verdict']}{arrow}")
        if has_usage:
            u = h.get("usage")
            # 报「实付」不报 input 总数：后者九成是缓存命中，直接看会以为贵十倍
            row = f"{row:<44}  {u['fresh']:>9,} 入 / {u['output']:>6,} 出" if u \
                else f"{row:<44}  {'—':>9}"
        out.append((style, row))
        prev = (d, p)
    if has_usage:
        tot_fresh = sum(h["usage"]["fresh"] for h in hist if h.get("usage"))
        tot_out = sum(h["usage"]["output"] for h in hist if h.get("usage"))
        tot_in = sum(h["usage"]["input"] for h in hist if h.get("usage"))
        cached = sum(h["usage"]["cached"] for h in hist if h.get("usage"))
        out.append(("dim",
                    f"   合计   实付 {tot_fresh:,} 入 / {tot_out:,} 出"
                    f"（输入总量 {tot_in:,}，其中 {cached * 100 // max(1, tot_in)}% 缓存命中）"))
    return out


def render_detail(item: dict, review: dict | None, width: int) -> list[Line]:
    """一个 loop 的完整视图。宽度用于给长文本折行。"""
    st = item["state"]
    out: list[Line] = []

    out.append(("title", f"{item['id']}"))
    out.append(("dim", f"  项目   {item['project']}"))
    out.append(("dim", f"  范围   {clip(item['scope'], max(20, width - 9))}"))
    if item.get("focus"):
        out.append(("dim", f"  侧重   {item['focus']}"))
    style = STATUS_STYLE.get(item["status"], "normal")
    out.append((style, f"  状态   {item['status']}    "
                       f"{st.get('outcome_reason') or ''}"))
    out.append(("dim", f"  轮次   {item['round']} / {item['max_rounds']}"
                       f"   门槛 {st.get('min_score')}"))
    out.append(("normal", ""))

    out.append(("title", "分数走势"))
    out += render_history(st.get("history") or [], st.get("min_score"))
    out.append(("normal", ""))

    if not review:
        out.append(("dim", "（这一轮还没有可用的 review 结果）"))
        return out

    if review.get("summary"):
        out.append(("title", "小结"))
        out += [("normal", "  " + ln) for ln in wrap(review["summary"], width - 4)]
        out.append(("normal", ""))

    prior = review.get("prior_findings_status") or []
    if prior:
        out.append(("title", f"对上一轮的裁决（{len(prior)} 条）"))
        for p in prior:
            s = STATUS_STYLE.get(p.get("status"), "normal")
            out.append((s, f"  [{p.get('status')}] {p.get('id')}  {p.get('description', '')}"))
            for ln in wrap(p.get("note", ""), width - 8):
                out.append(("dim", "      " + ln))
        out.append(("normal", ""))

    findings = review.get("findings") or []
    if findings:
        out.append(("title", f"本轮 findings（{len(findings)} 条）"))
        for f in findings:
            s = SEV_STYLE.get((f.get("severity") or "").lower(), "normal")
            loc = f"{f.get('file')}:{f.get('line')}"
            out.append((s, f"  [{(f.get('severity') or '?').upper()}] {f.get('id')} "
                           f"{loc}  ({f.get('category')})"))
            for ln in wrap(f.get("description", ""), width - 8):
                out.append(("normal", "      " + ln))
            for ln in wrap("→ " + (f.get("suggested_fix") or ""), width - 8):
                out.append(("dim", "      " + ln))
            out.append(("normal", ""))
    else:
        out.append(("ok", "本轮没有 findings"))
        out.append(("normal", ""))

    vals = review.get("validation_commands") or []
    if vals:
        out.append(("title", f"它实际跑了什么（{len(vals)} 条）"))
        for v in vals:
            s = {"pass": "ok", "fail": "err"}.get(v.get("outcome"), "dim")
            out.append((s, f"  [{v.get('outcome')}] {clip(v.get('command', ''), width - 12)}"))
        out.append(("normal", ""))

    nxt = review.get("next_priorities") or []
    if nxt:
        out.append(("title", "它建议先做什么"))
        for i, n in enumerate(nxt, 1):
            for j, ln in enumerate(wrap(n, width - 6)):
                out.append(("normal", f"  {i}. " + ln if j == 0 else "     " + ln))
        out.append(("normal", ""))

    pos = review.get("positive_evidence") or []
    if pos:
        out.append(("title", "正面证据"))
        for p in pos:
            for j, ln in enumerate(wrap(p, width - 6)):
                out.append(("dim", "  · " + ln if j == 0 else "    " + ln))
    return out


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行。中文没有空格可断，所以按字符累加而不是按词。"""
    if width < 8:
        width = 8
    lines, cur, used = [], [], 0
    for ch in str(text):
        if ch == "\n":
            lines.append("".join(cur))
            cur, used = [], 0
            continue
        w = char_width(ch)
        if used + w > width:
            lines.append("".join(cur))
            cur, used = [ch], w
        else:
            cur.append(ch)
            used += w
    if cur:
        lines.append("".join(cur))
    return lines or [""]


def render_markdown(item: dict, review: dict | None) -> str:
    """把一轮的结果渲染成 markdown。

    JSON 是给机器的 —— 门禁要靠字段交叉核对，格式必须硬。这份是给人的：
    打开 loop 目录，第一眼看到的应该是能读的东西。两者同源，不会各说各话。
    """
    st = item["state"]
    L: list[str] = []
    rnd = item.get("round") or 0
    L.append(f"# 第 {rnd} 轮 review — {item['id']}")
    L.append("")
    L.append(f"- **项目**：`{item['project']}`")
    L.append(f"- **范围**：{item['scope']}")
    if item.get("focus"):
        L.append(f"- **侧重**：{item['focus']}")
    L.append(f"- **状态**：{item['status']}"
             + (f" — {st.get('outcome_reason')}" if st.get("outcome_reason") else ""))
    L.append(f"- **轮次**：{rnd} / {item.get('max_rounds')}　**门槛**：{st.get('min_score')}")
    L.append("")

    hist = st.get("history") or []
    if hist:
        L.append("## 分数走势")
        L.append("")
        L.append("| 轮 | 交付物成熟度 | 生产就绪度 | 阻塞项 | 判定 |")
        L.append("|---:|---:|---:|---:|---|")
        for h in hist:
            L.append(f"| {h['round']} | {h['deliverable_maturity']} | "
                     f"{h['production_readiness']} | {h['blocking_findings']} | {h['verdict']} |")
        L.append("")

    if not review:
        L.append("_这一轮没有可用的 review 结果。_")
        return "\n".join(L) + "\n"

    if review.get("summary"):
        L += ["## 小结", "", review["summary"], ""]

    prior = review.get("prior_findings_status") or []
    if prior:
        L.append(f"## 对上一轮的裁决（{len(prior)}）")
        L.append("")
        for p in prior:
            L.append(f"### `{p.get('status')}` {p.get('id')} — {p.get('description', '')}")
            L.append("")
            L.append(p.get("note", ""))
            L.append("")

    findings = review.get("findings") or []
    L.append(f"## 本轮 findings（{len(findings)}）")
    L.append("")
    if not findings:
        L += ["_没有 findings。_", ""]
    for f in findings:
        L.append(f"### [{(f.get('severity') or '?').upper()}] {f.get('id')} "
                 f"`{f.get('file')}:{f.get('line')}` — {f.get('category')}")
        L.append("")
        L.append(f.get("description", ""))
        L.append("")
        L.append(f"**建议**：{f.get('suggested_fix', '')}")
        L.append("")

    vals = review.get("validation_commands") or []
    if vals:
        L += [f"## 它实际跑了什么（{len(vals)}）", "", "| 结果 | 命令 | 备注 |", "|---|---|---|"]
        for v in vals:
            cmd = (v.get("command") or "").replace("|", "\\|").replace("\n", " ")
            note = (v.get("note") or "").replace("|", "\\|")
            L.append(f"| {v.get('outcome')} | `{cmd}` | {note} |")
        L.append("")

    nxt = review.get("next_priorities") or []
    if nxt:
        L += ["## 它建议先做什么", ""]
        L += [f"{i}. {n}" for i, n in enumerate(nxt, 1)]
        L.append("")

    pos = review.get("positive_evidence") or []
    if pos:
        L += ["## 正面证据", ""]
        L += [f"- {e}" for e in pos]
        L.append("")

    L += ["---", "",
          f"_由 rloop 从 `round-{rnd:02d}/review.json` 渲染。_",
          f"_处理完这些 findings 后，把逐条交代写进 `round-{rnd:02d}/response.md`，_",
          "_然后再跑一次 rloop，reviewer 会读它并逐条裁决。_"]
    return "\n".join(L) + "\n"


def plain(lines: list[Line]) -> str:
    """给非交互场景用：丢掉样式，直接出文本。"""
    return "\n".join(text for _, text in lines)


# ─────────────────────────── api：给机器的出口 ───────────────────────────
#
# 三个出口互不混用，别把它们搅在一起：
#   rloop review [--json]           → 驱动循环的 skill（有上下文那一方）
#   rloop list/status/logs/...      → 人
#   rloop api <verb>                → 机器（GUI，以及任何语言写的第二个面板）
#
# 这一节里的东西是**对外契约**。字段可以加，删和改名要动 API_VERSION。

EXIT_API_NOT_FOUND = 4
EXIT_API_BAD_REQUEST = 5
EXIT_API_CONFLICT = 6
EXIT_API_SPAWN_FAILED = 7

# 产物清单。scope 决定它挂在 loop 上还是某一轮上。
ARTIFACTS = {
    "loop-log":     {"scope": "loop",  "file": "loop.log"},
    "report":       {"scope": "loop",  "file": "report.md", "fallback": "render"},
    "schema":       {"scope": "loop",  "file": "review-schema.json"},
    "diff":         {"scope": "round", "file": "diff.patch"},
    "prompt":       {"scope": "round", "file": "review-prompt.md"},
    "result":       {"scope": "round", "file": "review.json", "raw": True},
    "review-md":    {"scope": "round", "file": "review.md"},
    "response":     {"scope": "round", "file": "response.md"},
    "reviewer-log": {"scope": "round", "file": "reviewer.log"},
    "untracked":    {"scope": "round", "file": "untracked-manifest.json"},
    "progress":     {"scope": "round", "file": PROGRESS_FILE},
}

FILE_MAX_BYTES = 2 << 20


class ApiError(Exception):
    """api 分支里唯一的出错方式。

    **绝不在 api 分支调 `die()`** —— 它往 stderr 写一句中文就 SystemExit(1)，
    调用方拿到的是一个空 stdout 加一句没法解析的话。这里一律翻成结构化负载。
    """

    def __init__(self, code: str, message: str, exit_code: int,
                 hint: str = "", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.detail = detail or {}


def api_envelope(verb: str, data: dict, warnings: list | None = None) -> dict:
    return {"api": API_VERSION, "rloop_version": VERSION, "ok": True, "verb": verb,
            "generated_at": now_iso(), "warnings": warnings or [], "data": data}


def api_error_envelope(verb: str, e: ApiError) -> dict:
    return {"api": API_VERSION, "rloop_version": VERSION, "ok": False, "verb": verb,
            "generated_at": now_iso(), "warnings": [],
            "error": {"code": e.code, "message": e.message,
                      "hint": e.hint, "detail": e.detail}}


def loop_summary(loop: Loop, state: dict | None = None,
                 active_id: str | None = None) -> dict:
    """一个 loop 的对外投影：全是标量和字符串，**不含 loop.json 全量**。

    不把内部状态整个吐出去，是为了让 loop.json 的结构还能随便改。面板要看原文
    走 `api file`。

    颜色也在这里算好（`status_class` / `verdict_class`）——以前面板各抄一份
    STATUS_STYLE，抄漏一个键的失效模式是静默变灰，没人会注意到。
    """
    st = state if state is not None else loop.state
    hist = st.get("history") or []
    last = hist[-1] if hist else {}
    lifecycle = st.get("status", "?")
    status = st.get("outcome") or lifecycle
    rnd = st.get("round", 0)

    etag = ""
    with contextlib.suppress(OSError):
        s = loop.state_file.stat()
        # 不用 updated_at：now_iso() 只到秒，同秒两次 update 会让缓存假命中
        etag = f"{s.st_mtime_ns}:{s.st_size}"

    # 只对自称在跑的 loop 探活。pid_field 每次带 5 秒 ps 超时，列表页几十个 loop
    # 全探是真负担；更要紧的是绝不为了探测去碰 loop_lock —— 那会把只读探针变成
    # 写操作，还会把恰好在那一瞬启动的真 rloop die() 掉。
    running = False
    if lifecycle == "running":
        with contextlib.suppress(Exception):
            running = pid_is_our_runner(st.get("runner_pid") or -1, st.get("runner_started"))

    needs_response = False
    if lifecycle == "open" and status == "needs_work" and rnd >= 1:
        review = load_review(loop, rnd)
        if review and (review.get("findings") or []):
            resp = loop.round_path(rnd) / "response.md"
            needs_response = not (resp.exists() and resp.stat().st_size > 0)

    project = st.get("project", "")
    return {
        "id": st.get("id", loop.root.name),
        "etag": etag,
        "root": str(loop.root),
        "project": project,
        "project_name": Path(project).name if project else "",
        "lifecycle": lifecycle,
        "status": status,
        "status_class": STATUS_STYLE.get(status, "normal"),
        "outcome": st.get("outcome"),
        "outcome_reason": st.get("outcome_reason"),
        "round": rnd,
        "rounds_available": loop.rounds_available(),
        "max_rounds": st.get("max_rounds", 0),
        "min_score": st.get("min_score"),
        "scope": st.get("scope_desc", ""),
        # 给人认的名字。作者自己起的优先，其次从第一轮 diff 推的，都没有就空着 ——
        # **不在这儿现算**：查询路径要保持只读且便宜，--state 每 3 秒就要过一遍。
        "title": st.get("label") or "",
        "focus": st.get("focus"),
        "reviewer": st.get("reviewer"),
        "reviewer_model": st.get("reviewer_model"),
        "reviewer_effort": st.get("reviewer_effort"),
        # 面板得知道这一轮 reviewer 是哪一档，否则「codex」这三个字底下藏着
        # 「能跑测试」还是「只读」全看不出来。
        "verify": bool(st.get("verify", True)),
        # 这个 loop 到目前为止烧了多少。列表页正是「哪个贵」最该一眼看见的地方，
        # 否则得逐个点进去。拿不到用量时是 None 而不是 0 —— 「不知道」和「没花钱」
        # 是两回事。
        "usage_total": usage_total(hist),
        "deliverable": last.get("deliverable_maturity"),
        "production": last.get("production_readiness"),
        "blocking": last.get("blocking_findings"),
        "verdict": last.get("verdict"),
        "verdict_class": VERDICT_STYLE.get(last.get("verdict"), "normal"),
        "started_at": st.get("started_at", ""),
        "updated_at": st.get("updated_at", ""),
        "created_ns": st.get("created_ns"),
        # 与 emit_json 同名同义：能不能接着跑下一轮 / 改工作区进不进得了送审 diff
        "can_continue": lifecycle == "open",
        "fix_allowed": st.get("diff_target") is None,
        "is_active": active_id is not None and st.get("id") == active_id,
        "running": running,
        "run": st.get("run_id") if running else None,
        "needs_response": needs_response,
        "consistency_errors": st.get("consistency_errors") or [],
    }


def load_loop_state(root: Path) -> dict | None:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(read_text_safe(root / "loop.json"))
    return None


def api_meta() -> dict:
    """能力清单。**唯一不需要带 `--api` 的 verb** —— 它就是用来协商的。"""
    return {
        "api": API_VERSION,
        # envelope 顶层也有一份；这里再放一次，是为了让只留下 data 的调用方
        # （面板的 Contract 就是）不用回头去翻信封。
        "rloop_version": VERSION,
        "rloop_home": str(RLOOP_HOME),
        "loop_dirname": LOOP_DIRNAME,
        "methods": ["meta", "loops", "loop", "file", "events", "run", "stop"],
        "features": {
            "events_follow": True,
            "events_state": True,
            "run_detach": True,
            # reviewer 是 claude 时细粒度进度是真的全黑（只有 codex 吐 JSONL 事件
            # 流）。如实声明，面板显示「这个 reviewer 没有细粒度进度」，而不是留
            # 一片让人以为面板坏了的空白。run.start/phase/score/run.end 与 reviewer
            # 是谁无关，照发。
            "progress_for_reviewer": {"codex": True, "claude": False},
            # reviewer 默认带写权限跑（跑得动测试），`no_verify` 关回只读。
            # 面板据此决定要不要给这个开关、以及怎么标注每个 loop 的档位。
            "verify_default": True,
            "run_accepts_no_verify": True,
            # 作废那一轮的 scores/findings 会被清空，载荷里 voided 为真。
            "voided_rounds": True,
        },
        "review_exit_codes": {"pass": EXIT_PASS, "error": EXIT_ERROR,
                              "needs_work": EXIT_NEEDS_WORK,
                              "inconsistent": EXIT_INCONSISTENT},
        "api_exit_codes": {"ok": 0, "internal": 1, "not_found": EXIT_API_NOT_FOUND,
                           "bad_request": EXIT_API_BAD_REQUEST,
                           "conflict": EXIT_API_CONFLICT,
                           "spawn_failed": EXIT_API_SPAWN_FAILED},
        "classes": ["normal", "dim", "title", "ok", "warn", "err", "accent"],
        "status_class": dict(STATUS_STYLE),
        "severity_class": dict(SEV_STYLE),
        "verdict_class": dict(VERDICT_STYLE),
        "event_kinds": ["run.start", "phase", "cmd.start", "cmd.end", "agent.msg",
                        "agent.error", "agent.turn", "note", "score", "run.end",
                        "state", "heartbeat", "gap"],
        "event_levels": ["info", "note", "cmd", "warn", "err", "highlight"],
        "artifacts": {k: dict(v) for k, v in ARTIFACTS.items()},
        "defaults": {"min_score": DEFAULT_MIN_SCORE, "max_rounds": DEFAULT_MAX_ROUNDS,
                     "reviewer": DEFAULT_REVIEWER, "timeout": DEFAULT_TIMEOUT},
        "limits": {"file_max_bytes": FILE_MAX_BYTES,
                   "event_text_chars": EVENT_TEXT_CHARS,
                   "progress_max_bytes": PROGRESS_MAX_BYTES},
    }


def api_loops(project_filter: Path | None = None) -> tuple[dict, list]:
    warnings = []
    rows = []
    for lid, entry in registry_read().items():
        root = Path(entry.get("root", ""))
        st = load_loop_state(root)
        if st is None:
            warnings.append(f"registry 里的 {lid} 指向读不到的目录，已跳过")
            continue
        st.setdefault("id", lid)
        st.setdefault("project", entry.get("project", ""))
        if project_filter is not None and Path(st["project"]) != project_filter:
            continue
        rows.append((Loop(root), st))

    # is_active 按每个 loop 自己的 project 算，每组只问一次 find_active_loop
    actives = {}
    for _, st in rows:
        proj = st.get("project", "")
        if proj and proj not in actives:
            act = find_active_loop(Path(proj))
            actives[proj] = (act.state.get("id") if act else None) if act else None

    out = [loop_summary(lp, st, actives.get(st.get("project", ""))) for lp, st in rows]
    # created_ns 是纳秒级单调序号，秒级的 started_at 只是它缺席时的退路
    out.sort(key=lambda x: (x.get("created_ns") or 0, x.get("started_at") or ""),
             reverse=True)
    return {"loops": out, "any_running": any(x["running"] for x in out)}, warnings


def api_resolve(loop_id: str) -> tuple[Loop, dict]:
    """按 id 找 loop。找不到抛 ApiError，不 die()。"""
    entry = registry_read().get(loop_id)
    if entry:
        root = Path(entry.get("root", ""))
        st = load_loop_state(root)
        if st is not None:
            st.setdefault("id", loop_id)
            return Loop(root), st
    raise ApiError("not_found", f"找不到 loop：{loop_id}", EXIT_API_NOT_FOUND,
                   hint="用 `rloop api --api 1 loops` 看有哪些",
                   detail={"id": loop_id})


def usage_total(hist: list) -> dict | None:
    """整个 loop 的用量总账。没有任何一轮拿到用量就返回 None。

    报 `fresh`（实付）而不是 `input`：后者九成是缓存命中，照着它看会以为贵十倍。
    `rounds` 是**有用量数据的轮数**，可能少于总轮数 —— reviewer 是 claude 时那些
    轮次根本没有用量可拿，分母写成总轮数会让人以为每轮都便宜。
    """
    got = [h["usage"] for h in hist if h.get("usage")]
    if not got:
        return None
    total = {k: sum(u.get(k) or 0 for u in got)
             for k in ("input", "cached", "fresh", "output")}
    total["rounds"] = len(got)
    return total


def history_rows(hist: list, min_score: float | None) -> list:
    """历史分数摊平给面板。delta 阈值与 render_history 一致，别各算各的。"""
    out = []
    prev = None
    for h in hist:
        d, p = h.get("deliverable_maturity"), h.get("production_readiness")
        if prev is None:
            delta = "start"
        else:
            dd, dp = d - prev[0], p - prev[1]
            delta = "up" if (dd > 0.05 or dp > 0.05) else \
                    "down" if (dd < -0.05 or dp < -0.05) else "flat"
        meets = (min_score is not None and d >= min_score and p >= min_score
                 and h.get("blocking_findings") == 0)
        out.append({**h, "delta": delta, "meets_gate": bool(meets),
                    "row_class": "ok" if meets else "normal"})
        prev = (d, p)
    return out


def artifact_path(loop: Loop, what: str, rnd: int | None) -> Path:
    spec = ARTIFACTS[what]
    if spec["scope"] == "loop":
        return loop.root / spec["file"]
    # 一律走只拼不建的 round_path：光是浏览产物不该在 .review-loops/ 下留空目录
    return loop.round_path(rnd or 0) / spec["file"]


def api_loop(loop_id: str, rnd: int | None = None) -> tuple[dict, list]:
    loop, st = api_resolve(loop_id)
    avail = loop.rounds_available()
    cur = st.get("round", 0)
    if rnd is None:
        rnd = cur
    elif rnd not in avail:
        raise ApiError("not_found", f"loop {loop_id} 没有第 {rnd} 轮",
                       EXIT_API_NOT_FOUND,
                       hint="看 detail.rounds_available 里有哪些轮次",
                       detail={"id": loop_id, "round": rnd, "rounds_available": avail})

    review = load_review(loop, rnd) if rnd >= 1 else None
    findings = (review or {}).get("findings") or []
    prior = (review or {}).get("prior_findings_status") or []

    resp_path = loop.round_path(rnd) / "response.md" if rnd >= 1 else None
    resp_exists = bool(resp_path and resp_path.exists())

    arts = []
    for what, spec in ARTIFACTS.items():
        r = None if spec["scope"] == "loop" else rnd
        p = artifact_path(loop, what, r)
        item = {"what": what, "round": r, "path": str(p), "exists": p.exists(),
                "bytes": None, "lines": None}
        if item["exists"]:
            with contextlib.suppress(OSError):
                item["bytes"] = p.stat().st_size
        arts.append(item)

    data = {
        "loop": loop_summary(loop, st),
        "round": rnd,
        # review 必须是 load_review 的产物：它要截最外层 {...}（模型会带 markdown
        # fence 和废话）并校验必需键。面板重写这段容错就会出现「核心判它无效、
        # 面板显示得有模有样」的分叉。
        "review_valid": review is not None,
        "review": review,
        "findings_class": {f.get("id", ""): SEV_STYLE.get(f.get("severity"), "normal")
                           for f in findings if f.get("id")},
        "prior_class": {p.get("id", ""): STATUS_STYLE.get(p.get("status"), "normal")
                        for p in prior if p.get("id")},
        "history": history_rows(st.get("history") or [], st.get("min_score")),
        "response": {
            "exists": resp_exists,
            "chars": len(read_text_safe(resp_path)) if resp_exists else 0,
            "path": str(resp_path) if resp_path else None,
        },
        "artifacts": arts,
    }
    return data, []


def api_file(loop_id: str, what: str, rnd: int | None = None,
             tail: int | None = None, max_bytes: int = FILE_MAX_BYTES) -> tuple[dict, list]:
    if what not in ARTIFACTS:
        raise ApiError("bad_request", f"没有这种产物：{what}", EXIT_API_BAD_REQUEST,
                       hint="可选值见 `rloop api meta` 的 artifacts",
                       detail={"what": what, "choices": sorted(ARTIFACTS)})
    loop, st = api_resolve(loop_id)
    spec = ARTIFACTS[what]
    if spec["scope"] == "round" and rnd is None:
        rnd = st.get("round", 0)
    path = artifact_path(loop, what, rnd)
    warnings = []

    text, rendered = None, False
    if path.exists():
        text = read_text_safe(path)
    elif spec.get("fallback") == "render":
        # 跑到一半的 loop 还没有 report.md。直读文件的话报告页一片空白，
        # 而 cmd_report 本来就有现渲染的退路 —— 对齐它。
        text, rendered = render_report(loop), True
    else:
        raise ApiError("not_found", f"没有这份产物：{path}", EXIT_API_NOT_FOUND,
                       detail={"id": loop_id, "what": what, "round": rnd,
                               "path": str(path)})

    if tail:
        text = "\n".join(text.splitlines()[-tail:])
    raw = text.encode("utf-8")
    truncated = len(raw) > max_bytes
    if truncated:
        # 从尾部截：diff 和日志都是越往后越要紧
        text = raw[-max_bytes:].decode("utf-8", errors="replace")
        warnings.append(f"内容已截断到 {max_bytes} 字节，完整文件在 {path}")

    return {
        "what": what, "round": rnd if spec["scope"] == "round" else None,
        "name": spec["file"], "path": str(path),
        "exists": path.exists(), "rendered": rendered,
        "bytes": len(raw), "lines": text.count("\n") + 1 if text else 0,
        "truncated": truncated, "encoding": "text", "text": text,
    }, warnings


SPAWN_WAIT_SECONDS = 30.0     # 等新 loop 出现的上限
SPAWN_POLL_SECONDS = 0.02


SPAWN_LOG_KEEP_DAYS = 7


def sweep_spawn_logs() -> None:
    """清掉 `$RLOOP_HOME` 里过期的 spawn-*.err。

    新建 loop 时的 stderr 只能落在这儿（那会儿还没有 loop 目录），而失败的那些
    要留着给人看。于是它只增不减 —— 每次从面板新建一个 loop 就多一个文件。
    成功路径会当场删掉空的，这里再兜一次底，管那些失败留下的和中途放弃的。
    """
    cutoff = time.time() - SPAWN_LOG_KEEP_DAYS * 86400
    with contextlib.suppress(OSError):
        for f in RLOOP_HOME.glob("spawn-*.err"):
            with contextlib.suppress(OSError):
                if f.stat().st_mtime < cutoff:
                    f.unlink()


def registered_roots(project: Path) -> dict:
    """这个项目下所有 loop 的 {id: 目录}。扫目录，不建目录。"""
    out = {}
    d = project / LOOP_DIRNAME
    if not d.is_dir():
        return out
    with contextlib.suppress(OSError):
        for cand in d.iterdir():
            if (cand / "loop.json").exists():
                st = load_loop_state(cand)
                if st is not None:
                    out[st.get("id", cand.name)] = cand
    return out


def project_snapshot(project: Path) -> dict:
    """该项目下每个 loop 的 (created_ns, run_id)。用来认出「新起来的是哪一个」。"""
    out = {}
    d = project / LOOP_DIRNAME
    if not d.is_dir():
        return out
    with contextlib.suppress(OSError):
        for cand in d.iterdir():
            st = load_loop_state(cand)
            if st is not None:
                out[st.get("id", cand.name)] = (st.get("created_ns") or 0,
                                                st.get("run_id"))
    return out


def api_run(project: Path, opts: dict) -> tuple[dict, list]:
    """起一轮 review：核心自己 fork 一个 detached runner，确认起来了就返回。

    **调用方不持有句柄、不 pump、不 wait。** 进程管理整个留在这一侧 —— 以前
    两个面板各写一份 Popen + pump + kill_pgid，其中 TUI 那份还漏了
    `start_new_session`，停的时候 codex 孙进程会留下来接着跑。
    """
    if not project.is_dir():
        raise ApiError("bad_request", f"不是目录：{project}", EXIT_API_BAD_REQUEST,
                       detail={"project": str(project)})
    if not (project / ".git").exists():
        # 不做任何隐式 fallback。以前 web 在没选中 loop 时传 null，服务端悄悄
        # 退回自己进程的 cwd —— 用户完全看不见这个替换发生了。
        raise ApiError("bad_request", f"不是 git 仓库：{project}", EXIT_API_BAD_REQUEST,
                       hint="rloop 审的是 git 里的改动，先 git init",
                       detail={"project": str(project)})

    # 忙碌预检：只读地看一眼，把最常见的情况变成一句人话。**不碰任何锁文件** ——
    # 权威的并发保护仍然是子进程自己的 project_lock + loop_lock。
    active = find_active_loop(project)
    if active is not None:
        st = active.state
        alive = False
        if st.get("status") == "running":
            with contextlib.suppress(Exception):
                alive = pid_is_our_runner(st.get("runner_pid") or -1,
                                          st.get("runner_started"))
        if alive:
            raise ApiError("conflict", f"loop {st.get('id')} 正在跑，等它结束或先 stop",
                           EXIT_API_CONFLICT,
                           detail={"id": st.get("id"), "round": st.get("round")})

    before = project_snapshot(project)
    max_ns = max([v[0] for v in before.values()] or [0])

    argv = [sys.executable, str(Path(__file__).resolve()), "review",
            "-C", str(project), "--notify", "none"]
    for flag, key in (("--base", "base"), ("--commit", "commit"),
                      ("--label", "label"),
                      ("--reviewer", "reviewer"), ("--reviewer-model", "reviewer_model"),
                      ("--reviewer-effort", "reviewer_effort")):
        if opts.get(key):
            argv += [flag, str(opts[key])]
    for flag, key in (("-n", "max_rounds"), ("--min-score", "min_score")):
        if opts.get(key) is not None:
            argv += [flag, str(opts[key])]
    if opts.get("new"):
        argv.append("--new")
    # 只读档是安全开关，不能只有命令行够得着 —— 面板也是主入口之一。
    if opts.get("no_verify"):
        argv.append("--no-verify")
    if opts.get("focus"):
        argv.append(str(opts["focus"]))

    # stderr 绝不进 DEVNULL：`nothing to review` 和 `not a git repository` 都在
    # loop 创建之前 die()，只写 stderr。丢掉的话第一个配置错误的用户只会看到
    # 一句毫无信息量的「起不来」。
    if active is not None and not opts.get("new"):
        errfile = active.root / "spawn.err"      # 续跑：固定一个，自然不会堆积
    else:
        RLOOP_HOME.mkdir(parents=True, exist_ok=True)
        sweep_spawn_logs()
        errfile = RLOOP_HOME / f"spawn-{time.time_ns()}-{os.getpid()}.err"

    with errfile.open("w", encoding="utf-8") as ef:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(project), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=ef, start_new_session=True)
        except OSError as e:
            raise ApiError("spawn_failed", f"起不来：{e}", EXIT_API_SPAWN_FAILED,
                           detail={"argv": argv}) from e

    # 记下子进程的启动时刻。pid 会被系统回收复用，光比 pid 不够 —— 项目里其他
    # 探活（pid_is_our_runner / pid_started_exactly_at）一律配 lstart 一起比，
    # 这里也照做。
    child_started = pid_field(proc.pid, "lstart=")

    deadline = time.monotonic() + SPAWN_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(SPAWN_POLL_SECONDS)
        # 只认**我们自己 fork 出来的那个进程**写下的 loop。
        #
        # 光看「前后快照多了一个 loop」是不够的：两个并发的 api run 会拿到相同的
        # before 快照，其中一个 runner 抢到锁把 loop 建起来之后，两个父进程都会
        # 看到同一个新 run_id，于是都返回 started=true —— 而另一个的子进程随后
        # 因为锁冲突退出了。第二个调用方会以为自己的 focus / reviewer / --new
        # 生效了，实际被领到别人建的 loop 上。
        # runner 在 run_one_round 开头就把自己的 pid 和启动时刻写进 loop.json。
        #
        # **pid 和启动时刻都要比**：正常结束或中途死掉的历史 loop 会一直留着
        # 旧的 runner_pid，系统把那个号回收给我们这个新子进程之后，光比 pid
        # 就会命中那个历史 loop，返回一个根本不是本次启动的 started=true。
        for lid, root in registered_roots(project).items():
            st = load_loop_state(root)
            if not st or st.get("runner_pid") != proc.pid or not st.get("run_id"):
                continue
            if child_started and st.get("runner_started") != child_started:
                continue        # pid 撞上了，但不是同一个进程
            ns = st.get("created_ns") or 0
            # 起成功了，这份 stderr 就没人要了。留着只会在 ~/.rloop 里堆一地。
            # 有内容的留下 —— 那多半是 reviewer 之外的告警，值得有人看见。
            with contextlib.suppress(OSError):
                if errfile.stat().st_size == 0:
                    errfile.unlink()
            return {"started": True, "loop": lid, "round": st.get("round"),
                    "run": st.get("run_id"), "project": str(project),
                    "is_new": bool(ns > max_ns), "since": 0, "pending": False}, []

        rc = proc.poll()
        if rc is not None:
            err = read_text_safe(errfile)[-8192:]
            raise ApiError(
                "spawn_failed",
                f"rloop 起来之后立刻退出了（退出码 {rc}）", EXIT_API_SPAWN_FAILED,
                hint="detail.stderr 是它自己说的原话",
                detail={"exit_code": rc, "stderr": err, "stderr_path": str(errfile)})

    # 还活着但迟迟没建出 loop：不当失败处理，交给 state 通道去等
    return ({"started": True, "loop": None, "round": None, "run": None,
             "project": str(project), "is_new": None, "since": 0, "pending": True},
            [f"loop 还在创建中（等了 {SPAWN_WAIT_SECONDS:.0f} 秒）。"
             f"用 `api events --state --follow` 等它出现。"])


EVENT_POLL_SECONDS = 0.3      # tail 进度文件的间隔
STATE_POLL_SECONDS = 3.0      # 扫 loop.json etag 的间隔
LIVENESS_POLL_SECONDS = 2.0   # 探 runner 死活的间隔
HEARTBEAT_SECONDS = 15.0      # 多久没事件就发一次心跳


def summary_fingerprint(summary: dict) -> str:
    """判断一个 loop 的对外投影有没有变，用的指纹。

    **不能只用 `etag`**。etag 是 loop.json 的 mtime+size，可 summary 里有三个
    字段根本不来自那个文件：
      · `running`     —— 来自 pid 探活。而 `cmd_stop` 明确一个字节都不写状态，
                         所以 runner 被 stop 或 SIGKILL 之后 etag 纹丝不动，
                         订阅方会一直以为它还在跑，「审一轮」按钮一直是灰的。
      · `needs_response` —— 来自 round-NN/response.md 在不在。
      · `rounds_available` —— 来自扫目录。
    整个投影拿来比，才不会漏掉这几类变化。
    """
    return json.dumps(summary, sort_keys=True, ensure_ascii=False)


def emit_ndjson(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def synth_event(kind: str, text: str, level: str = "info", data: dict | None = None) -> dict:
    """follower 自己合成的事件。seq 为 null —— 它不在任何一轮的序列里。"""
    return {"api": API_VERSION, "seq": None, "ts": now_iso(),
            "loop": None, "round": None, "run": None,
            "kind": kind, "level": level, "text": text, "data": data or {}}


def read_progress_since(path: Path, since: int,
                        offset: int = 0) -> tuple[list, int, int]:
    """从 offset 起读新行，返回 (该发给下游的行, 新 offset, 见到的最大 seq)。

    真实事件**原样透传**，不重新序列化：省一次转换，也免得核心和 follower
    对同一条事件给出两种字节。

    缺口按顺序**插在它出现的位置上**，作为合成的 gap 行混在结果里 —— 不是
    攒到一批开头一起发，那样读者看不出洞在哪儿。缺口是逐条比对期望序号算的，
    不是只看第一条：坏行被跳过、写盘失败（`emit` 里 seq 已经加过才失败）、
    文件被外部截断，这几种都会在序列**中间**留下跳号，而下游拿到的会是一段
    看着连续的 1、3。
    """
    lines, top = [], since
    expected = since + 1
    if not path.exists():
        return lines, offset, top
    with contextlib.suppress(OSError):
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            # 必须用 readline 而不是 `for line in f`：文本模式下迭代器会禁用
            # tell()（抛 OSError: telling position disabled by next() call），
            # 而我们要靠 tell 记住读到哪儿了。
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    break               # 半行：写者还没写完，下一轮再读
                offset = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # 坏行跳过，**也不往下游传**：follower 是读者，它的 stdout
                    # 得是合法 NDJSON，不能把写者的半行原样转手给面板。
                    continue
                seq = obj.get("seq") if isinstance(obj, dict) else None
                if isinstance(seq, int):
                    if seq < expected:
                        continue        # 调用方已经见过，或者序号倒退了
                    if seq > expected:
                        lines.append(json.dumps(synth_event(
                            "gap", f"缺少 seq {expected}–{seq - 1} 的事件", "warn",
                            {"from": expected, "to": seq - 1, "reason": "missing"}),
                            ensure_ascii=False))
                    expected = seq + 1
                    top = max(top, seq)
                lines.append(line)
    return lines, offset, top


def progress_has_final(path: Path) -> bool:
    """这一轮的收尾事件是不是已经躺在文件里了。

    不能只看「这次读到的行里有没有 run.end」：调用方带着 `--since` 重连时会
    跳过全部历史，收尾事件明明在文件里、它也早就见过了，这次却一条都读不到。
    照那个判据兜底，就会给一个正常收尾的轮次反复合成假的「没有收尾记录」。
    """
    with contextlib.suppress(OSError):
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"run.end"' in line:
                    with contextlib.suppress(json.JSONDecodeError):
                        if json.loads(line).get("kind") == "run.end":
                            return True
    return False


def scan_states(project_filter: Path | None = None) -> dict:
    """registry 里每个 loop 的 (etag, summary)。给 --state 轮询用。"""
    out = {}
    for lid, entry in registry_read().items():
        root = Path(entry.get("root", ""))
        st = load_loop_state(root)
        if st is None:
            continue
        st.setdefault("id", lid)
        st.setdefault("project", entry.get("project", ""))
        if project_filter is not None and Path(st["project"]) != project_filter:
            continue
        out[lid] = (Loop(root), st)
    return out


def api_events(loop_id: str | None, rnd: int | None, since: int,
               want_state: bool, follow: bool, idle_timeout: float,
               project_filter: Path | None = None) -> int:
    """进度与状态的订阅出口。输出 NDJSON，不套 envelope。

    面板只需要**一个**长活 follower。换选中的 loop 或起新一轮 = 杀掉重起；
    `--since` 幂等，重起不会重复补。
    """
    loop = state = None
    prog = None
    if loop_id:
        loop, state = api_resolve(loop_id)
        if rnd is None:
            rnd = state.get("round", 0)
        prog = loop.round_path(rnd) / PROGRESS_FILE

    # --- 先把欠调用方的历史补齐 ---
    offset, top = 0, since
    lines: list = []
    if prog is not None:
        lines, offset, top = read_progress_since(prog, since)
        for l in lines:
            sys.stdout.write(l + "\n")
        sys.stdout.flush()

    seen_end = any(json.loads(l).get("kind") == "run.end"
                   for l in lines if l.startswith("{"))

    known: dict = {}
    if want_state:
        # 第一次扫描把全部 loop 都发一遍 —— 面板的初始列表也从这条流里来，
        # 不用额外调 api loops。
        for lid, (lp, st) in scan_states(project_filter).items():
            summary = loop_summary(lp, st)
            known[lid] = summary_fingerprint(summary)
            emit_ndjson(synth_event("state", f"{lid} {summary['status']}", "info",
                                    {"loop": summary}))

    if not follow:
        return 0

    # --- 长活订阅 ---
    # 两个计时器要分开：心跳本身也是「发出去的事件」，拿它重置空闲计时的话，
    # 任何大于心跳间隔的 --idle-timeout 都永远等不到。
    last_beat = last_real = time.monotonic()
    last_state_poll = last_live_poll = time.monotonic()
    while True:
        time.sleep(EVENT_POLL_SECONDS)
        now = time.monotonic()
        fired = False

        if prog is not None:
            lines, offset, top = read_progress_since(prog, top, offset)
            for l in lines:
                sys.stdout.write(l + "\n")
                fired = True
                with contextlib.suppress(json.JSONDecodeError):
                    if json.loads(l).get("kind") == "run.end":
                        seen_end = True
            if lines:
                sys.stdout.flush()

        if seen_end:
            prog = None     # 这一轮跟完了，别再 tail 它的文件
            if not want_state:
                return 0    # 有 --loop 无 --state：到此为止

        if want_state and now - last_state_poll >= STATE_POLL_SECONDS:
            last_state_poll = now
            for lid, (lp, st) in scan_states(project_filter).items():
                summary = loop_summary(lp, st)
                fp = summary_fingerprint(summary)
                if known.get(lid) != fp:
                    known[lid] = fp
                    emit_ndjson(synth_event(
                        "state", f"{lid} {summary['status']}", "info",
                        {"loop": summary}))
                    fired = True

        # 收尾兜底。没有它，follower 会一直挂着等一个不会来的 run.end，
        # 面板那边的连接跟着不释放。两种情况都要管：
        #   a) 账本还说 running，但那个进程已经不在了（被 SIGKILL / 崩了）
        #   b) 账本已经进终态，可磁盘上没有收尾事件（写盘失败、撞了上限、
        #      文件被外部截断）—— 只查 (a) 的话这一种会永远挂着
        if (loop is not None and not seen_end
                and now - last_live_poll >= LIVENESS_POLL_SECONDS):
            last_live_poll = now
            st = load_loop_state(loop.root) or {}
            status = st.get("status")
            round_over = False
            if status == "running":
                alive = False
                with contextlib.suppress(Exception):
                    alive = pid_is_our_runner(st.get("runner_pid") or -1,
                                              st.get("runner_started"))
                if not alive:
                    emit_ndjson(synth_event(
                        "run.end", "runner 已消失", "err",
                        {"exit_code": None, "outcome": "orphaned",
                         "outcome_reason": "runner 进程不在了，且没有写下收尾事件"}))
                    round_over = True
            elif rnd is not None and (st.get("round") or 0) >= rnd:
                # 账本终态 + 我们跟的这一轮确实已经开始过。要是 round 还没走到
                # 这一轮，那是它还没轮到，继续等。
                if not (prog is not None and progress_has_final(prog)):
                    emit_ndjson(synth_event(
                        "run.end", "这一轮已经结束，但事件文件里没有收尾记录", "warn",
                        {"exit_code": None, "outcome": st.get("outcome") or status,
                         "outcome_reason": "进度可能写盘失败或被截断，结果以账本为准"}))
                round_over = True

            if round_over:
                # 收尾走的是**和 seen_end 同一条**规则：停止 tail 这一轮，
                # 但只有不带 --state 时才真的退出。带 --state 的订阅是长活的，
                # 在这儿退出会让浏览器的 EventSource 立刻重连，变成
                # 「每 2 秒起一个 follower 子进程 + 全量扫一遍状态」的空转。
                seen_end = True
                prog = None
                if not want_state:
                    return 0

        if fired:
            last_beat = last_real = now
        elif now - last_beat >= HEARTBEAT_SECONDS:
            emit_ndjson(synth_event("heartbeat", "", "info", {"last_seq": top}))
            last_beat = now

        if idle_timeout and now - last_real >= idle_timeout:
            return 0


def cmd_web(args) -> int:
    """把网页面板拉起来。

    依赖方向是**核心以进程方式拉起面板**，不是 import —— 和 `cmd_logs -f` 走
    `os.execvp("tail", ...)` 是同一个手法。所以「面板不 import 核心」这条不受
    影响，反过来的 RLOOP_BIN 还顺手解决了「面板怎么找回核心」。
    """
    # 顺着**自己的真实位置**找同级的 rloopgui/，不靠 cwd 也不靠 sys.path：
    # 装好之后 `rloop` 通常是 ~/.local/bin 里指向别处的符号链接，而用户可能
    # 在任何目录下敲它。resolve() 会把符号链接解开，落到真正装着代码的地方。
    me = Path(__file__).resolve()
    home = me.parent
    if not (home / "rloopgui" / "__init__.py").exists():
        die(f"面板没装：{home / 'rloopgui'} 不存在。它应当和 rloop.py 放在一起。")

    argv = [sys.executable, "-m", "rloopgui", "web",
            "-C", str(Path(args.directory).resolve() if args.directory else Path.cwd())]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    # PYTHONPATH 把 rloopgui 的所在目录送给子解释器；RLOOP_BIN 让面板精确找回
    # **这一个**核心，不用猜 PATH 上是哪个。
    pypath = os.pathsep.join(x for x in (str(home), os.environ.get("PYTHONPATH", "")) if x)
    os.execve(sys.executable, argv,
              {**os.environ, "PYTHONPATH": pypath, "RLOOP_BIN": str(me)})
    return 1        # execve 成功就不会走到这儿


def cmd_api(args) -> int:
    """api 分发。stdout 上永远只有一个 JSON 对象，stderr 只在崩溃时有内容。"""
    verb = args.verb
    try:
        if verb != "meta" and args.api != API_VERSION:
            raise ApiError(
                "unsupported_api_version",
                f"这个 rloop 只提供 api {API_VERSION}，调用方要的是 {args.api}",
                EXIT_API_BAD_REQUEST,
                hint="先跑 `rloop api meta` 看核心提供哪个版本，两边一起升级")

        if verb == "meta":
            data, warnings = api_meta(), []
        elif verb == "loops":
            data, warnings = api_loops(
                Path(args.project).resolve() if args.project else None)
        elif verb == "loop":
            data, warnings = api_loop(args.id, args.round)
        elif verb == "file":
            data, warnings = api_file(args.id, args.what, args.round,
                                      args.tail, args.max_bytes)
        elif verb == "run":
            if not args.project:
                raise ApiError("bad_request", "run 必须给 --project", EXIT_API_BAD_REQUEST,
                               hint="切开之后没有共享的 cwd 可借，不做隐式 fallback")
            data, warnings = api_run(Path(args.project).resolve(), {
                "new": args.new, "focus": args.focus, "label": args.label,
                "base": args.base,
                "commit": args.commit, "max_rounds": args.max_rounds,
                "min_score": args.min_score, "reviewer": args.reviewer,
                "reviewer_model": args.reviewer_model,
                "reviewer_effort": args.reviewer_effort,
                "no_verify": args.no_verify,
            })
        elif verb == "stop":
            if not args.id:
                raise ApiError("bad_request", "stop 必须给 loop id", EXIT_API_BAD_REQUEST)
            loop, _ = api_resolve(args.id)
            was_running, killed, msgs = cmd_stop(args, loop, collect=True)
            data, warnings = {"was_running": was_running, "killed": killed,
                              "message": " ".join(msgs)}, []
        elif verb == "events":
            # 这个 verb 自己往 stdout 流 NDJSON，不套 envelope，也不回 data。
            return api_events(
                args.id, args.round, args.since or 0, args.state, args.follow,
                args.idle_timeout,
                Path(args.project).resolve() if args.project else None)
        else:
            raise ApiError("unsupported_method", f"没有这个 verb：{verb}",
                           EXIT_API_BAD_REQUEST, detail={"verb": verb})
    except ApiError as e:
        print(json.dumps(api_error_envelope(verb, e), ensure_ascii=False, indent=2))
        return e.exit_code

    if verb == "file" and getattr(args, "raw", False):
        # 大 diff 省一层 JSON 转义。截断信息走 stderr，退出码仍是 0。
        sys.stdout.write(data["text"])
        for w in warnings:
            print(w, file=sys.stderr)
        return 0

    print(json.dumps(api_envelope(verb, data, warnings), ensure_ascii=False, indent=2))
    return 0


# ─────────────────────────── CLI ───────────────────────────


def write_round_markdown(loop: Loop, rnd: int, review: dict) -> None:
    """把这一轮渲染成 round-NN/review.md。

    JSON 那份是给门禁和渲染用的，格式必须硬；这份是给人的 —— 打开 loop 目录，
    第一眼能读的应该是它。同源渲染，不会和 JSON 各说各话。
    """
    item = loop_item(loop, round_override=rnd)
    with contextlib.suppress(OSError):
        (loop.round_dir(rnd) / "review.md").write_text(
            render_markdown(item, review), encoding="utf-8")


def print_round_result(loop: Loop, stream=None) -> None:
    """把这一轮的完整结果打到终端。渲染逻辑和面板共用一份。

    `--json` 模式下走 stderr，**不是不打**。早先这里是 `if not args.json` —— 而
    驱动循环的会话恰恰用的就是 `--json`，于是最需要看结果的那条路径上，人在终端
    上只能看到「6.5 / 4.0 / 2 个阻塞项」，reviewer 到底报了哪三条一个字都没有。
    stdout 仍然只有那份 JSON，契约没变；日志早就是这么分流的。
    """
    item = loop_item(loop)
    width = shutil.get_terminal_size((100, 24)).columns
    out = stream or sys.stdout
    print(file=out)
    print(plain(render_detail(
        item, load_review(loop, item["round"]), min(110, max(60, width)))), file=out)


def subcommands() -> set:
    """CLI 上真正挂着的子命令名。

    **不要写成手抄的常量。** 这个集合决定 `normalize_argv` 把第一个词当子命令
    还是当 focus 文本：漏掉一个名字，`rloop api loops` 就会被改写成
    `review api loops`，起一轮真 review 烧掉两个模型的配额。手抄两份迟早会漂，
    所以直接从解析器上现问。
    """
    return registered_subcommands()

TOP_LEVEL_FLAGS = ("-h", "--help", "--version")


def normalize_argv(argv: list, known: set | None = None) -> list:
    """裸 `rloop` 以及 `rloop 一些侧重点` 都当成 review。

    只有第一个词正好是子命令时才走它，这样 `rloop 重点看并发` 不会被误解析成子命令。
    `known` 由调用方把已经建好的解析器传进来，省一次重复构建。
    """
    if not argv:
        return ["review"]
    known = subcommands() if known is None else known
    if argv[0].startswith("-"):
        return argv if argv[0] in TOP_LEVEL_FLAGS else ["review"] + argv
    return argv if argv[0] in known else ["review"] + argv


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器。

    抽成函数是为了让 `subcommands()` 能从它身上现问有哪些子命令 —— 那个集合
    决定 `normalize_argv` 把第一个词当命令还是当 focus 文本，抄错一个名字就会
    让 `rloop api loops` 变成一轮真 review。
    """
    p = argparse.ArgumentParser(
        prog="rloop",
        description="用另一个模型独立审当前工作区的改动，跑一轮就返回。"
                    "不带参数直接跑 `rloop` 即可；循环由调用它的会话驱动。",
    )
    p.add_argument("--version", action="version", version=f"rloop {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("review", help="审当前改动（默认命令，可省略）")
    s.add_argument("focus", nargs="*",
                   help="可选：这次想让 reviewer 侧重看什么。不给就让它自己从 diff 推断意图")
    s.add_argument("-C", "--directory", help="项目目录（默认当前目录）")
    s.add_argument("--label", help="给这个 loop 起个名字，列表里好认；不给就从改动里推一个")
    s.add_argument("--no-verify", action="store_true", dest="no_verify",
                   help="把 reviewer 关回只读：它跑不了测试，也就拿不到实证。"
                        "审来路不明的代码时用")
    s.add_argument("--base", help="审相对该分支/提交的全部改动（含未提交）")
    s.add_argument("--commit", help="审某个 commit 引入的改动")
    s.add_argument("--reviewer", choices=["claude", "codex"], default=None,
                   help=f"默认 {DEFAULT_REVIEWER}")
    s.add_argument("--reviewer-model", default=None, help="覆盖 reviewer 模型")
    s.add_argument("--reviewer-effort", default=None,
                   help=f"reviewer 推理档位，默认 {DEFAULT_EFFORT}（省配额）")
    s.add_argument("--effort", default=None, help="--reviewer-effort 的简写")
    s.add_argument("--json", action="store_true",
                   help="把这一轮的结构化结果打到 stdout（给驱动循环的会话用）")
    s.add_argument("--new", action="store_true",
                   help="强制开一个新 loop，不续当前项目里开着的那个")
    s.add_argument("-n", "--max-rounds", type=int, default=None,
                   help=f"默认 {DEFAULT_MAX_ROUNDS}")
    s.add_argument("-m", "--min-score", type=float, default=None,
                   help=f"默认 {DEFAULT_MIN_SCORE}")
    s.add_argument("-t", "--timeout", type=int, default=None,
                   help=f"单个 agent 单轮超时秒数，默认 {DEFAULT_TIMEOUT}")
    s.add_argument("--notify", choices=["macos", "cmd", "none"], default=None,
                   help="默认 macos")
    s.add_argument("--notify-cmd", help="--notify cmd 时执行的命令，环境变量 RLOOP_TITLE/RLOOP_BODY/RLOOP_ROOT")
    s.set_defaults(func=cmd_review)

    for name, fn, helptxt in [
        ("list", cmd_list, "列出所有 loop"),
        ("status", cmd_status, "查看某个 loop 的状态和分数走势"),
        ("logs", cmd_logs, "查看日志"),
        ("report", cmd_report, "输出最终报告"),
        ("stop", cmd_stop, "停止一个 loop"),
    ]:
        q = sub.add_parser(name, help=helptxt)
        if name != "list":
            q.add_argument("id", nargs="?", help="loop id（默认取当前项目最近的一个）")
            q.add_argument("-C", "--directory")
        if name == "logs":
            q.add_argument("-f", "--follow", action="store_true")
        q.set_defaults(func=fn)

    q = sub.add_parser("replay", help="回看某一轮实际喂给 agent 的输入")
    q.add_argument("round", type=int)
    q.add_argument("what", nargs="?", default="review",
                   choices=["review", "result", "diff", "response"])
    q.add_argument("--id")
    q.add_argument("-C", "--directory")
    q.set_defaults(func=cmd_replay, id=None)

    # api：给机器的出口。人不用敲这些。
    a = sub.add_parser("api", help="结构化输出，给 GUI 和别的程序用")
    a.add_argument("verb", choices=["meta", "loops", "loop", "file", "events",
                                    "run", "stop"])
    a.add_argument("id", nargs="?", help="loop id（loop/file/stop 必填）")
    a.add_argument("--api", type=int, default=None,
                   help=f"调用方要的契约版本（当前 {API_VERSION}）。meta 之外都必须给")
    a.add_argument("--project", help="只看这个项目下的 loop")
    a.add_argument("--round", type=int, help="第几轮，缺省取当前轮")
    a.add_argument("--what", help="产物名，见 api meta 的 artifacts")
    a.add_argument("--tail", type=int, help="只取最后 N 行")
    a.add_argument("--max-bytes", type=int, default=FILE_MAX_BYTES, dest="max_bytes")
    a.add_argument("--raw", action="store_true", help="file：不包 envelope，直出字节")
    a.add_argument("--since", type=int, default=0, help="events：只要 seq 大于它的事件")
    a.add_argument("--state", action="store_true", help="events：把各 loop 的状态变化也发过来")
    a.add_argument("--follow", action="store_true", help="events：吐完不退出，接着跟")
    a.add_argument("--idle-timeout", type=float, default=0.0, dest="idle_timeout",
                   help="events：多少秒没有真事件就自己退出，0 表示永不")
    # run 的参数：面板上那张「审一轮」表单就是照着这些做的
    a.add_argument("--new", action="store_true", help="run：另起一个 loop，不续")
    a.add_argument("--focus", help="run：这一轮想让 reviewer 侧重看什么")
    a.add_argument("--label", help="run：给这个 loop 起个名字")
    a.add_argument("--base", help="run：审相对该分支/提交的全部改动")
    a.add_argument("--commit", help="run：只审这一个提交")
    a.add_argument("-n", "--max-rounds", type=int, dest="max_rounds", help="run：轮数上限")
    a.add_argument("--min-score", type=float, dest="min_score", help="run：达标门槛")
    a.add_argument("--reviewer", choices=["codex", "claude"], help="run：谁来审")
    a.add_argument("--reviewer-model", dest="reviewer_model", help="run：指定模型")
    a.add_argument("--reviewer-effort", dest="reviewer_effort", help="run：推理档位")
    a.add_argument("--no-verify", action="store_true", dest="no_verify",
                   help="run：把 reviewer 关回只读（审来路不明的代码时用）")
    # stop 借用 review 的 -C，好让 cmd_stop 原样工作
    a.add_argument("-C", "--directory", help="stop：项目目录")
    a.set_defaults(func=cmd_api)

    w = sub.add_parser("web", help="打开网页面板")
    w.add_argument("-C", "--directory", help="项目目录（默认当前目录）")
    w.add_argument("--port", type=int, help="指定端口，默认随机")
    w.add_argument("--no-open", action="store_true", dest="no_open",
                   help="不要自动开浏览器")
    w.set_defaults(func=cmd_web)

    return p


def registered_subcommands(parser: argparse.ArgumentParser | None = None) -> set:
    """解析器上实际挂着的子命令名。"""
    parser = parser or build_parser()
    for act in parser._subparsers._group_actions if parser._subparsers else []:
        if isinstance(act, argparse._SubParsersAction):
            return set(act.choices)
    return set()


def main() -> int:
    p = build_parser()
    args = p.parse_args(normalize_argv(sys.argv[1:], registered_subcommands(p)))
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
