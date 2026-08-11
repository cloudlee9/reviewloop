#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`rloop api` 的契约。

这是 GUI 和核心之间唯一的接触面。GUI 不 import 核心的任何模块，只起子进程、
读 stdout —— 所以这里断言的每一条，都是「面板会不会突然显示不出东西」。

关注点分三类：
1. **自洽**：核心自己产出的状态值，必须在自己声明的配色表里查得到。
   漏一个的失效模式是静默变灰，没人会注意到。
2. **只读**：查询类 verb 一个字节都不许往磁盘写。
3. **形状**：envelope、错误码、stdout/stderr 的分工。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_fake_agents import Harness, review, PASSING  # noqa: E402


def api(*argv, env=None, cwd=None):
    """跑一条 api 命令，返回 (退出码, 解析后的负载, stderr)。"""
    r = subprocess.run([sys.executable, str(REPO_ROOT / "rloop.py"), "api", *argv],
                       capture_output=True, text=True, timeout=120, env=env, cwd=cwd)
    payload = None
    if r.stdout.strip().startswith("{"):
        payload = json.loads(r.stdout)
    return r.returncode, payload, r.stderr


# ─────────── 一、自洽 ───────────

def test_every_outcome_the_core_can_produce_has_a_colour():
    """`finish()` 能写出的每一个 outcome，都要在 STATUS_STYLE 里有条目。

    机械扫源码，不靠人记得同步。漏一个的症状是那个状态在面板上静默变灰 ——
    今天 gui/rloop_web.py 的 STAT 表就把核心的 `running -> accent` 抄成了空串。
    """
    src = (REPO_ROOT / "rloop.py").read_text(encoding="utf-8")
    body = src[src.index("def run_one_round"):src.index("# ─────────────────────────── 其余子命令")]
    outcomes = set(re.findall(r'finish\(loop,\s*"([a-z_]+)"', body))
    outcomes |= set(re.findall(r'outcome="([a-z_]+)"', body))
    assert outcomes, "一个 outcome 都没扫到，正则该跟着代码改了"

    meta = rloop.api_meta()
    for o in outcomes:
        assert o in rloop.STATUS_STYLE, f"outcome `{o}` 在 STATUS_STYLE 里没有配色"
        assert o in meta["status_class"], f"outcome `{o}` 没进 api meta 的 status_class"


def test_every_class_referenced_is_declared():
    """所有配色表的值，必须都在 meta.classes 这个闭包里。

    GUI 拿 classes 建 CSS；出现表外的值就是一个没有样式的元素。
    """
    meta = rloop.api_meta()
    known = set(meta["classes"])
    for table in ("status_class", "severity_class", "verdict_class"):
        unknown = set(meta[table].values()) - known
        assert not unknown, f"{table} 里有 classes 之外的值：{unknown}"


def test_every_severity_the_schema_allows_has_a_colour():
    """review schema 允许的每个 severity 都要能查到配色。"""
    schema = json.dumps(rloop.REVIEW_SCHEMA)
    for sev in ("critical", "high", "medium", "low"):
        assert sev in schema, f"schema 里没有 severity `{sev}`，这个测试该更新了"
        assert sev in rloop.SEV_STYLE, f"severity `{sev}` 没有配色"


def test_declared_artifacts_all_resolve_to_a_path(tmp_path):
    """meta 里声明的每个产物都要能算出路径，不能有算不出来的死条目。"""
    loop = rloop.Loop(tmp_path / "some-loop")
    for what in rloop.api_meta()["artifacts"]:
        p = rloop.artifact_path(loop, what, 3)
        assert isinstance(p, Path) and p.name


