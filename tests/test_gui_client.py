#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板侧的 CLI 客户端。

这些用例全部**不碰真的 rloop** —— 假 rloop 是一个几十行的脚本。这本身就是
论点：如果客户端只需要一份 JSON 契约就能测通，那它确实只依赖契约，换成
Go 或 TypeScript 重写也是同一份工作量。

反过来说，凡是在这里测不了、非得起真 rloop 才能验的行为，都说明有条依赖
漏出了契约。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rloopgui.client import Client, find_core          # noqa: E402
from rloopgui.contract import API, Contract            # noqa: E402
from rloopgui.errors import (CoreFailed, CoreNotFound,  # noqa: E402
                             CoreUnintelligible)

FAKE = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
script = json.loads(os.environ.get("FAKE_SCRIPT", "{}"))
verb = argv[1] if argv and argv[0] == "api" else ""
if verb == "--api":
    verb = argv[3] if len(argv) > 3 else ""
for a in argv:
    if a in ("meta", "loops", "loop", "file", "events", "run", "stop"):
        verb = a
        break
resp = script.get(verb, script.get("*"))
if resp is None:
    print(json.dumps({"api": 1, "rloop_version": "9.9.9", "ok": True,
                      "verb": verb, "warnings": [], "data": {}}))
    sys.exit(0)
if isinstance(resp, str):
    sys.stdout.write(resp)
    sys.exit(int(script.get("_rc", 0)))
