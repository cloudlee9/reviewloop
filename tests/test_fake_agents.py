#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用假 reviewer 打通 rloop 的整条外壳：CLI 分发 → 一轮 review → 判定 → 续轮。

`tests/test_rloop.py` 只测判定层的纯函数，`tests/test_integration.py` 的第 3 档要真
模型、要配额。中间这一段——**零参数 CLI 分发、每轮产物、reviewer 拿到什么命令行、
退出码契约、--json 载荷、跨轮续接**——只能在这里覆盖。

做法是把 `claude` / `codex` 换成 PATH 上的假可执行文件：按剧本吐出预先写好的 review
JSON，并把自己收到的完整 argv 记下来。于是能断言的东西和真链路一样多，但不花配额、
不联网、秒级完成，所以放在默认档跑。

rloop 自己不改代码——处理 findings 的是调用方的开发会话。所以这里没有"假 fixer"：
需要模拟"作者改了代码"时，测试自己去动工作区，这也更贴近真实。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402


# ─────────────────────────── 假 reviewer ───────────────────────────

FAKE_AGENT = '''#!/usr/bin/env python3
"""claude / codex 的替身，只扮演 reviewer —— rloop 不再起别的 agent。"""
import json, os, pathlib, sys

argv = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_LOG"])
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"agent": pathlib.Path(sys.argv[0]).name,
                        "argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\\n")

import time
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
reviews = json.loads(pathlib.Path(os.environ["FAKE_REVIEWS"]).read_text("utf-8"))
counter = pathlib.Path(os.environ["FAKE_COUNTER"])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
payload = json.dumps(reviews[min(n, len(reviews) - 1)], ensure_ascii=False)

for ev in json.loads(os.environ.get("FAKE_EVENTS", "[]")):
    print(json.dumps(ev, ensure_ascii=False), flush=True)

tamper = os.environ.get("FAKE_TAMPER")       # 扮演一个动手改了被审代码的 reviewer
if tamper:
    t = pathlib.Path(tamper)
    t.write_text(t.read_text("utf-8") + "# reviewer 动的手\\n", encoding="utf-8")

drop = os.environ.get("FAKE_DROP")           # 扮演跑测试掉了个产物的 reviewer
if drop:
    pathlib.Path(drop).write_text("测试产物\\n", encoding="utf-8")

if "-o" in argv:                       # codex：自己写 -o 指定的文件
    pathlib.Path(argv[argv.index("-o") + 1]).write_text(payload, encoding="utf-8")
    print("fake reviewer: wrote review file")
else:                                  # claude：plan 模式没有写工具，只能走 stdout
    print(payload)
sys.exit(int(os.environ.get("FAKE_REVIEWER_RC", "0")))
'''


def review(deliverable=5.0, production=4.0, blocking=1, verdict="needs_work",
           findings=None, prior=None) -> dict:
    if findings is None:
        findings = [{
            "id": "F1",
            "severity": "high", "category": "correctness", "file": "app.py", "line": 1,
            "description": "示例问题。", "suggested_fix": "示例修法。",
        }]
    return {
        "deliverable_maturity": deliverable,
        "production_readiness": production,
        "blocking_findings": blocking,
        "verdict": verdict,
        "summary": "假 reviewer 的小结。",
        "findings": findings,
        "prior_findings_status": prior or [],
        "positive_evidence": [],
        "validation_commands": [],
        "next_priorities": [],
    }


PASSING = review(deliverable=9.0, production=9.0, blocking=0, verdict="pass", findings=[])


# ─────────────────────────── 夹具 ───────────────────────────


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    git(project, "config", "user.email", "t@t")
    git(project, "config", "user.name", "t")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")
    # 「刚 vibe 完」：留一坨未提交的改动等着被审
    (project / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    return project


class Harness:
    """一个临时项目 + 假 reviewer，可以反复跑 rloop，就像会话驱动循环那样。"""

    def __init__(self, tmp_path: Path, reviews: list, project: Path | None = None):
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)   # 允许传 tmp_path 的子目录，好在一条测试里开两个互不干扰的 harness
        self.project = project or make_project(tmp_path)
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir(exist_ok=True)
        for name in ("claude", "codex"):
            p = fakebin / name
            p.write_text(FAKE_AGENT, encoding="utf-8")
            p.chmod(0o755)
        (tmp_path / "reviews.json").write_text(
            json.dumps(reviews, ensure_ascii=False), encoding="utf-8")
        self.env = dict(
            os.environ,
            PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
            RLOOP_HOME=str(tmp_path / "rloop-home"),
            FAKE_LOG=str(tmp_path / "calls.jsonl"),
            FAKE_REVIEWS=str(tmp_path / "reviews.json"),
            FAKE_COUNTER=str(tmp_path / "counter"),
        )

    def run(self, *extra: str, json_out: bool = True) -> "Result":
        argv = [sys.executable, str(REPO_ROOT / "rloop.py"),
                "-C", str(self.project), "--notify", "none", *extra]
        if json_out and "--json" not in extra:
            argv.append("--json")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=self.env)
        return Result(proc, self)

    def author_edits(self, text: str = "# author edit\n"):
        """模拟开发会话动了工作区，好让下一轮 diff 有变化。"""
        p = self.project / "app.py"
        p.write_text(p.read_text("utf-8") + text, encoding="utf-8")

    def write_response(self, loop: rloop.Loop, rnd: int, text: str = "已按 findings 修改。"):
        (loop.round_dir(rnd) / "response.md").write_text(text, encoding="utf-8")


class Result:
    def __init__(self, proc, h: Harness):
        self.proc = proc
        self.h = h
        d = h.project / rloop.LOOP_DIRNAME
        # 按创建时间排，不按名字：同一秒内建的两个 loop 名字只差随机后缀，
        # 字典序不等于创建顺序，loops[-1] 会随机取错。
        roots = sorted((x for x in d.iterdir() if (x / "loop.json").exists()),
                       key=lambda x: (x / "loop.json").stat().st_mtime) if d.is_dir() else []
        self.loops = [rloop.Loop(x) for x in roots]
        self.loop = self.loops[-1] if self.loops else None
        log = h.tmp / "calls.jsonl"
        self.calls = [json.loads(l) for l in log.read_text("utf-8").splitlines()] \
            if log.exists() else []

    @property
    def rc(self) -> int:
        return self.proc.returncode

    @property
    def state(self) -> dict:
        return self.loop.state

    @property
    def payload(self) -> dict:
        return json.loads(self.proc.stdout)

    def fail_msg(self) -> str:
        return f"rc={self.rc}\n--stdout--\n{self.proc.stdout}\n--stderr--\n{self.proc.stderr}"