def test_the_sandbox_tier_reaches_the_panel_both_ways(tmp_path):
    """档位得**看得见**也**选得着**，两头都要通。

    只看得见的话，面板上每一轮都带着写权限起 —— 而「审别人的代码」恰恰是最会
    从面板顺手起一轮的场景。所以 run 要认 no_verify，loop 摘要要透出 verify，
    meta 里要声明这个维度存在（第三方面板据此决定给不给这个开关）。
    """
    meta = rloop.api_meta()
    assert meta["features"]["run_accepts_no_verify"] is True
    assert meta["features"]["verify_default"] is True
    assert meta["features"]["voided_rounds"] is True

    # api_run 真的会把它变成命令行参数（不起进程，只看拼出来的 argv）
    src = (REPO_ROOT / "rloop.py").read_text(encoding="utf-8")
    assert 'opts.get("no_verify")' in src and '"--no-verify"' in src


def test_event_kinds_cover_what_the_core_actually_emits():
    """核心 emit 的 kind 必须都在 meta.event_kinds 里声明过。"""
    src = (REPO_ROOT / "rloop.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'\.emit\(\s*"([a-z.]+)"', src))
    emitted |= {ev["kind"] for ev in
                (rloop.parse_codex_event(l) for l in (
                    '{"type":"item.started","item":{"type":"command_execution","command":"x"}}',
                    '{"type":"item.completed","item":{"type":"command_execution",'
                    '"command":"x","exit_code":1}}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"x"}}',
                    '{"type":"item.completed","item":{"type":"error","message":"x"}}',
                    '{"type":"turn.completed"}',
                )) if ev}
    declared = set(rloop.api_meta()["event_kinds"])
    assert emitted <= declared, f"这些 kind 发出去了但没在 meta 里声明：{emitted - declared}"


# ─────────── 二、只读 ───────────

def test_browsing_never_creates_a_directory(tmp_path):
    """查询类 verb 一个目录都不许建。

    读路径以前走 round_dir（带 mkdir），光是在面板上点点看看，
    `.review-loops/` 下就会长出一堆空的 round-NN/。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    lid = r.state["id"]
    root = r.loop.root
    before = sorted(p.name for p in root.iterdir())

    for argv in (["--api", "1", "loops"],
                 ["--api", "1", "loop", lid],
                 ["--api", "1", "loop", lid, "--round", "99"],
                 ["--api", "1", "file", lid, "--what", "diff"],
                 ["--api", "1", "file", lid, "--what", "diff", "--round", "99"],
                 ["--api", "1", "file", lid, "--what", "response"],
                 ["meta"]):
        api(*argv, env=h.env)

    assert sorted(p.name for p in root.iterdir()) == before, "查询把目录搞脏了"


def test_a_missing_round_is_not_found_not_an_empty_answer(tmp_path):
    h = Harness(tmp_path, [review()])
    r = h.run()
    rc, payload, err = api("--api", "1", "loop", r.state["id"], "--round", "9",
                           env=h.env)
    assert rc == rloop.EXIT_API_NOT_FOUND
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["detail"]["rounds_available"] == [1], \
        "报不存在的同时要告诉调用方有哪些轮次"


# ─────────── 三、形状 ───────────

def test_meta_needs_no_version_but_everything_else_does():
    """meta 是协商用的，自然不能要求先协商。"""
    rc, payload, _ = api("meta")
    assert rc == 0 and payload["data"]["api"] == rloop.API_VERSION

    rc, payload, _ = api("--api", "999", "loops")
    assert rc == rloop.EXIT_API_BAD_REQUEST
    assert payload["error"]["code"] == "unsupported_api_version"
    assert str(rloop.API_VERSION) in payload["error"]["message"], \
        "版本对不上时要说清核心提供的是哪个版本"


def test_errors_are_json_on_stdout_never_a_bare_die(tmp_path):
    """api 分支绝不 die()。

    `die()` 往 stderr 写一句中文就 SystemExit(1)，调用方拿到空 stdout
    加一句没法解析的话 —— 面板只能显示「出错了」三个字。
    """
    h = Harness(tmp_path, [review()])
    h.run()
    for argv, code in (
        (["--api", "1", "loop", "根本没有这个id"], rloop.EXIT_API_NOT_FOUND),
        (["--api", "1", "file", "根本没有这个id", "--what", "diff"], rloop.EXIT_API_NOT_FOUND),
    ):
        rc, payload, err = api(*argv, env=h.env)
        assert rc == code
        assert payload is not None and payload["ok"] is False, "出错时 stdout 不是 JSON"
        assert payload["error"]["message"], "错误负载没有给人看的话"
        assert err == "", f"api 不该往 stderr 写东西，实际写了：{err[:200]}"


def test_the_summary_never_leaks_the_raw_state(tmp_path):
    """LoopSummary 不含 loop.json 全量 —— 内部结构不成为对外契约。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    rc, payload, _ = api("--api", "1", "loops", env=h.env)
    assert rc == 0
    summary = payload["data"]["loops"][0]
    assert "state" not in summary, "把 loop.json 全量吐出去了，内部结构就锁死了"
    assert all(not isinstance(v, dict) or k in ("",) for k, v in summary.items()
               if k != "consistency_errors"), "投影里混进了嵌套结构"


def test_every_envelope_carries_version_and_warnings(tmp_path):
    h = Harness(tmp_path, [review()])
    r = h.run()
    for argv in (["meta"], ["--api", "1", "loops"],
                 ["--api", "1", "loop", r.state["id"]],
                 ["--api", "1", "file", r.state["id"], "--what", "diff"]):
        rc, payload, _ = api(*argv, env=h.env)
        assert rc == 0, argv
        assert payload["api"] == rloop.API_VERSION
        assert payload["rloop_version"] == rloop.VERSION
        assert payload["ok"] is True
        assert isinstance(payload["warnings"], list), "warnings 必须始终存在"
        assert payload["verb"] == argv[-1] if argv[0] == "meta" else True


def test_raw_skips_the_envelope(tmp_path):
    h = Harness(tmp_path, [review()])
    r = h.run()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "rloop.py"), "api", "--api", "1", "file",
         r.state["id"], "--what", "result", "--raw"],
        capture_output=True, text=True, env=h.env, timeout=120)
    assert proc.returncode == 0
    body = json.loads(proc.stdout)      # 直接就是 review.json 本身
    assert "deliverable_maturity" in body, "--raw 应当直出产物字节，不包 envelope"