print(json.dumps(resp, ensure_ascii=False))
sys.exit(int(script.get("_rc", 0)))
'''


@pytest.fixture
def fake_core(tmp_path):
    """一个假 rloop。返回 (client 工厂, 设置剧本的函数)。"""
    exe = tmp_path / "fake-rloop"
    exe.write_text(FAKE, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)

    script: dict = {}

    def make() -> Client:
        os.environ["FAKE_SCRIPT"] = json.dumps(script, ensure_ascii=False)
        return Client([sys.executable, str(exe)])

    def set_script(**kw):
        script.clear()
        script.update(kw)

    yield make, set_script
    os.environ.pop("FAKE_SCRIPT", None)


def envelope(verb: str, data: dict, **kw) -> dict:
    return {"api": 1, "rloop_version": "9.9.9", "ok": True, "verb": verb,
            "warnings": [], "data": data, **kw}


# ─────────── 找到核心 ───────────

def test_rloop_bin_wins(tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "rloop.py"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    monkeypatch.setenv("RLOOP_BIN", str(target))
    assert find_core() == [sys.executable, str(target)]


def test_a_broken_rloop_bin_says_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("RLOOP_BIN", str(tmp_path / "根本不存在"))
    with pytest.raises(CoreNotFound) as e:
        find_core()
    assert "不存在" in e.value.message
    assert e.value.hint, "报错没告诉用户怎么办"


def test_it_falls_back_to_the_sibling_in_the_same_repo(monkeypatch):
    """同一个仓库里的 rloop.py —— 从源码跑的时候走的就是这条。"""
    monkeypatch.delenv("RLOOP_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert find_core() == [sys.executable, str(REPO_ROOT / "rloop.py")]


def test_no_core_anywhere_is_a_sentence_a_human_can_act_on(monkeypatch):
    monkeypatch.delenv("RLOOP_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(CoreNotFound) as e:
        find_core()
    assert "RLOOP_BIN" in e.value.hint and "PATH" in e.value.hint


# ─────────── 握手 ───────────

def test_handshake_reads_the_capability_map(fake_core):
    make, script = fake_core
    script(meta=envelope("meta", {
        "api": API, "rloop_version": "9.9.9",
        "methods": ["meta", "loops", "loop"],
        "features": {"progress_for_reviewer": {"codex": True, "claude": False}},
        "status_class": {"converged": "ok"},
        "classes": ["normal", "ok"],
    }))
    c = make()
    ct = c.handshake()
    assert ct.matches and ct.core_version == "9.9.9"
    assert ct.has_method("loops") and not ct.has_method("run")
    assert ct.has_fine_progress("codex") and not ct.has_fine_progress("claude")


def test_a_version_mismatch_is_explained_not_fatal(fake_core):
    """老面板遇到新核心：说清楚，不崩。只读的部分多半还能用。"""
    make, script = fake_core
    script(meta=envelope("meta", {"api": API + 5, "rloop_version": "9.9.9"}))
    ct = make().handshake()
    assert not ct.matches
    assert str(API) in ct.mismatch_note and str(API + 5) in ct.mismatch_note


def test_an_unknown_status_is_flagged_not_silently_greyed(fake_core):
    """表里没有的状态值要记下来，界面上说一声，不许静默变灰。"""
    make, script = fake_core
    script(meta=envelope("meta", {"api": API, "status_class": {"converged": "ok"}}))
    ct = make().handshake()
    assert ct.status_class("converged") == "ok"
    assert ct.status_class("某个新状态") == "normal"
    assert "status_class:某个新状态" in ct.unknown


def test_the_contract_falls_back_when_meta_is_thin():
    """meta 少了几个键也不该白屏 —— 契约允许加字段，老面板得撑住。"""
    ct = Contract({"api": API})
    assert ct.status_class("converged") == "ok"      # 走兜底表
    assert ct.severity_class("critical") == "err"
    assert "normal" in ct.classes


# ─────────── 错误 ───────────

def test_an_error_payload_becomes_an_exception_with_the_core_words(fake_core):
    make, script = fake_core
    script(loops={"api": 1, "rloop_version": "9.9.9", "ok": False, "verb": "loops",
                  "warnings": [],
                  "error": {"code": "not_found", "message": "找不到 loop：abc",
                            "hint": "先看 loops", "detail": {"id": "abc"}}},
           _rc=4)
    with pytest.raises(CoreFailed) as e:
        make().loops()
    assert e.value.code == "not_found"
    assert e.value.message == "找不到 loop：abc"
    assert e.value.hint == "先看 loops"
    assert e.value.detail["id"] == "abc"


def test_garbage_output_is_not_mistaken_for_data(fake_core):
    """不是 JSON 就说不是 JSON，别硬解析出个空结果来。"""
    make, script = fake_core
    script(loops="这不是 JSON，是别的什么程序的输出\n")
    with pytest.raises(CoreUnintelligible):
        make().loops()


def test_silence_is_also_an_error(fake_core):
    make, script = fake_core
    script(loops="")
    with pytest.raises(CoreUnintelligible) as e:
        make().loops()
    assert e.value.hint, "什么都没输出时要提示可能是版本太老"


# ─────────── 调用形状 ───────────

def test_meta_is_the_only_call_without_a_version(fake_core, monkeypatch):
    """meta 是用来协商的，不能要求先协商。"""
    seen = []
    make, script = fake_core
    c = make()
    real = c._run

    def spy(argv, timeout=None):
        seen.append(argv)
        return real(argv, timeout)
    monkeypatch.setattr(c, "_run", spy)

    script(meta=envelope("meta", {"api": API}), loops=envelope("loops", {"loops": []}))
    os.environ["FAKE_SCRIPT"] = json.dumps(
        {"meta": envelope("meta", {"api": API}),
         "loops": envelope("loops", {"loops": []})}, ensure_ascii=False)
    c.handshake()
    c.loops()
    assert seen[0] == ["api", "meta"], seen[0]
    assert seen[1][:3] == ["api", "--api", str(API)], seen[1]


def test_run_passes_the_whole_form_through(fake_core, monkeypatch):
    """面板那张表单上的每个字段都要能到达核心。"""
    make, script = fake_core
    script(run=envelope("run", {"started": True, "loop": "L1"}))
    c = make()
    seen = []
    real = c._run
    monkeypatch.setattr(c, "_run", lambda argv, timeout=None: (seen.append(argv),
                                                               real(argv, timeout))[1])
    c.run("/p", new=True, focus="只看并发", base="main", max_rounds=3,
          min_score=8.5, reviewer="codex", reviewer_effort="high", no_verify=True)
    argv = seen[0]
    for expect in ("--new", "--focus", "只看并发", "--base", "main",
                   "-n", "3", "--min-score", "8.5", "--reviewer", "codex",
                   "--reviewer-effort", "high",
                   # 只读档是安全开关。它只在命令行上能碰的话，从面板起的每一轮
                   # 都带着写权限 —— 而面板正是「审别人代码」时最顺手的入口。
                   "--no-verify"):
        assert expect in argv, f"表单字段 {expect} 没传给核心：{argv}"

    # 不选就不能带上：默认档是放开的，多一个 flag 会把它悄悄关成只读
    seen.clear()
    c.run("/p", reviewer="codex")
    assert "--no-verify" not in seen[0]


def test_empty_form_fields_are_not_passed_as_empty_flags(fake_core, monkeypatch):
    """留空的输入框不该变成 `--focus ''`。"""
    make, script = fake_core
    script(run=envelope("run", {"started": True}))
    c = make()
    seen = []
    real = c._run
    monkeypatch.setattr(c, "_run", lambda argv, timeout=None: (seen.append(argv),
                                                               real(argv, timeout))[1])
    c.run("/p", focus="", base=None, max_rounds=None)
    assert "--focus" not in seen[0] and "--base" not in seen[0] and "-n" not in seen[0]


def test_follow_skips_unparsable_lines(fake_core, tmp_path):
    """契约要求读者跳过解析失败的行。"""
    streamer = tmp_path / "streamer"
    streamer.write_text(
        '#!/usr/bin/env python3\nimport sys\n'
        'sys.stdout.write(\'{"kind":"a"}\\n这不是 JSON\\n{"kind":"b"}\\n\')\n',
        encoding="utf-8")
    streamer.chmod(streamer.stat().st_mode | stat.S_IEXEC)
    c = Client([sys.executable, str(streamer)])
    got = [e["kind"] for e in c.follow("L1")]
    assert got == ["a", "b"], f"没跳过坏行：{got}"