# ─────────────────────────── 单轮契约 ───────────────────────────


def test_one_run_is_exactly_one_review_and_nothing_else(tmp_path):
    """rloop 跑一轮就返回：只起一次 reviewer，绝不去改代码。"""
    h = Harness(tmp_path, [review()])
    before = (h.project / "app.py").read_text("utf-8")
    r = h.run()

    assert len(r.calls) == 1, f"应当只起一次 agent，实际 {len(r.calls)} 次"
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    assert (h.project / "app.py").read_text("utf-8") == before, "rloop 自己改了代码"
    assert r.state["round"] == 1


def test_passing_review_exits_zero_and_closes_the_loop(tmp_path):
    h = Harness(tmp_path, [PASSING])
    r = h.run()

    assert r.rc == rloop.EXIT_PASS, r.fail_msg()
    assert r.state["outcome"] == "converged"
    assert r.state["status"] == "done"
    assert r.payload["can_continue"] is False


def test_needs_work_leaves_the_loop_open_for_the_next_round(tmp_path):
    h = Harness(tmp_path, [review()])
    r = h.run()

    assert r.rc == rloop.EXIT_NEEDS_WORK
    assert r.state["status"] == "open", "未达标的 loop 必须留着让下一轮续"
    assert r.payload["can_continue"] is True
    assert r.payload["response_path"].endswith("round-01/response.md")


def test_reviewer_failure_is_reported_not_swallowed(tmp_path):
    h = Harness(tmp_path, [review()])
    h.env["FAKE_REVIEWER_RC"] = "9"
    r = h.run()

    assert r.rc == rloop.EXIT_ERROR, r.fail_msg()
    assert r.state["outcome"] == "failed"


def test_nothing_to_review_refuses_to_burn_a_call(tmp_path):
    project = make_project(tmp_path)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "everything committed")
    h = Harness(tmp_path, [review()], project=project)
    # 工作区干净、无分支差异 → 回退到审最后一个 commit，仍有东西可审
    r = h.run()
    assert r.rc in (rloop.EXIT_PASS, rloop.EXIT_NEEDS_WORK), r.fail_msg()
    assert "last commit" in r.state["scope_desc"]


# ─────────────────────────── 续轮 ───────────────────────────


RESOLVED = [{"id": "F1", "description": "示例问题。", "status": "fixed", "note": "已修。"}]
PASSING_R2 = review(deliverable=9.0, production=9.0, blocking=0, verdict="pass",
                    findings=[], prior=RESOLVED)


def test_second_run_continues_the_same_loop(tmp_path):
    """会话改完再跑一次 rloop，应当接在同一个 loop 上、轮次递增，而不是另起炉灶。"""
    h = Harness(tmp_path, [review(), PASSING_R2])
    first = h.run()
    assert first.rc == rloop.EXIT_NEEDS_WORK

    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_PASS, second.fail_msg()
    assert len(second.loops) == 1, "不该新建 loop"
    assert second.state["round"] == 2
    assert [e["round"] for e in second.state["history"]] == [1, 2]


def test_second_round_reviewer_sees_prior_findings_and_the_response(tmp_path):
    """无状态 reviewer 的连续性全靠账本：上轮 findings 和作者的回应都得喂回去。"""
    h = Harness(tmp_path, [review(), PASSING_R2])
    first = h.run()
    h.write_response(first.loop, 1, "反驳：第 3 行本来就不会为空，见 app.py:1。")
    h.author_edits()
    h.run()

    pack = (first.loop.round_dir(2) / "review-prompt.md").read_text("utf-8")
    assert "示例问题" in pack, "上轮 findings 没喂给这一轮的 reviewer"
    assert "prior_findings_status" in pack, "没要求它对上轮逐条裁决"
    assert "反驳：第 3 行本来就不会为空" in pack, "作者的回应没喂进去"


def test_missing_response_is_flagged_but_not_fatal(tmp_path):
    """没写回应也能继续，但日志要明说 reviewer 会因此判 not_fixed。"""
    h = Harness(tmp_path, [review(), review(prior=RESOLVED)])
    first = h.run()
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_NEEDS_WORK, second.fail_msg()
    log = first.loop.log_file.read_text("utf-8")
    assert "response.md" in log and "not_fixed" in log


def test_new_forces_a_separate_loop(tmp_path):
    h = Harness(tmp_path, [review(), review()])
    h.run()
    r = h.run("--new")

    assert len(r.loops) == 2, "--new 没有另起 loop"
    assert r.state["round"] == 1


def test_scope_flags_start_a_fresh_loop(tmp_path):
    """显式给了范围就是要审别的东西，不该硬接在开着的 loop 上。"""
    h = Harness(tmp_path, [review(), review()])
    h.run()
    r = h.run("--base", "HEAD")

    assert len(r.loops) == 2
    assert r.state["round"] == 1


def test_max_rounds_closes_the_loop(tmp_path):
    h = Harness(tmp_path, [review(), review(prior=RESOLVED)])
    first = h.run("-n", "2")
    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_NEEDS_WORK
    assert second.state["outcome"] == "exhausted"
    assert second.payload["can_continue"] is False, "跑满轮数还说能继续，会让调用方空转"


# ─────────────────────────── 门禁自洽 ───────────────────────────


def test_blocking_count_that_contradicts_findings_is_rejected(tmp_path):
    """双 9 分 + blocking=0 + verdict=pass，但 findings 里躺着 critical —— 不许放行。"""
    lying = review(deliverable=9.0, production=9.0, blocking=0, verdict="pass", findings=[{
        "id": "F1",
        "severity": "critical", "category": "security", "file": "app.py", "line": 1,
        "description": "远程代码执行。", "suggested_fix": "别 eval。",
    }])
    r = Harness(tmp_path, [lying]).run()

    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert r.state["outcome"] == "inconsistent"
    assert any("critical/high" in e for e in r.payload["consistency_errors"])


def test_verdict_that_contradicts_scores_is_rejected(tmp_path):
    bad = review(deliverable=9.0, production=9.0, blocking=0, verdict="needs_work", findings=[])
    r = Harness(tmp_path, [bad]).run()

    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert any("verdict=needs_work" in e for e in r.payload["consistency_errors"])