def test_truncation_is_reported_not_silent(tmp_path):
    h = Harness(tmp_path, [review()])
    r = h.run()
    rc, payload, _ = api("--api", "1", "file", r.state["id"], "--what", "diff",
                         "--max-bytes", "64", env=h.env)
    assert rc == 0
    assert payload["data"]["truncated"] is True
    assert payload["warnings"], "截断了却不吭声，调用方会以为看到的是全部"
    assert payload["data"]["bytes"] > len(payload["data"]["text"].encode())


def test_report_falls_back_to_rendering(tmp_path):
    """跑到一半、还没有 report.md 的 loop，点报告不该是一片空白。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    (r.loop.root / "report.md").unlink()
    rc, payload, _ = api("--api", "1", "file", r.state["id"], "--what", "report",
                         env=h.env)
    assert rc == 0, "报告不存在时应当现渲染，而不是 not_found"
    assert payload["data"]["rendered"] is True
    assert payload["data"]["exists"] is False
    assert len(payload["data"]["text"]) > 0


# ─────────── 四、那个会烧配额的坑 ───────────

def test_no_subcommand_can_be_mistaken_for_a_focus_string():
    """每个注册过的子命令都必须被 normalize_argv 认出来。

    `normalize_argv` 对不认识的第一个词会改写成 `review <那个词>` 当侧重点文本。
    漏掉「api」的后果不是报错，是 `rloop api loops` **起一轮真 review**，
    烧掉两个模型的配额。
    """
    # 先钉住来源：这条测试遍历 subcommands()，要是有人把它改回手抄的常量、
    # 又抄漏了一个名字，下面的循环压根遍历不到那个名字，测试会假绿。
    assert rloop.subcommands() == rloop.registered_subcommands(), \
        "子命令集合和 argparse 实际注册的对不上了"

    for name in rloop.subcommands():
        assert rloop.normalize_argv([name]) == [name], \
            f"子命令 `{name}` 会被当成 focus 文本，敲它就是起一轮 review"


def test_a_non_subcommand_is_still_treated_as_focus():
    """反过来别矫枉过正：普通词还得是侧重点。"""
    assert rloop.normalize_argv(["重点看并发"]) == ["review", "重点看并发"]
    assert rloop.normalize_argv([]) == ["review"]


def test_api_verbs_and_declared_methods_agree():
    """meta 声明的 methods 要和 argparse 的 choices 对得上。"""
    parser = rloop.build_parser()
    for act in parser._subparsers._group_actions:
        if "api" in getattr(act, "choices", {}):
            verb_action = next(a for a in act.choices["api"]._actions
                               if a.dest == "verb")
            assert set(verb_action.choices) == set(rloop.api_meta()["methods"])
            return
    pytest.fail("没找到 api 子解析器")


# ─────────── 五、run / stop ───────────

def test_run_refuses_to_guess_the_project(tmp_path):
    """没有 --project 就报错，绝不退回自己进程的 cwd。

    以前 web 在没选中 loop 时传 null，服务端悄悄 fallback 到面板进程的 cwd ——
    用户完全看不见这个替换发生了，审的是哪个仓库全凭运气。切开之后没有共享
    cwd 可借，隐式 fallback 必须消失。
    """
    rc, payload, _ = api("--api", "1", "run")
    assert rc == rloop.EXIT_API_BAD_REQUEST
    assert payload["error"]["code"] == "bad_request"

    notgit = tmp_path / "notgit"
    notgit.mkdir()
    rc, payload, _ = api("--api", "1", "run", "--project", str(notgit))
    assert rc == rloop.EXIT_API_BAD_REQUEST
    assert "git" in payload["error"]["message"]


def test_a_failed_spawn_hands_back_rloops_own_words(tmp_path):
    """起不来时把 rloop 自己说的原话交出去。

    `nothing to review` 和 `not a git repository` 都在 loop 创建之前 die()、
    只写 stderr。把 stderr 丢进 DEVNULL 的话，第一个配置错误的用户只会看到
    一句毫无信息量的「起不来」。
    """
    project = tmp_path / "clean"
    project.mkdir()
    (project / "a.txt").write_text("x\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)

    env = dict(os.environ, RLOOP_HOME=str(tmp_path / "home"))
    rc, payload, _ = api("--api", "1", "run", "--project", str(project), env=env)
    assert rc == rloop.EXIT_API_SPAWN_FAILED
    assert payload["error"]["code"] == "spawn_failed"
    detail = payload["error"]["detail"]
    assert detail["exit_code"] != 0
    assert "nothing to review" in detail["stderr"], \
        f"没把 rloop 的原话带回来，只有：{detail['stderr'][:200]}"


def test_run_returns_before_the_round_finishes(tmp_path):
    """run 是短命命令：起个 detached runner 就返回，调用方不持有句柄。"""
    h = Harness(tmp_path, [review()])
    started = time.monotonic()
    rc, payload, _ = api("--api", "1", "run", "--project", str(h.project), env=h.env)
    took = time.monotonic() - started

    assert rc == 0, payload
    d = payload["data"]
    assert d["started"] is True and d["loop"] and d["is_new"] is True
    assert took < 20, f"run 等到了这一轮跑完（{took:.1f}s），它该起完就走"

    # 那一轮确实在自己跑，跟发起它的进程没关系了
    for _ in range(300):
        rc2, p2, _ = api("--api", "1", "loop", d["loop"], env=h.env)
        if rc2 == 0 and p2["data"]["loop"]["round"] >= 1 and not p2["data"]["loop"]["running"]:
            break
        time.sleep(0.1)
    assert p2["data"]["loop"]["round"] == 1
    assert p2["data"]["loop"]["status"] == "needs_work"


def test_stopping_an_idle_loop_is_not_an_error(tmp_path):
    """没在跑也返回 0 —— 面板上点「停」不该因为手快而变成红色错误。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    rc, payload, _ = api("--api", "1", "stop", r.state["id"], env=h.env)
    assert rc == 0
    assert payload["data"]["was_running"] is False
    assert payload["data"]["killed"] == []
    assert payload["data"]["message"], "至少要说清为什么没什么可停的"


