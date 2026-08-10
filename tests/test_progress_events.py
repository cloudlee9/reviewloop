#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度事件层：解析、渲染、落盘。

这一层的存在理由：`render_codex_event` 的结果以前只 print 给自己的 stdout，
一个字节都不落盘 —— 于是谁不拥有那个 reviewer 进程，谁就看不见进度。两个面板
因此都被迫自己 Popen 起 rloop。落盘之后进度成了可回放、可多消费者的东西。

**最重要的是第一条 golden 用例**：终端上那几行进度是用户天天看的，拆成
parse+format 两半之后必须一个字节都不变。参照实现直接抄在下面，不 import。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402


# ─────────── 拆分前的原样实现，只作对照，不许改 ───────────

def render_codex_event_before_the_split(line: str) -> str | None:
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
        return f"    $ {cmd[:110]}"
    if kind == "item.completed":
        if itype == "command_execution":
            rc = item.get("exit_code")
            return None if rc == 0 else f"      ↳ exit {rc}"
        if itype == "agent_message":
            text = " ".join((item.get("text") or "").split())
            if text.startswith("{"):
                return None
            return f"    · {text[:120]}"
        if itype == "error":
            return f"    ! {' '.join((item.get('message') or '').split())[:120]}"
    if kind == "turn.completed":
        u = ev.get("usage") or {}
        return f"    · 本轮完成（输出 {u.get('output_tokens', '?')} tokens）"
    return None


CODEX_LINES = [
    '{"type":"item.started","item":{"type":"command_execution","command":"pytest  -q\\n"}}',
    '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":0}}',
    '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":2}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"我在读 rloop.py"}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"scores\\": 1}"}}',
    '{"type":"item.completed","item":{"type":"error","message":"沙箱拒绝了写操作"}}',
    '{"type":"turn.completed","usage":{"output_tokens":4210}}',
    '{"type":"turn.completed"}',
    '{"type":"item.started","item":{"type":"command_execution","command":"x" * 300}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":"' + "长" * 300 + '"}}',
    'not json at all',
    '',
    '{"broken json',
    '{"type":"unknown.kind"}',
]


def test_the_terminal_output_is_byte_identical_after_the_split():
    """拆成 parse+format 之后，终端上那一行必须一模一样。"""
    for line in CODEX_LINES:
        assert rloop.render_codex_event(line) == render_codex_event_before_the_split(line), \
            f"这行的渲染变了：{line[:70]}"


def test_byte_identical_on_every_real_reviewer_log():
    """拿仓库里所有真跑出来的 reviewer.log 再验一遍。

    构造的用例覆盖不到真实数据的形状；这个仓库自己审自己攒下了几万行真日志，
    不用白不用。日志不在（干净 clone）就跳过。
    """
    logs = sorted((REPO_ROOT / ".review-loops").glob("*/round-*/reviewer.log"))
    if not logs:
        pytest.skip("没有历史 reviewer.log 可比对")
    total = 0
    for lg in logs:
        for line in lg.read_text(encoding="utf-8", errors="replace").splitlines():
            total += 1
            assert rloop.render_codex_event(line) == render_codex_event_before_the_split(line), \
                f"{lg}: {line[:70]}"
    assert total > 100, f"只比了 {total} 行，样本太少说明不了什么"


def test_parse_gives_semantics_not_symbols():
    """解析结果里带的是 kind/level/data，不是终端符号。

    面板从此按 kind 判断语义。以前 TUI 和 web 各存一份「匹配 $ / ↳ / ! / ·」的表，
    核心改一个符号两边同时静默变灰。
    """
    ev = rloop.parse_codex_event(CODEX_LINES[0])
    assert ev["kind"] == "cmd.start" and ev["level"] == "cmd"
    assert "$" not in ev["text"], "text 里不该有终端前缀符号"
    assert ev["data"]["command"] == "pytest -q"

    fail = rloop.parse_codex_event(CODEX_LINES[2])
    assert fail["kind"] == "cmd.end" and fail["data"]["exit_code"] == 2

    assert rloop.parse_codex_event(CODEX_LINES[1]) is None, "成功的命令不该刷屏"
    assert rloop.parse_codex_event(CODEX_LINES[4]) is None, "结构化结果不是说给人听的"

    turn = rloop.parse_codex_event(CODEX_LINES[6])
    assert turn["kind"] == "agent.turn" and turn["data"]["output_tokens"] == 4210