def test_out_of_range_score_is_rejected(tmp_path):
    r = Harness(tmp_path, [review(deliverable=42.0)]).run()
    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert any("超出 0-10" in e for e in r.payload["consistency_errors"])


def test_low_score_with_no_findings_is_rejected(tmp_path):
    """未达标却一条 findings 都不给，调用方无从下手，不能假装这是正常结果。"""
    r = Harness(tmp_path, [review(blocking=0, findings=[])]).run()

    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert "findings" in (r.state["outcome_reason"] or "")


def test_missing_prior_verdict_is_rejected(tmp_path):
    """漏掉对上轮某条的裁决，等于让那个问题从账本上消失 —— 必须拦。"""
    h = Harness(tmp_path, [review(), review(prior=[])])
    first = h.run()
    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_INCONSISTENT, second.fail_msg()
    assert any("漏项" in e for e in second.payload["consistency_errors"])


def test_unknown_prior_id_is_rejected(tmp_path):
    bogus = [{"id": "F99", "description": "凭空的", "status": "fixed", "note": "?"}]
    h = Harness(tmp_path, [review(), review(prior=bogus)])
    first = h.run()
    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_INCONSISTENT, second.fail_msg()
    assert any("不存在的 id" in e for e in second.payload["consistency_errors"])


def test_prior_verdicts_on_round_one_are_rejected(tmp_path):
    """第一轮没有"上一轮"，凭空给裁决说明它在编。"""
    r = Harness(tmp_path, [review(prior=RESOLVED)]).run()
    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert any("第一轮" in e for e in r.payload["consistency_errors"])


def test_unresolved_finding_dropped_from_the_list_is_rejected(tmp_path):
    """判成 not_fixed 却不再列在 findings 里 —— 那个问题就从账本上消失了。"""
    vanish = review(deliverable=9.0, production=9.0, blocking=0, verdict="pass", findings=[],
                    prior=[{"id": "F1", "description": "示例问题。",
                            "status": "not_fixed", "note": "还是没改。"}])
    h = Harness(tmp_path, [review(), vanish])
    first = h.run()
    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert second.rc == rloop.EXIT_INCONSISTENT, second.fail_msg()
    assert any("从账本上消失" in e for e in second.payload["consistency_errors"])


def test_findings_without_ids_get_one_instead_of_failing_the_round(tmp_path):
    """id 是 rloop 自己的追踪需求，模型漏给不该让整轮 review 作废 —— 补一个并警告。"""
    no_id = review(findings=[{
        "severity": "high", "category": "correctness", "file": "app.py", "line": 1,
        "description": "没带 id 的问题。", "suggested_fix": "改。",
    }])
    r = Harness(tmp_path, [no_id]).run()

    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    assert r.payload["findings"][0]["id"] == "R1F1"
    assert "没给 id" in r.loop.log_file.read_text("utf-8")


def test_schema_file_is_refreshed_every_round(tmp_path):
    """schema 升级后，已开着的 loop 必须换成新版 —— 否则 reviewer 按旧规矩产出，
    却被按新规矩写的自洽校验判成不合规，loop 卡死在退出码 3。"""
    h = Harness(tmp_path, [review(), PASSING_R2])
    first = h.run()

    sf = first.loop.root / "review-schema.json"
    sf.write_text('{"stale": true}', encoding="utf-8")
    h.write_response(first.loop, 1)
    h.author_edits()
    h.run()

    assert json.loads(sf.read_text("utf-8")) == rloop.REVIEW_SCHEMA, "旧 schema 没被刷新"


def test_a_second_run_cannot_fork_a_parallel_loop(tmp_path):
    """A 正在跑（status=running）时，B 不许绕过去新建第二个 loop。

    回归用例：早先 find_open_loop() 只认 open，A 一把状态改成 running，
    B 就看不见任何可续的 loop，转头新建一个并行跑同一份范围 —— 每-loop 锁
    挡不住，因为两边根本不在争同一个锁。这里让 B 在 A 确实进入 running
    之后才启动，压中那个窗口。
    """
    import threading, time

    h = Harness(tmp_path, [review(), review(prior=RESOLVED), review(prior=RESOLVED)])
    first = h.run()
    h.write_response(first.loop, 1)
    h.author_edits()

    h.env["FAKE_SLEEP"] = "3"
    slow = {}
    t = threading.Thread(target=lambda: slow.update(r=h.run()))
    t.start()

    # 等到 A 真的把状态写成 running 再让 B 进来
    deadline = time.time() + 20
    while time.time() < deadline:
        if first.loop.state.get("status") == "running":
            break
        time.sleep(0.05)
    else:
        t.join(timeout=60)
        pytest.fail("A 一直没有进入 running，用例没压中窗口")

    b = h.run()
    t.join(timeout=60)

    assert b.rc == 1, f"B 没被拦下：rc={b.rc}\n{b.proc.stdout}\n{b.proc.stderr}"
    assert "正在被另一个 rloop 进程跑着" in b.proc.stderr
    assert len(b.loops) == 1, "B 绕过锁另建了一个并行 loop"
    assert slow["r"].rc in (rloop.EXIT_PASS, rloop.EXIT_NEEDS_WORK), slow["r"].fail_msg()