def test_stop_needs_an_id(tmp_path):
    rc, payload, _ = api("--api", "1", "stop")
    assert rc == rloop.EXIT_API_BAD_REQUEST
    assert payload["error"]["code"] == "bad_request"


def test_run_refuses_when_that_project_already_has_one_running(tmp_path, monkeypatch):
    """已经有一轮在跑就退 conflict，而不是让两个 runner 撞在一起。

    这只是把最常见的情况变成一句人话 —— 权威的并发保护仍然是子进程自己的
    project_lock + loop_lock。预检**绝不碰锁文件**：loop_lock 会 mkdir + 建
    .lock + LOCK_EX，把只读探针变成写操作，还会把恰好在那一瞬启动的真 rloop
    die() 掉。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    loop = r.loop
    st = loop.state
    st["status"] = "running"
    st["runner_pid"] = 4242
    st["runner_started"] = "Mon Jan  1 00:00:00 2001"
    loop.save(st)

    monkeypatch.setattr(rloop, "pid_is_our_runner", lambda *a, **kw: True)
    lock_touched = []
    monkeypatch.setattr(rloop, "loop_lock",
                        lambda *a, **kw: lock_touched.append(a) or (_ for _ in ()).throw(
                            AssertionError("预检碰了 loop_lock")))

    with pytest.raises(rloop.ApiError) as e:
        rloop.api_run(h.project, {})
    assert e.value.code == "conflict"
    assert e.value.exit_code == rloop.EXIT_API_CONFLICT
    assert st["id"] in e.value.message
    assert not lock_touched, "忙碌预检碰了锁文件"


def test_run_proceeds_when_the_recorded_runner_is_gone(tmp_path, monkeypatch):
    """状态停在 running 但进程早没了（崩了或被 stop 掉）→ 照常起，走接管那条路。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    loop = r.loop
    st = loop.state
    st["status"] = "running"
    st["runner_pid"] = 999999
    st["runner_started"] = "Mon Jan  1 00:00:00 2001"
    loop.save(st)

    monkeypatch.setattr(rloop, "pid_is_our_runner", lambda *a, **kw: False)
    data, warnings = rloop.api_run(h.project, {})     # 不该抛 conflict
    assert data["started"] is True