def test_every_prefix_belongs_to_a_kind_that_can_be_produced():
    """EVENT_PREFIX 的键必须都是 parse 真能产出的 kind。

    留一个产不出来的键 = 死代码；反过来漏一个 kind，那类事件就在终端上消失。
    """
    producible = {"cmd.start", "cmd.end", "agent.msg", "agent.error", "agent.turn"}
    assert set(rloop.EVENT_PREFIX) == producible


# ─────────── 落盘 ───────────

@pytest.fixture
def writer(tmp_path):
    (tmp_path / "round-01").mkdir()
    return rloop.ProgressWriter(tmp_path, 1, "test-loop", "run-1")


def read_events(tmp_path) -> list[dict]:
    """读回事件。跳过解析不了的行 —— 契约要求读者这么做，而且撑文件到上限的
    那几条用例本来就往里塞了非 JSON 的填充。"""
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_events_are_appended_as_ndjson(writer, tmp_path):
    writer.emit("run.start", "开始", "info", {"pid": 1})
    writer.emit("score", "交付物=7.1", "highlight", {"deliverable_maturity": 7.1})

    evs = read_events(tmp_path)
    assert [e["seq"] for e in evs] == [1, 2]
    assert [e["kind"] for e in evs] == ["run.start", "score"]
    assert all(e["api"] == rloop.API_VERSION for e in evs)
    assert all(e["loop"] == "test-loop" and e["run"] == "run-1" for e in evs)
    assert evs[1]["data"]["deliverable_maturity"] == 7.1


def test_a_new_writer_resumes_the_sequence(writer, tmp_path):
    """同一轮被接管重跑时 seq 接着往下走。

    从 1 重来会让读者的 `--since` 裁剪把新事件当成看过的丢掉。
    """
    writer.emit("note", "第一次运行")
    writer.emit("note", "第一次运行")
    again = rloop.ProgressWriter(tmp_path, 1, "test-loop", "run-2")
    assert again.seq == 2
    again.emit("note", "接管重跑")
    assert [e["seq"] for e in read_events(tmp_path)] == [1, 2, 3]


def test_an_oversized_event_is_cut_down_not_dropped(writer, tmp_path):
    """单行必须 < 3500 字节。

    O_APPEND 的单次 write() 只在 < PIPE_BUF(4096) 时原子，超了并发读者会读到半行。
    """
    writer.emit("note", "长" * 900, "info", {"payload": "x" * 9000})
    ev = read_events(tmp_path)[-1]
    assert ev["data"] == {"oversized": True}
    assert len(ev["text"]) == 200
    assert len(json.dumps(ev, ensure_ascii=False).encode()) < rloop.EVENT_LINE_BYTES


def test_text_is_capped_even_when_the_line_fits(writer, tmp_path):
    writer.emit("note", "字" * 800)
    assert len(read_events(tmp_path)[-1]["text"]) == rloop.EVENT_TEXT_CHARS


def test_it_stops_appending_past_the_size_cap(writer, tmp_path):
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    p.write_text("x" * (rloop.PROGRESS_MAX_BYTES + 1), encoding="utf-8")
    writer.emit("note", "这条应该被挡下")

    tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
    assert "超上限" in tail
    assert writer.stopped
    writer.emit("note", "这条更不该写")
    assert p.read_text(encoding="utf-8", errors="replace").splitlines()[-1] == tail