def _race_two_first_runs(tmp_path, no_lock: bool):
    """两个进程在都还没看到任何 loop 时同时冲进"发现→新建"，返回建出的 loop 数。"""
    import threading

    barrier = tmp_path / "go"
    barrier2 = tmp_path / "go2"
    h = Harness(tmp_path, [review(), review()])
    h.env["RLOOP_TEST_BARRIER"] = str(barrier)
    h.env["FAKE_SLEEP"] = "2"
    if no_lock:
        # 无锁版再卡一道：两边都读完 find_active_loop 才放行，于是"双方同时
        # 看到空结果"是确定事件，而不是碰运气。
        h.env["RLOOP_TEST_NO_PROJECT_LOCK"] = "1"
        h.env["RLOOP_TEST_BARRIER2"] = str(barrier2)

    out = []
    threads = [threading.Thread(target=lambda: out.append(h.run())) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(1.5)          # 两边都堵在屏障上
    d = h.project / rloop.LOOP_DIRNAME
    assert not d.is_dir() or not any((x / "loop.json").exists() for x in d.iterdir()), \
        "屏障没拦住，用例又退化成了「A 建完 B 才来」"

    barrier.write_text("go", encoding="utf-8")   # 同时放行
    if no_lock:
        # 真握手：数 ready 文件，确认两个进程都已经跑完 find_active_loop 并
        # 读到空结果，再放行。sleep 猜时间在慢调度下会漏。
        deadline2 = time.time() + 30
        while time.time() < deadline2:
            if len(list(tmp_path.glob("go2.ready.*"))) >= 2:
                break
            time.sleep(0.02)
        else:
            pytest.fail("两个进程没有都抵达第二道屏障，无锁版证明不了竞态")
        barrier2.write_text("go", encoding="utf-8")
    for t in threads:
        t.join(timeout=120)
    assert len(out) == 2
    return [x for x in d.iterdir() if (x / "loop.json").exists()], out


def test_the_race_test_would_fail_without_the_project_lock(tmp_path):
    """自证：关掉项目锁，同样的场景必须建出两个 loop。

    F5 的判据 —— 一条抓不住 bug 的回归用例等于没有。把这个证明自动化，
    免得下次改动悄悄让上面那条用例退化成永远通过。
    """
    loops, _ = _race_two_first_runs(tmp_path, no_lock=True)
    assert len(loops) == 2, (
        f"没了项目锁却只建出 {len(loops)} 个 loop —— 那么下面那条用例通过也"
        f"说明不了什么，它可能压根没碰到竞态")


def test_two_first_runs_cannot_both_create_a_loop(tmp_path):
    """两个进程在都还没看到任何 loop 时同时冲进"发现→新建"：只能建出一个。

    上一版用例先等 A 写出 loop.json 再放 B 进来 —— 那时候 B 走的是
    "看到 running loop → 争 per-loop 锁 → busy"，就算把 project_lock 整个删掉
    也照样通过，根本回归不了首次创建竞态。现在用 RLOOP_TEST_BARRIER 让两边都
    卡在发现阶段之前，再同时放行。

    这条用例的有效性由 test_the_race_test_would_fail_without_the_project_lock
    自动担保，不靠人工做一次变异验证。
    """
    loops, out = _race_two_first_runs(tmp_path, no_lock=False)
    assert len(loops) == 1, f"首次启动竞态没被挡住，建出了 {len(loops)} 个 loop"

    busy = [r for r in out if r.rc == 1 and "正在被另一个 rloop 进程跑着" in r.proc.stderr]
    assert len(busy) == 1, f"应当恰好一个被拦下：{[(r.rc, r.proc.stderr[:70]) for r in out]}"


def test_stale_running_loop_is_taken_over(tmp_path):
    """进程被杀留下 status=running：下一次裸调应当接管它，而不是另起炉灶。"""
    h = Harness(tmp_path, [review(), review(prior=RESOLVED)])
    first = h.run()
    first.loop.update(status="running")     # 模拟上次被杀，没来得及收尾

    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    assert len(second.loops) == 1, "没接管，另起了一个 loop"
    assert "接管" in first.loop.log_file.read_text("utf-8")


def test_concurrent_continuations_are_serialised(tmp_path):
    """两个裸 rloop 同时续同一个 loop：只能有一个进去，另一个明确报 busy。

    没有互斥时两边会挑中同一个 open loop、跑同一轮、互相覆盖 round-NN、
    child_pid 和 history —— 原子写只保证单个 JSON 不半写，防不住丢失更新。
    """
    import threading

    h = Harness(tmp_path, [review(), review(prior=RESOLVED), review(prior=RESOLVED)])
    first = h.run()
    assert first.rc == rloop.EXIT_NEEDS_WORK, first.fail_msg()
    h.write_response(first.loop, 1)
    h.author_edits()

    h.env["FAKE_SLEEP"] = "2"          # 让先进来的那个占住锁
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.run()))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(results) == 2
    rcs = sorted(r.rc for r in results)
    assert rloop.EXIT_NEEDS_WORK in rcs or rloop.EXIT_PASS in rcs, \
        f"没有一个成功跑完：{[r.fail_msg() for r in results]}"
    busy = [r for r in results if r.rc == 1 and "正在被另一个 rloop 进程跑着" in r.proc.stderr]
    assert len(busy) == 1, f"应当恰好一个被拦下，实得 {[(r.rc, r.proc.stderr[:80]) for r in results]}"

    # 账本没被写坏：轮次连续，没有重复
    rounds = [e["round"] for e in first.loop.state["history"]]
    assert rounds == sorted(set(rounds)), f"history 轮次错乱：{rounds}"


def test_open_loop_is_picked_by_start_time_not_by_name(tmp_path):
    """loop id 只到秒、尾巴是随机后缀，字典序不等于创建顺序。"""
    h = Harness(tmp_path, [review(), review(), review(prior=RESOLVED)])
    a = h.run("--new")
    b = h.run("--new")
    assert len({l.root.name for l in b.loops}) == 2

    older, newer = a.loop, b.loop
    # 把新建那个的名字改成字典序更小，模拟随机后缀排在前面的情况
    assert older.root.name != newer.root.name
    newer.update(started_at="2099-01-01T00:00:00Z")
    older.update(started_at="2000-01-01T00:00:00Z")

    picked = rloop.find_active_loop(h.project)
    assert picked.root.name == newer.root.name, "没有按 started_at 选最近的那个"


def test_zero_findings_on_the_last_round_still_reports_inconsistent(tmp_path):
    """最后一轮出现"未达标却零 findings"，不能被 exhausted 掩盖成退出码 2。"""
    r = Harness(tmp_path, [review(blocking=0, findings=[])]).run("-n", "1")

    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert r.state["outcome"] == "inconsistent", \
        f"被掩盖成了 {r.state['outcome']}，调用方会以为只是没跑到点上"


def test_duplicate_finding_ids_are_rejected(tmp_path):
    dupes = [
        {"id": "F1", "severity": "high", "category": "c", "file": "a.py", "line": 1,
         "description": "一", "suggested_fix": "改"},
        {"id": "F1", "severity": "low", "category": "c", "file": "a.py", "line": 2,
         "description": "二", "suggested_fix": "改"},
    ]
    r = Harness(tmp_path, [review(blocking=1, findings=dupes)]).run()
    assert r.rc == rloop.EXIT_INCONSISTENT, r.fail_msg()
    assert any("重复" in e for e in r.payload["consistency_errors"])