def test_run_only_claims_the_loop_its_own_child_created(tmp_path, monkeypatch):
    """`api run` 只认自己 fork 出来的那个进程写下的 loop。

    回归用例：认领判据是「前后快照多了一个 loop」，而两个并发的 api run 会拿到
    相同的 before 快照 —— 其中一个 runner 抢到锁把 loop 建起来后，两个父进程
    都会看到同一个新 run_id，于是都返回 started=true。第二个调用方会以为自己的
    focus / reviewer / --new 生效了，实际被领到别人建的 loop 上。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()                      # 先有一个已存在的 loop
    other = r.loop

    class FakeProc:
        pid = 111111                 # 和账本里那个对不上
        def poll(self):
            return 3                 # 我们自己的子进程很快就退了（锁冲突）

    def fake_popen(*a, **kw):
        # 时序是这条用例的关键：**在 before 快照取完之后**，别的请求才把 loop
        # 建起来。这正是并发时的真实顺序 —— 提前改的话旧逻辑也不会认领它，
        # 用例就验不到东西了（第一版就是这么写错的，变异测试当场抓出来）。
        st = other.state
        st["created_ns"] = 9 * 10 ** 18
        st["run_id"] = "别人的-999"
        st["runner_pid"] = 424242
        other.save(st)
        return FakeProc()

    monkeypatch.setattr(rloop.subprocess, "Popen", fake_popen)
    # pid_field 内部也走 subprocess，会撞上上面那个 mock。这条用例验的是
    # pid 归属，启动时刻校验让它空着跳过即可。
    monkeypatch.setattr(rloop, "pid_field", lambda *a, **kw: "")
    monkeypatch.setattr(rloop, "SPAWN_WAIT_SECONDS", 2.0)

    with pytest.raises(rloop.ApiError) as e:
        rloop.api_run(h.project, {})
    assert e.value.code == "spawn_failed", \
        f"认领了别人建的 loop，而不是报自己的子进程失败了：{e.value.code}"
    assert e.value.detail["exit_code"] == 3


def test_a_successful_run_does_not_leave_an_empty_error_log(tmp_path):
    """起成功了就把空的 spawn stderr 删掉，别在 ~/.rloop 里堆一地。"""
    h = Harness(tmp_path, [review()])
    home = Path(h.env["RLOOP_HOME"])
    rc, payload, _ = api("--api", "1", "run", "--project", str(h.project), env=h.env)
    assert rc == 0, payload

    for _ in range(300):
        rc2, p2, _ = api("--api", "1", "loop", payload["data"]["loop"], env=h.env)
        if rc2 == 0 and not p2["data"]["loop"]["running"]:
            break
        time.sleep(0.1)

    leftovers = list(home.glob("spawn-*.err"))
    assert not leftovers, f"起成功了还留下空的 stderr：{[f.name for f in leftovers]}"


def test_a_failed_run_keeps_its_error_log(tmp_path):
    """反过来：失败的那份要留着，人得看得到 rloop 说了什么。"""
    project = tmp_path / "clean"
    project.mkdir()
    (project / "a.txt").write_text("x\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    home = tmp_path / "home"
    env = dict(os.environ, RLOOP_HOME=str(home))

    rc, payload, _ = api("--api", "1", "run", "--project", str(project), env=env)
    assert rc == rloop.EXIT_API_SPAWN_FAILED
    kept = list(home.glob("spawn-*.err"))
    assert kept, "失败的 stderr 被删了，用户没处看原因"
    assert "nothing to review" in kept[0].read_text(encoding="utf-8")


def test_old_spawn_logs_get_swept(tmp_path, monkeypatch):
    """过期的那些兜底清掉 —— 失败留下的不会有人回头删。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(rloop, "RLOOP_HOME", home)
    old = home / "spawn-1-1.err"
    new = home / "spawn-2-2.err"
    old.write_text("旧的", encoding="utf-8")
    new.write_text("新的", encoding="utf-8")
    ancient = time.time() - (rloop.SPAWN_LOG_KEEP_DAYS + 1) * 86400
    os.utime(old, (ancient, ancient))

    rloop.sweep_spawn_logs()
    assert not old.exists(), "过期的没清掉"
    assert new.exists(), "把还新的也清了"