def test_a_missing_round_dir_silently_drops_the_event(tmp_path):
    """轮次目录还没建就丢掉，绝不代为创建。

    读路径凭空造目录正是 round_path 要解决的问题，落盘器不能反过来再造一次。
    """
    w = rloop.ProgressWriter(tmp_path, 7, "test-loop", "run-1")
    w.emit("note", "没地方落")
    assert not (tmp_path / "round-07").exists()


def test_write_failures_never_reach_the_caller(writer, monkeypatch):
    """磁盘满了也不能把一轮 review 带崩 —— 进度是附属品。"""
    def boom(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(rloop.ProgressWriter, "_append_raw", boom)
    writer.emit("note", "写不进去")     # 不抛就算过


def test_null_progress_swallows_everything():
    """查询类命令拿到的是黑洞，不会往轮次目录里写东西。"""
    n = rloop.NullProgress()
    n.emit("note", "x")
    n.emit("run.end", "y", "highlight", {"a": 1})


def test_a_corrupt_line_does_not_break_seq_recovery(tmp_path):
    """半行、乱码都得能跳过 —— 契约明确要求读者跳过解析失败的行。"""
    (tmp_path / "round-01").mkdir()
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    p.write_text('{"seq":1,"kind":"note"}\n{"seq":2, 半行\nnot json\n'
                 '{"seq":5,"kind":"note"}\n', encoding="utf-8")
    assert rloop.ProgressWriter(tmp_path, 1).seq == 5


def test_the_cap_warning_gets_its_own_sequence_number(writer, tmp_path):
    """撞上限的告警必须占一个**新** seq。

    回归用例：告警在 `self.seq += 1` 之前打包，复用了上一条事件的序号 ——
    按 seq 去重的读者（`--since` 就是）会把它整个丢掉，于是日志静默截断，
    没有任何人知道后面还有内容没落盘。
    """
    writer.emit("note", "第一条")
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    with p.open("a", encoding="utf-8") as f:
        f.write("x" * rloop.PROGRESS_MAX_BYTES + "\n")

    writer.emit("note", "这条会撞上限")
    evs = read_events(tmp_path)
    warn = [e for e in evs if "超上限" in (e["text"] or "")]
    assert len(warn) == 1, "没发出容量告警"
    seqs = [e["seq"] for e in evs]
    assert len(seqs) == len(set(seqs)), f"序号重复了：{seqs}"
    assert warn[0]["seq"] > 1, "告警复用了上一条的序号"


def test_the_final_event_still_lands_after_the_cap(writer, tmp_path):
    """停止追加之后，run.end 仍然必须写得进去。

    回归用例：撞上限就把 writer 永久置为 stopped，连收尾事件也丢掉 ——
    于是 follower 一直等一个不会来的 run.end，面板上那一轮永远在转圈。
    """
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    with p.open("a", encoding="utf-8") as f:
        f.write("x" * rloop.PROGRESS_MAX_BYTES + "\n")

    writer.emit("note", "普通事件，应该被挡下")
    writer.emit("run.end", "收工", "highlight", {"outcome": "needs_work"})

    evs = read_events(tmp_path)
    kinds = [e["kind"] for e in evs]
    assert kinds[-1] == "run.end", f"收尾事件没写进去：{kinds}"
    assert evs[-1]["data"]["outcome"] == "needs_work"
    assert "普通事件" not in "".join(e["text"] or "" for e in evs), \
        "撞上限之后普通事件还在写"
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_ordinary_events_stay_blocked_after_the_final_one(writer, tmp_path):
    """run.end 是特例，不是把闸门整个打开。"""
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    with p.open("a", encoding="utf-8") as f:
        f.write("x" * rloop.PROGRESS_MAX_BYTES + "\n")
    writer.emit("run.end", "收工")
    before = len(read_events(tmp_path))
    writer.emit("note", "还想写")
    assert len(read_events(tmp_path)) == before