def test_legacy_findings_without_ids_do_not_deadlock_the_loop(tmp_path):
    """磁盘上是加 id 之前跑的 loop，被新版本续上：没有可比对的键就跳过覆盖校验。

    否则一个跨版本的 loop 会卡死——续一次退 3，再续还是退 3，永远走不下去。
    """
    h = Harness(tmp_path, [review(), PASSING_R2])
    first = h.run()
    assert first.rc == rloop.EXIT_NEEDS_WORK, first.fail_msg()

    # 把第一轮产物降级成旧格式，模拟跨版本
    path = first.loop.round_dir(1) / "review.json"
    d = json.loads(path.read_text("utf-8"))
    for f in d["findings"]:
        f.pop("id", None)
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()
    assert second.rc == rloop.EXIT_PASS, second.fail_msg()


def test_garbage_review_is_an_error_not_a_pass(tmp_path):
    h = Harness(tmp_path, [review()])
    (tmp_path / "reviews.json").write_text('["not a review object"]', encoding="utf-8")
    r = h.run()

    assert r.rc == rloop.EXIT_ERROR, r.fail_msg()
    assert r.state["outcome"] == "failed"


# ─────────────────────────── reviewer 的命令行 ───────────────────────────


def test_codex_reviewer_is_opened_up_only_as_far_as_running_tests_needs(tmp_path):
    """默认放开到 workspace-write —— 再往上就没有沙箱了。

    reviewer 读的是可能被污染的代码，所以「够它跑测试」和「全权限」之间那条线
    要钉在 argv 上：workspace-write 让它跑得动 pytest、写得了临时文件，而内核仍然
    挡着 HOME；danger-full-access 或 --dangerously-bypass-* 一旦漏进来，仓库里
    一句提示注入就能落到这台机器上。

    沙箱和工作目录是 codex 的**全局**选项，必须排在子命令之前 —— 放到 `exec`
    后面 codex 会报 unexpected argument，那时沙箱不是变松了而是根本没起来。
    """
    r = Harness(tmp_path, [review()]).run()
    argv = r.calls[0]["argv"]

    assert argv[0] == "-s" and argv[1] == "workspace-write", f"沙箱不在最前：{argv[:4]}"
    assert "exec" in argv and argv.index("-s") < argv.index("exec"), \
        f"沙箱排到了子命令后面：{argv[:6]}"
    assert "-C" in argv and argv.index("-C") < argv.index("exec")
    assert "--output-schema" in argv, "没有 schema，模型可以自由发挥"
    assert "--ephemeral" in argv
    assert "danger-full-access" not in argv, "放开过头了"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv, "reviewer 拿到了全权限"
    assert "--dangerously-bypass-hook-trust" not in argv, "替仓库绕过了 hook trust"


def test_a_reviewer_caught_editing_the_code_voids_the_round(tmp_path):
    """指纹动了 **且** 它自己的日志里有动手痕迹 —— 两个信号齐了才作废。

    作废的理由不是洁癖：这一轮的判断建立在一份 reviewer 自己动过的代码上，
    "把测试改绿了"和"代码本来就对"在结果里长得一模一样。
    """
    h = Harness(tmp_path / "tamper", [review()])
    h.env["FAKE_TAMPER"] = str(h.project / "app.py")
    h.env["FAKE_EVENTS"] = json.dumps(
        [{"type": "item.completed", "item": {"type": "file_change", "path": "app.py"}}])
    r = h.run()

    assert r.rc == rloop.EXIT_ERROR, f"改了被审代码却没作废（退出码 {r.rc}）"
    log = (r.loop.root / "loop.log").read_text("utf-8")
    assert "作废" in log and "--no-verify" in log, "没告诉人怎么把它关回只读"


def test_droppings_left_behind_are_named_in_the_log(tmp_path):
    """变异测试发现这条提醒没人守：删掉它全套测试照样绿。

    它要紧是因为未跟踪文件**会进下一轮的送审范围** —— 不点名的话，下一轮作者会
    在补丁里看见一堆自己没写过的"改动"。
    """
    h = Harness(tmp_path / "drop", [review()])
    h.env["FAKE_DROP"] = str(h.project / "leftover.log")
    r = h.run()

    assert r.rc == rloop.EXIT_NEEDS_WORK, "掉个产物不该影响判定"
    log = (r.loop.root / "loop.log").read_text("utf-8")
    assert "未跟踪文件" in log and "leftover.log" in log, f"多出来的文件没被点名：\n{log[-600:]}"


def test_a_void_says_so_in_the_ledger_and_the_report(tmp_path):
    """作废和「reviewer 自己崩了」都退 1，但对调用方是两回事。

    账本里只留一句 `reviewer exit 1` 的话，事后没人分得清这一轮是怎么没的；
    而给 claude 的 prompt 正是拿「会记进本轮报告」当约束的 —— 报告里不写，
    那句话就是空的。
    """
    h = Harness(tmp_path / "void", [review()])
    h.env["FAKE_TAMPER"] = str(h.project / "app.py")
    h.env["FAKE_EVENTS"] = json.dumps(
        [{"type": "item.completed", "item": {"type": "file_change", "path": "app.py"}}])
    r = h.run()

    assert r.rc == rloop.EXIT_ERROR
    assert "exit" not in (r.state.get("outcome_reason") or ""), \
        f"作废被记成了普通的 reviewer 崩溃：{r.state.get('outcome_reason')}"
    assert "改动了被审代码" in r.state["outcome_reason"]
    assert "作废" in (r.state.get("fingerprint_note") or ""), "指纹裁决没落进账本"
    assert "作废" in rloop.render_report(r.loop), "报告里看不到工作区核对的结果"


def test_the_author_editing_during_the_round_does_not_void_it(tmp_path):
    """回归用例，来自第一次实跑时的误判。

    评审要跑好几分钟，作者在这期间接着改自己的代码是 rloop 的正常用法。第一版
    只看指纹，于是 303 秒的评审连同它跑出来的实证一起被判无效。工作区变了但
    reviewer 日志里没有动手痕迹时，只提醒，不作废。
    """
    h = Harness(tmp_path / "author", [review()])
    h.env["FAKE_TAMPER"] = str(h.project / "app.py")     # 没有事件证据 = 看着就像作者改的
    r = h.run()

    assert r.rc == rloop.EXIT_NEEDS_WORK, f"作者自己改了代码就被判成 reviewer 越界（{r.rc}）"
    assert r.state["history"][-1]["deliverable_maturity"] == 5.0, "评审结果没留下来"
    log = (r.loop.root / "loop.log").read_text("utf-8")
    assert "工作区在评审期间变过" in log and "作废" not in log