def test_run_does_not_claim_a_history_loop_whose_pid_got_recycled(tmp_path, monkeypatch):
    """pid 被系统回收复用时，不能认领那个历史 loop。

    回归用例：认领只比 runner_pid。正常结束或中途死掉的 loop 会一直留着旧的
    runner_pid/run_id，系统把那个号发给我们这个新子进程之后，扫描就会命中
    历史 loop 并返回一个根本不是本次启动的 started=true。
    项目里其他探活一律 pid + lstart 一起比，这里当初漏了。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    old_loop = r.loop

    st = old_loop.state
    st["status"] = "done"
    st["runner_pid"] = 111111                    # 和下面 FakeProc 的 pid 一样
    st["runner_started"] = "Mon Jan  1 00:00:00 2001"   # 但那是很久以前的进程
    st["run_id"] = "上一次的-1"
    old_loop.save(st)

    class FakeProc:
        pid = 111111
        def poll(self):
            return 3

    monkeypatch.setattr(rloop.subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(rloop, "pid_field",
                        lambda pid, field: "Sun Aug 10 02:00:00 2026")  # 我们这个的
    monkeypatch.setattr(rloop, "SPAWN_WAIT_SECONDS", 2.0)

    with pytest.raises(rloop.ApiError) as e:
        rloop.api_run(h.project, {})
    assert e.value.code == "spawn_failed", \
        f"认领了 pid 撞车的历史 loop：{e.value.code}"