def test_no_verify_puts_the_codex_reviewer_back_behind_read_only(tmp_path):
    """`--no-verify` 是不信任送审代码时用的，它必须真落到沙箱档位上。"""
    r = Harness(tmp_path / "ro", [review()]).run("--no-verify")
    argv = r.calls[0]["argv"]

    assert argv[0] == "-s" and argv[1] == "read-only", f"--no-verify 没关掉写权限：{argv[:4]}"
    assert r.loop.state["verify"] is False, "loop.json 没记下这一档，续跑会悄悄变回放开"


def test_no_verify_takes_effect_on_a_resumed_loop_too(tmp_path):
    """自审抓到的最硬一条：`--no-verify` 在续轮时是一句静默失效的咒语。

    verify 先前只在**新建 loop** 的分支里读一次。于是「loop 还开着的时候改主意想
    收紧权限」这条路上，开关落不进 loop.json，reviewer 照旧拿 workspace-write
    起跑，终端上一个字都不说 —— 用户以为自己关掉了写权限，其实一直开着。
    """
    h = Harness(tmp_path / "resume", [review(), review()])
    r1 = h.run()
    assert r1.state["verify"] is True and r1.calls[-1]["argv"][1] == "workspace-write"

    h.write_response(r1.loop, 1)
    h.author_edits()
    r2 = h.run("--no-verify")

    assert len(r2.loops) == 1, "前提：这确实是续轮，不是另起了一个 loop"
    assert r2.state["verify"] is False, "--no-verify 没落进 loop.json"
    assert r2.calls[-1]["argv"][1] == "read-only", "沙箱没关回只读"
    log = (r2.loop.root / "loop.log").read_text("utf-8")
    assert "关回只读" in log, "档位变了却一声不吭"


def test_claude_reviewer_gets_write_tools_but_never_the_skip_flag(tmp_path):
    """claude 这边没有操作系统沙箱，唯一的边界就是 permission-mode。

    所以两头都要钉：放开时给的是 auto，不是 --dangerously-skip-permissions；
    仓库定制无论哪档都关着 —— hook 在模型说第一句话之前就跑掉了，permission-mode
    管不着它。
    """
    r = Harness(tmp_path / "rw", [review()]).run("--reviewer", "claude")
    argv = r.calls[0]["argv"]

    # `auto` 是实测出来的唯一能跑 shell 的档：acceptEdits 和 dontAsk 只自动批准
    # 文件编辑，Bash 仍要人点头，而 -p 模式下没人可问 —— reviewer 会以为自己能跑，
    # 然后在 validation_commands 里写满「被权限层拒绝」。
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert "--safe-mode" in argv, "没关掉仓库定制，hook 仍可绕过 permission-mode 执行"
    assert "--no-session-persistence" in argv
    assert "--json-schema" in argv, "结果没走结构化 stdout，等于还需要写权限"
    assert "--dangerously-skip-permissions" not in argv

    plan = Harness(tmp_path / "plan", [review()]).run("--reviewer", "claude", "--no-verify")
    ro = plan.calls[0]["argv"]
    assert ro[ro.index("--permission-mode") + 1] == "plan"
    assert "--safe-mode" in ro


def test_model_and_effort_reach_the_reviewer(tmp_path):
    r = Harness(tmp_path, [review()]).run("--reviewer-model", "o3", "--effort", "xhigh")
    argv = r.calls[0]["argv"]

    assert argv[argv.index("-m") + 1] == "o3"
    assert "model_reasoning_effort=xhigh" in argv


# ─────────────────────────── --json 载荷 ───────────────────────────


def test_progress_callback_is_actually_wired_into_the_run(tmp_path):
    """渲染函数被 run_reviewer 真的用上了，而不是只存在于单测里。

    回归用例：render_codex_event() 和 stream_subprocess(on_line=) 都写好了，
    但 run_reviewer() 那行调用没传回调，于是进度一行都不显示 —— 纯函数单测
    全绿，功能却是死的。这里从 rloop 的真实输出里找进度。
    """
    h = Harness(tmp_path, [review()])
    h.env["FAKE_EVENTS"] = json.dumps([
        {"type": "item.started",
         "item": {"id": "i1", "type": "command_execution",
                  "command": "python3 -m pytest -q", "status": "in_progress"}},
        {"type": "item.completed",
         "item": {"id": "i1", "type": "command_execution",
                  "command": "python3 -m pytest -q", "exit_code": 1, "status": "completed"}},
        {"type": "turn.completed", "usage": {"output_tokens": 42}},
    ])
    r = h.run()          # --json，所以进度走 stderr

    assert "$ python3 -m pytest -q" in r.proc.stderr, \
        f"进度没接进运行路径\n--stderr--\n{r.proc.stderr}"
    assert "exit 1" in r.proc.stderr, "失败命令的退出码没显示"
    assert "42 tokens" in r.proc.stderr

    # 原始事件流仍然全量落盘
    assert "item.started" in (r.loop.round_dir(1) / "reviewer.log").read_text("utf-8")


def test_progress_goes_to_stderr_so_json_stdout_stays_parseable(tmp_path):
    h = Harness(tmp_path, [review()])
    h.env["FAKE_EVENTS"] = json.dumps([
        {"type": "item.started",
         "item": {"id": "i1", "type": "command_execution",
                  "command": "ls -la", "status": "in_progress"}},
    ])
    r = h.run()

    assert "$ ls -la" not in r.proc.stdout, "进度混进了 stdout，JSON 就没法解析了"
    json.loads(r.proc.stdout)      # 仍是干净的单个 JSON 对象


def test_stop_signals_the_reviewer_and_writes_nothing(tmp_path):
    """stop 只发信号：reviewer 被收掉，loop.json 一个字节都不该被 stop 改。

    早先 stop 还要负责把终态写成 aborted，于是它和运行中的进程成了同一份
    状态文件的两个写者，怎么排顺序都有窗口。现在写者归零。
    """
    import threading

    h = Harness(tmp_path, [review()])
    h.env["FAKE_SLEEP"] = "6"

    out = []
    t = threading.Thread(target=lambda: out.append(h.run()))
    t.start()

    d = h.project / rloop.LOOP_DIRNAME
    root, deadline = None, time.time() + 20
    while time.time() < deadline:
        if d.is_dir():
            c = [x for x in d.iterdir() if (x / "loop.json").exists()]
            if c and json.loads((c[0] / "loop.json").read_text("utf-8")).get("child_pid"):
                root = c[0]
                break
        time.sleep(0.05)
    assert root is not None, "reviewer 一直没起来，用例没压中窗口"

    stop = subprocess.run(
        [sys.executable, str(REPO_ROOT / "rloop.py"), "stop", "-C", str(h.project)],
        capture_output=True, text=True, timeout=60, env=h.env)
    assert stop.returncode == 0, stop.stderr
    assert "已收掉" in stop.stdout, stop.stdout
    t.join(timeout=90)

    state = json.loads((root / "loop.json").read_text("utf-8"))
    assert state["status"] == "running", \
        f"stop 或运行方改写了状态（现在是 {state['status']}），应当原样停在 running"


def test_a_stopped_loop_is_taken_over_and_the_round_is_redone(tmp_path):
    """被 stop 掐掉之后再跑：接管同一个 loop，重跑那一轮，不另起炉灶。

    这正是"崩了"和"被停了"能共用一条路的原因 —— 对下一步而言两者没区别。
    """
    h = Harness(tmp_path, [review(), review()])
    r = h.run()
    r.loop.update(status="running", child_pid=None)      # 模拟被掐在半路
    (r.loop.round_dir(1) / "review.json").unlink()       # 那轮没留下可用产物

    second = h.run()
    assert len(second.loops) == 1, "另起了一个 loop，没有接管"
    assert second.state["round"] == 1, "没有退回去重跑那一轮"
    log = r.loop.log_file.read_text("utf-8")
    assert "接管" in log and "重跑该轮" in log


def test_stop_kills_the_runner_before_the_reviewer(tmp_path):
    """顺序要紧：runner 必须先死，否则它会抢在 stop 之前把这轮写成 failed。

    回归用例：早先先收 reviewer、再收 runner —— reviewer 一退出，runner 的
    p.wait() 立刻返回，一路走完 finish 写下 done/failed，等 stop 轮到杀它时
    人已经没了。于是"状态停在 running、下次接管"这个对外契约不成立。
    """
    src = (REPO_ROOT / "rloop.py").read_text(encoding="utf-8")
    head = "def cmd_stop(args"
    assert head in src, "cmd_stop 改名了，这个测试得跟着改"
    body = src[src.index(head):]
    # 切到函数体末尾，不是切到某个远处的分节注释：那样一旦有人往中间插新代码，
    # 切片会悄悄膨胀成几百行，断言还照样通过，只是不再验它本来要验的东西。
    end = min(i for i in (body.find("\ndef "), body.find("\n# ═"), body.find("\n# ─"))
              if i > 0)
    body = body[:end]
    assert body.index("runner_pid") < body.index('st.get("child_pid")'), \
        "cmd_stop 里 reviewer 排在了 runner 前面，会漏掉 finish 抢跑的窗口"


def test_recycled_pid_started_later_is_not_signalled(tmp_path):
    """PID 被一个**更晚启动**的同类进程复用时，绝不能开枪。

    回归用例：上一版用「启动时间不早于本轮」做判据，方向是反的 —— 复用者天然
    启动得更晚，一定通过那个下界；而当时的测试拿 2099 年做参考，验的是"启动
    早于参考时间被拒"，跟真实场景正相反。现在比对的是**记录下来的那个启动
    时刻是否完全一致**。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()

    # 一个命令行里带 codex 字样、但启动时刻与账本不符的进程 —— 正是复用者的样子
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)  # codex"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        real = rloop.pid_field(victim.pid, "lstart=")
        assert real, "拿不到启动时刻，用例没测到东西"

        # 账本里记的是"另一个时刻"：模拟旧进程没了、这个 pid 被别人占了
        assert rloop.pid_is_our_reviewer(victim.pid, "Sun Aug  9 00:00:00 2026") is False
        assert rloop.pid_is_our_runner(victim.pid, "Sun Aug  9 00:00:00 2026") is False
        # 而记录与实际一致时（真的是我们起的那个）应当认得出来
        assert rloop.pid_started_exactly_at(victim.pid, real) is True

        r.loop.update(child_pid=victim.pid, runner_pid=victim.pid,
                      child_started="Sun Aug  9 00:00:00 2026",
                      runner_started="Sun Aug  9 00:00:00 2026")
        stop = subprocess.run(
            [sys.executable, str(REPO_ROOT / "rloop.py"), "stop", "-C", str(h.project)],
            capture_output=True, text=True, timeout=60, env=h.env)
        assert stop.returncode == 0, stop.stderr
        time.sleep(0.4)
        assert victim.poll() is None, "复用了 pid 的无关进程被杀掉了"
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_takeover_clears_stale_pids_from_the_old_run(tmp_path):
    """接管一个 running loop 时，先把遗留的 pid 字段清干净再登记自己。

    回归用例：遗留 pid 属于一个已经不在的进程，而 pid 会被系统回收复用 ——
    留着它们，之后一次 stop 就可能照着这些号码去杀无关进程。
    """
    h = Harness(tmp_path, [review(), review(prior=RESOLVED)])
    first = h.run()
    first.loop.update(status="running", child_pid=99999, child_started="Sun Jan 1 00:00:00 2020",
                      runner_pid=99998, runner_started="Sun Jan 1 00:00:00 2020")

    h.write_response(first.loop, 1)
    h.author_edits()
    second = h.run()

    st = second.state
    assert "接管" in first.loop.log_file.read_text("utf-8")
    # 接管之后登记的必须是这一轮自己的，绝不能还是 99999/99998
    assert st.get("child_pid") != 99999
    assert st.get("runner_pid") != 99998


def test_no_recorded_start_time_means_no_signal(tmp_path):
    """旧账本没记启动时刻时，宁可不发信号也不能只凭命令行子串就开枪。"""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)  # codex"],
                              start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 命令行里带 codex 字样，但账本里没有启动时刻 —— 判定必须拒绝
        assert rloop.pid_is_our_reviewer(victim.pid, None) is False
        assert rloop.pid_is_our_runner(victim.pid, None) is False
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_same_second_loops_are_ordered_by_creation_not_by_name(tmp_path):
    """同一秒建的两个 loop：按纳秒序号选最新的，而不是按随机后缀排。"""
    h = Harness(tmp_path, [review(), review(), review()])
    a = h.run("--new")
    b = h.run("--new")
    assert len({l.root.name for l in b.loops}) == 2

    older, newer = a.loop, b.loop
    same = "2026-01-01T00:00:00Z"          # 秒级时间戳一模一样
    older.update(started_at=same, created_ns=1_000)
    newer.update(started_at=same, created_ns=2_000)

    picked = rloop.find_active_loop(h.project)
    assert picked.root.name == newer.root.name, "同秒时没有按 created_ns 选最新的"


def test_stop_on_an_idle_loop_signals_nothing(tmp_path):
    """没有进程在跑时，stop 一枪都不许开 —— 账本里的 pid 必然是陈旧的。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    assert r.state["status"] == "open"

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r.loop.update(child_pid=victim.pid)      # 陈旧 pid，且已被别人占用
        stop = subprocess.run(
            [sys.executable, str(REPO_ROOT / "rloop.py"), "stop", "-C", str(h.project)],
            capture_output=True, text=True, timeout=60, env=h.env)
        assert stop.returncode == 0, stop.stderr
        assert "没什么可停的" in stop.stdout, stop.stdout
        time.sleep(0.5)
        assert victim.poll() is None, "无辜进程被 stop 杀掉了"
    finally:
        victim.kill()
        victim.wait(timeout=10)

    assert rloop.Loop(r.loop.root).state["status"] == "open", "stop 改写了状态"


def test_json_payload_carries_what_the_caller_needs(tmp_path):
    r = Harness(tmp_path, [review()]).run()
    p = r.payload

    assert p["exit_code"] == rloop.EXIT_NEEDS_WORK
    assert p["round"] == 1 and p["outcome"] == "needs_work"
    assert p["scores"]["blocking_findings"] == 1
    assert p["findings"] and p["findings"][0]["severity"] == "high"
    assert p["fix_allowed"] is True
    for key in ("report_path", "response_path", "patch_path"):
        assert Path(p[key]).parent.exists(), f"{key} 指向一个不存在的地方"
    assert Path(p["patch_path"]).exists()


def test_pinned_scope_tells_the_caller_not_to_edit(tmp_path):
    """范围钉在历史提交上时，改工作区也进不了送审 diff —— 必须告诉调用方别改。"""
    project = make_project(tmp_path)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "c2")
    git(project, "commit", "-qm", "c3", "--allow-empty")
    target = git(project, "rev-parse", "HEAD~1").strip()

    r = Harness(tmp_path, [review()], project=project).run("--commit", target)
    assert r.payload["fix_allowed"] is False, "钉死范围还允许改，循环不可能收敛"


def test_pinned_scope_cannot_be_continued(tmp_path):
    """钉死的范围改了也进不了 diff —— 必须就地关闭，不能让调用方一直续下去。

    回归用例：早先只把 fix_allowed 设成 false，loop 却仍留在 open，于是
    can_continue 是 true，调用方会一轮轮审同一份补丁直到熔断。
    """
    project = make_project(tmp_path)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "c2")
    git(project, "commit", "-qm", "c3", "--allow-empty")
    target = git(project, "rev-parse", "HEAD~1").strip()

    h = Harness(tmp_path, [review(), review()], project=project)
    r = h.run("--commit", target)

    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    assert r.payload["fix_allowed"] is False
    assert r.payload["can_continue"] is False, "钉死范围还说能继续，调用方会空转到熔断"
    assert r.state["outcome"] == "pinned_scope"
    assert r.state["status"] == "done"

    # 再裸调一次不该续在这个 loop 上（给点新改动，免得无内容可审直接退出）
    h.author_edits()
    again = h.run()
    assert len(again.loops) == 2, "钉死的 loop 被续上了"
    assert again.state["diff_target"] is None, "新 loop 不该继承钉死的终点"


def test_no_json_flag_keeps_stdout_clean(tmp_path):
    r = Harness(tmp_path, [review()]).run(json_out=False)
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    assert not r.proc.stdout.strip().startswith("{"), "没要 --json 却吐了 JSON"


def test_a_round_leaves_a_replayable_progress_log(tmp_path):
    """跑完一轮，磁盘上留下一份完整、自洽的事件流。

    这是面板能看到进度的全部依据 —— 它不拥有 reviewer 进程，只读这个文件。
    少了 run.end 面板会永远挂在「运行中」。
    """
    r = Harness(tmp_path, [review()]).run()
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()

    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    assert pf.exists(), "跑完一轮却没有 progress.ndjson"

    evs = []
    for line in pf.read_text(encoding="utf-8").splitlines():
        assert len(line.encode()) < rloop.EVENT_LINE_BYTES, "单行超了原子写上限"
        evs.append(json.loads(line))          # 每行都得是合法 JSON

    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "run.start", f"第一条不是 run.start：{kinds[:3]}"
    assert kinds[-1] == "run.end", f"最后一条不是 run.end：{kinds[-3:]}"
    assert "score" in kinds, "没有 score 事件，面板画不出走势"
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1)), "seq 不连续"

    run_ids = {e["run"] for e in evs}
    assert len(run_ids) == 1 and None not in run_ids, f"run id 不一致：{run_ids}"
    assert all(e["loop"] == r.state["id"] for e in evs)

    end = evs[-1]["data"]
    assert end["outcome"] == "needs_work" and end["exit_code"] == rloop.EXIT_NEEDS_WORK


def test_needs_work_also_closes_the_event_stream(tmp_path):
    """未达标这条出口不走 finish()，但事件流同样要收尾。

    回归用例：needs_work 自己写状态、自己 notify、自己 return，最初漏了 run.end，
    症状是面板上这一轮永远转着圈。
    """
    r = Harness(tmp_path, [review()]).run()
    evs = [json.loads(l) for l in
           (r.loop.round_path(1) / rloop.PROGRESS_FILE).read_text(encoding="utf-8").splitlines()]
    ends = [e for e in evs if e["kind"] == "run.end"]
    assert len(ends) == 1, f"run.end 应当正好一条，实际 {len(ends)}"
    assert ends[0]["data"]["outcome"] == "needs_work"


def test_a_converged_round_closes_the_event_stream_too(tmp_path):
    """达标走的是 finish()，那条路径也得发 run.end。"""
    r = Harness(tmp_path, [PASSING]).run()
    assert r.rc == rloop.EXIT_PASS, r.fail_msg()
    evs = [json.loads(l) for l in
           (r.loop.round_path(1) / rloop.PROGRESS_FILE).read_text(encoding="utf-8").splitlines()]
    assert evs[-1]["kind"] == "run.end"
    assert evs[-1]["data"]["outcome"] == "converged"
    assert evs[-1]["data"]["exit_code"] == rloop.EXIT_PASS
