#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rloop 的真实依赖验证。**默认不跑。**

`tests/test_rloop.py` 的规矩是绝不起真实 agent；这个文件正好相反——它存在的
唯一理由就是去碰真东西：真的子进程、真的 SIGKILL、真的 claude / codex。两者
用 `integration` marker 分开，`pytest.ini` 里 `addopts = -m "not integration"`，
所以 `python3 -m pytest` 永远不会误跑到这里。

三档，代价递增，都要显式授权：

    python3 -m pytest -m integration                     # 第 1、2 档
    RLOOP_E2E=1 python3 -m pytest -m integration         # 加上第 3 档

1. 依赖探活    `claude --version` / `codex --version`，几百毫秒，不花配额。
2. 进程生命周期 用真实子进程打 `stream_subprocess` 的流式、超时、SIGKILL、
                可执行文件缺失四条路径。几秒，不花配额。
3. 端到端      在临时 git 仓库里真的跑一遍 `rloop`，reviewer
                都是真模型。**要花两个模型的配额**，所以额外用 `RLOOP_E2E=1` 门控。

缺依赖时 skip 而不是 fail：这套东西是给能连上模型的机器跑的，CI 里没有
claude / codex 不该被判成红。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402

pytestmark = pytest.mark.integration


# ─────────────────────────── 夹具 ───────────────────────────


def make_live_loop(tmp_path: Path, timeout: int = 600) -> rloop.Loop:
    """一个能被 stream_subprocess 正常读写的 Loop：state 与 log 都落在 tmp_path。"""
    root = tmp_path / "loop"
    root.mkdir(parents=True, exist_ok=True)
    loop = rloop.Loop(root)
    loop.save({
        "id": "itest",
        "project": str(tmp_path),
        "timeout": timeout,
        "round": 0,
        "history": [],
        "status": "running",
        "child_pid": None,
    })
    return loop


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        pytest.skip(f"本机没有 {binary}，跳过真实依赖验证")
    return path


# ────────────────────── 第 1 档：依赖探活 ──────────────────────


@pytest.mark.parametrize("binary", ["claude", "codex"])
def test_agent_cli_is_installed_and_responds(binary):
    """rloop 全部能力都架在这两个 CLI 上，先证明它们真的在、真的能跑。"""
    exe = require(binary)
    r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip(), "--version 没有输出"


def test_git_is_available():
    """determine_scope 的范围判定、base_sha、每轮 diff 全靠 git。"""
    exe = require("git")
    r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("git version")


# ─────────────────── 第 2 档：子进程生命周期 ───────────────────


def test_stream_subprocess_streams_to_log_and_clears_child_pid(tmp_path):
    """正常路径：真的起进程，输出进日志，跑完把 child_pid 抹回 None。"""
    loop = make_live_loop(tmp_path)
    logfile = tmp_path / "child.log"

    rc = rloop.stream_subprocess(
        loop,
        [sys.executable, "-u", "-c", "print('第一行'); print('第二行')"],
        tmp_path, logfile, timeout=60,
    )

    assert rc == 0
    assert logfile.read_text(encoding="utf-8").splitlines() == ["第一行", "第二行"]
    assert loop.state["child_pid"] is None
    assert "退出码=0" in loop.log_file.read_text(encoding="utf-8")


def test_stream_subprocess_propagates_nonzero_exit(tmp_path):
    """agent 非零退出要原样传回来——drive() 靠它判定这一轮失败。"""
    loop = make_live_loop(tmp_path)
    rc = rloop.stream_subprocess(
        loop, [sys.executable, "-c", "raise SystemExit(3)"],
        tmp_path, tmp_path / "child.log", timeout=60,
    )
    assert rc == 3


def test_stream_subprocess_reports_missing_executable(tmp_path):
    """PATH 里没有 claude/codex 时返回 127，而不是把 FileNotFoundError 抛给 drive()。"""
    loop = make_live_loop(tmp_path)
    rc = rloop.stream_subprocess(
        loop, ["rloop-no-such-binary-4b1f", "--version"],
        tmp_path, tmp_path / "child.log", timeout=60,
    )
    assert rc == 127
    assert "找不到可执行文件" in loop.log_file.read_text(encoding="utf-8")


def test_stream_subprocess_timeout_really_kills_a_chatty_child(tmp_path):
    """超时清理：真的 SIGKILL，且证明子进程之后不再动。

    子进程一边往 stdout 打字一边给心跳文件追加字节。stream_subprocess 超时后
    先看返回码和日志，再隔一段时间比对心跳文件大小——没长说明进程是真死了，
    而不是只是被丢下不管。
    """
    loop = make_live_loop(tmp_path)
    heartbeat = tmp_path / "heartbeat"
    script = write_script(tmp_path, "chatty.py", (
        "import sys, time\n"
        "hb = sys.argv[1]\n"
        "while True:\n"
        "    open(hb, 'a').write('x')\n"
        "    print('tick', flush=True)\n"
        "    time.sleep(0.05)\n"
    ))

    start = time.time()
    rc = rloop.stream_subprocess(
        loop, [sys.executable, "-u", str(script), str(heartbeat)],
        tmp_path, tmp_path / "child.log", timeout=1,
    )
    elapsed = time.time() - start

    assert rc == -1
    assert elapsed < 15, f"超时判定拖了 {elapsed:.1f}s，远超 timeout=1"
    assert "超时 1s，已终止" in loop.log_file.read_text(encoding="utf-8")
    assert loop.state["child_pid"] is None

    size_at_kill = heartbeat.stat().st_size
    time.sleep(1.0)
    assert heartbeat.stat().st_size == size_at_kill, "子进程在超时之后还在写，没被真正杀掉"


def test_runner_reaps_its_reviewer_when_sigtermed(tmp_path):
    """runner 被 SIGTERM 时要顺手收掉自己起的 reviewer，不留孤儿。

    补的是 Popen 已返回、child_pid 还没写进账本的那个窗口 —— 那一瞬 stop 只
    看得到 runner，杀完它 reviewer 就没人管了。runner 手里一直握着 Popen
    对象，由它兜底不依赖账本。
    """
    heartbeat = tmp_path / "reviewer-heartbeat"
    child = write_script(tmp_path, "fake_reviewer.py", (
        "import sys, time\n"
        "hb = sys.argv[1]\n"
        "while True:\n"
        "    open(hb, 'a').write('x')\n"
        "    time.sleep(0.05)\n"
    ))
    # 一个最小的 runner：装上 rloop 的 handler，起子进程，然后干等
    runner = write_script(tmp_path, "fake_runner.py", (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import contextlib, os, signal, subprocess, time\n"
        "import rloop\n"
        "p = subprocess.Popen([sys.executable, '-u', sys.argv[1], sys.argv[2]],\n"
        "                     start_new_session=True,\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "rloop._ACTIVE_CHILD = p\n"
        "signal.signal(signal.SIGTERM, rloop._terminate_with_child)\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    ))

    r = subprocess.Popen([sys.executable, "-u", str(runner), str(child), str(heartbeat)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert r.stdout.readline().strip() == "ready"
    deadline = time.time() + 10
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert heartbeat.exists(), "reviewer 没起来，用例没测到东西"

    r.terminate()                       # 就像 rloop stop 做的那样
    r.wait(timeout=10)
    time.sleep(0.5)

    size = heartbeat.stat().st_size
    time.sleep(1.0)
    assert heartbeat.stat().st_size == size, "runner 死了，reviewer 还在写 —— 成孤儿了"


def test_sigterm_escalates_to_sigkill_for_stubborn_descendants(tmp_path):
    """后代忽略 SIGTERM 时必须升级到 SIGKILL，而不是看直接子进程退了就收手。

    回归用例：早先 kill_process_group 在 p.wait() 一返回就 break —— 直接子进程
    先退出的话，忽略 TERM 的孙进程根本等不到那一发 SIGKILL。stop 与
    KeyboardInterrupt 走的都是 TERM 起手这条路。
    """
    heartbeat = tmp_path / "stubborn-heartbeat"
    script = write_script(tmp_path, "stubborn.py", (
        "import signal, subprocess, sys, time\n"
        "hb = sys.argv[1]\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "    \"import signal,sys,time\\n\"\n"
        "    \"signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n\"\n"
        "    \"hb=sys.argv[1]\\n\"\n"
        "    \"while True:\\n open(hb,'a').write('x')\\n time.sleep(0.05)\",\n"
        "    hb])\n"
        "print('spawned', flush=True)\n"
        "time.sleep(0.3)\n"                      # 直接子进程先退，孙进程赖着
    ))

    p = subprocess.Popen([sys.executable, "-u", str(script), str(heartbeat)],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert heartbeat.exists(), "顽固孙进程没起来，用例没测到东西"
    time.sleep(0.6)                              # 让直接子进程先退出

    # start_new_session=True 时 p.pid 本身就是组长 pid；此刻直接子进程可能已经
    # 退出，再去 getpgid 会 ProcessLookupError，但组还在（孙进程赖着）。
    assert rloop.kill_pgid(p.pid, first=signal.SIGTERM, grace=3.0) is True

    size = heartbeat.stat().st_size
    time.sleep(1.0)
    assert heartbeat.stat().st_size == size, "忽略 TERM 的孙进程还在写，没升级到 KILL"


def test_sigterm_reaches_the_runner_while_the_reviewer_is_running(tmp_path):
    """review 跑着的时候，发给 runner 的 SIGTERM 必须立刻送达。

    回归用例：把清理收进单一 finally 那次，顺手把「登记完成后立刻解除屏蔽」
    弄丢了 —— 屏蔽于是盖住整个 p.wait()，最长 2400 秒。stop、关机、手工 kill
    发来的信号都要等 reviewer 自己退出才生效，_terminate_with_child 形同虚设。
    屏蔽只该盖住「子进程已存在但还没登记」那一小段。
    """
    import signal as sig
    loop = make_live_loop(tmp_path)
    got: list[float] = []
    old = sig.signal(sig.SIGTERM, lambda *a: got.append(time.time()))
    try:
        t0 = time.time()
        threading.Timer(0.6, lambda: os.kill(os.getpid(), sig.SIGTERM)).start()
        rloop.stream_subprocess(
            loop, [sys.executable, "-c", "import time; time.sleep(4)"],
            tmp_path, tmp_path / "child.log", timeout=30)
    finally:
        sig.signal(sig.SIGTERM, old)

    assert got, "SIGTERM 一直没送达"
    delay = got[0] - t0
    assert delay < 2.0, (
        f"信号等了 {delay:.1f}s 才送达 —— 屏蔽盖住了整个 wait，"
        f"stop 得等 reviewer 自己退出才生效")


def test_bookkeeping_failure_leaves_no_mask_and_no_orphan(tmp_path):
    """账本写失败这条异常路径也要清干净：掩码恢复、全局句柄清空、子进程收掉。

    回归用例：早先清理是分散在 FileNotFoundError / 超时各个分支里手工做的，
    loop.update() 抛异常时全都绕过 —— 结果 SIGTERM 一直被屏蔽、_ACTIVE_CHILD
    指着一个没人管的子进程、那个子进程还在跑。
    """
    import signal as sig
    loop = make_live_loop(tmp_path)
    heartbeat = tmp_path / "hb"
    script = write_script(tmp_path, "child.py", (
        "import sys, time\n"
        "hb = sys.argv[1]\n"
        "while True:\n"
        "    open(hb, 'a').write('x')\n"
        "    time.sleep(0.05)\n"
    ))

    def boom(**kw):
        if "child_pid" in kw and kw.get("child_pid"):
            raise OSError("模拟账本写失败")
        return None
    loop.update = boom                      # type: ignore[method-assign]

    before = sig.pthread_sigmask(sig.SIG_BLOCK, set())
    with pytest.raises(OSError, match="模拟账本写失败"):
        rloop.stream_subprocess(loop, [sys.executable, "-u", str(script), str(heartbeat)],
                                tmp_path, tmp_path / "child.log", timeout=30)

    assert sig.pthread_sigmask(sig.SIG_BLOCK, set()) == before, "SIGTERM 掩码没恢复"
    assert rloop._ACTIVE_CHILD is None, "_ACTIVE_CHILD 泄漏了"

    deadline = time.time() + 5
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.05)
    if heartbeat.exists():
        size = heartbeat.stat().st_size
        time.sleep(1.0)
        assert heartbeat.stat().st_size == size, "子进程没被收掉，还在跑"


def test_timeout_clears_the_active_child_handle(tmp_path):
    """超时路径同样不能留下全局句柄。"""
    loop = make_live_loop(tmp_path)
    rc = rloop.stream_subprocess(
        loop, [sys.executable, "-c", "import time; time.sleep(20)"],
        tmp_path, tmp_path / "child.log", timeout=1)
    assert rc == -1
    assert rloop._ACTIVE_CHILD is None


def test_timeout_kills_grandchildren_too(tmp_path):
    """超时要连 reviewer 派生出来的后代一起收掉。

    回归用例：早先只对 Popen 的直接子进程发信号，而 codex reviewer 会 fork
    shell 去跑 pytest、grep 之类 —— 父进程被杀之后那些孙进程变成孤儿继续跑，
    超时"成功"返回了，机器上却还有东西在吃 CPU。现在子进程用
    start_new_session=True 起，超时走 killpg。
    """
    loop = make_live_loop(tmp_path)
    heartbeat = tmp_path / "grandchild-heartbeat"
    # 父进程立刻起一个孙进程写心跳，然后自己安静地睡 —— 只杀父进程的话，
    # 心跳会一直涨下去。
    parent = write_script(tmp_path, "parent.py", (
        "import subprocess, sys, time\n"
        "hb = sys.argv[1]\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "    \"import sys,time\\nhb=sys.argv[1]\\nwhile True:\\n open(hb,'a').write('x')\\n time.sleep(0.05)\",\n"
        "    hb])\n"
        "print('spawned', flush=True)\n"
        "time.sleep(300)\n"
    ))

    rc = rloop.stream_subprocess(
        loop, [sys.executable, "-u", str(parent), str(heartbeat)],
        tmp_path, tmp_path / "child.log", timeout=2,
    )
    assert rc == -1

    deadline = time.time() + 5
    while not heartbeat.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert heartbeat.exists(), "孙进程根本没起来，用例没测到东西"

    size = heartbeat.stat().st_size
    time.sleep(1.5)
    assert heartbeat.stat().st_size == size, \
        "孙进程在父进程被杀之后还在写 —— 变成孤儿了"


def test_stream_subprocess_timeout_kills_a_silent_child(tmp_path):
    """不出声的子进程也该在 timeout 后被杀掉。

    回归用例。早先超时判定写在 `for line in p.stdout` 循环体内，读会一直阻塞，
    超时根本轮不到执行；子进程自己跑完退出后 p.wait 立刻返回，超时被完全绕过，
    返回码还是 0。现在读 stdout 交给守护线程，超时由 p.wait(timeout=) 判定。
    """
    loop = make_live_loop(tmp_path)
    rc = rloop.stream_subprocess(
        loop, [sys.executable, "-c", "import time; time.sleep(3)"],
        tmp_path, tmp_path / "child.log", timeout=1,
    )
    assert rc == -1


# ───────────────── 第 3 档：真实双模型端到端 ─────────────────


def loop_roots(project: Path) -> list[Path]:
    """项目下所有 loop 目录。

    不能直接 iterdir —— `.review-loops/` 里除了 loop 还躺着 `.project.lock`，
    数进去就变成「凭空多出一个 loop」。认 loop.json 才作数。
    """
    d = project / rloop.LOOP_DIRNAME
    return sorted(x for x in d.iterdir() if (x / "loop.json").exists())


E2E_REASON = "端到端会真的调用 claude 与 codex 并消耗配额，需要 RLOOP_E2E=1 显式授权"


@pytest.mark.skipif(os.environ.get("RLOOP_E2E") != "1", reason=E2E_REASON)
def test_full_loop_against_real_agents(tmp_path):
    """在临时仓库里真的跑一轮 `rloop`：reviewer 是真模型。

    模拟真实用法——工作区里先有一坨未提交的改动，然后零参数起 loop。
    只断言链路走通了、产物齐全、reviewer 的 JSON 能被 load_review 吃下去，
    不断言分数高低——那是模型的判断，不该拿来当测试的稳定性依据。

    `-n 1` 意味着第一轮 review 之后就收摊，fixer 不会被叫起来（最后一轮跑 fixer
    没人审，白烧配额），所以这条用例覆盖的是 review 链路，不含 fixer 链路。
    """
    require("codex")

    project = tmp_path / "repo"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)

    # 模拟「刚 vibe 完」：工作区里留一坨未提交的改动等着被审
    (project / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    (project / "util.py").write_text(
        "def divide(a, b):\n    return a / b\n", encoding="utf-8")

    env = dict(os.environ, RLOOP_HOME=str(tmp_path / "rloop-home"))
    timeout = int(os.environ.get("RLOOP_E2E_TIMEOUT", "900"))
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "rloop.py"),
         "-C", str(project), "-n", "1", "-t", str(timeout),
         "--notify", "none", "--reviewer-effort", "low"],
        capture_output=True, text=True, timeout=timeout + 120, env=env,
    )
    # -n 1 跑满一轮未达标 → exhausted → 退出码 2。0 是达标，两者都算链路通。
    assert r.returncode in (0, 2), f"rloop 异常退出 {r.returncode}\n{r.stdout}\n{r.stderr}"

    roots = loop_roots(project)
    assert len(roots) == 1
    loop = rloop.Loop(roots[0])
    state = loop.state

    assert state["status"] == "done"
    assert state["outcome"] in ("converged", "exhausted", "stalled")
    assert len(state["history"]) == 1, "reviewer 没有产出可记账的分数"
    assert state["diff_base"] and state["scope_desc"], "范围没被记下来"
    assert state["diff_target"] is None, "零参数流程的 diff 终点应当是工作树"

    rd = loop.round_dir(1)
    for name in ("diff.patch", "review-prompt.md", "review.json", "reviewer.log"):
        assert (rd / name).exists(), f"缺少产物 {name}"

    review = rloop.load_review(loop, 1)
    assert review is not None, "reviewer 的真实输出没能通过 load_review"
    assert isinstance(float(review["deliverable_maturity"]), float)
    assert rloop.gate_pass(review, state["min_score"]) == (state["outcome"] == "converged")

    assert (loop.root / "report.md").exists()
    assert (rd / "diff.patch").read_text(encoding="utf-8").strip(), \
        "送审的 diff 是空的，reviewer 等于什么都没看到"

    registry = json.loads((tmp_path / "rloop-home" / "registry.json").read_text(encoding="utf-8"))
    assert state["id"] in registry


@pytest.mark.skipif(os.environ.get("RLOOP_E2E") != "1", reason=E2E_REASON)
def test_two_rounds_continue_the_same_loop_with_a_real_reviewer(tmp_path):
    """两轮真链路：真 reviewer 打分 → 作者改代码写回应 → 同一个 loop 续第 2 轮。

    上一条用 `-n 1`，只证明了单轮。这条模拟调用方驱动循环：第一次跑完拿到 findings，
    测试自己扮演开发会话去改工作区、写 response.md，再跑一次 rloop，断言它接在同一个
    loop 上、轮次递增、第 2 轮的 context pack 确实带上了上轮 findings 与那份回应。
    """
    require("codex")

    project = tmp_path / "repo"
    project.mkdir()
    (project / "hello.txt").write_text("hello\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)

    # 一坨有真问题的未提交改动：除零没防、路径没校验、异常被吞
    (project / "util.py").write_text(
        "import os\n\n\n"
        "def divide(a, b):\n    return a / b\n\n\n"
        "def read_config(name):\n"
        "    try:\n"
        "        return open(os.path.join('/etc/app', name)).read()\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8")

    env = dict(os.environ, RLOOP_HOME=str(tmp_path / "rloop-home"))
    timeout = int(os.environ.get("RLOOP_E2E_TIMEOUT", "900"))
    base = [sys.executable, str(REPO_ROOT / "rloop.py"), "-C", str(project),
            "-n", "3", "-m", "9.5", "-t", str(timeout),
            "--notify", "none", "--effort", "low", "--json"]

    first = subprocess.run(base, capture_output=True, text=True,
                           timeout=timeout + 120, env=env)
    assert first.returncode == rloop.EXIT_NEEDS_WORK, (
        f"门槛 9.5 下第一轮就达标了？rc={first.returncode}\n{first.stdout}\n{first.stderr}")
    payload = json.loads(first.stdout)
    assert payload["round"] == 1 and payload["can_continue"] is True
    assert payload["findings"], "真 reviewer 一条 findings 都没给"

    # 扮演开发会话：改代码 + 写逐条回应
    (project / "util.py").write_text(
        "import os\n\n"
        "CONFIG_DIR = '/etc/app'\n\n\n"
        "def divide(a, b):\n"
        "    if b == 0:\n        raise ValueError('b 不能为 0')\n"
        "    return a / b\n\n\n"
        "def read_config(name):\n"
        "    path = os.path.realpath(os.path.join(CONFIG_DIR, name))\n"
        "    if not path.startswith(CONFIG_DIR + os.sep):\n"
        "        raise ValueError(f'路径越界: {name}')\n"
        "    with open(path) as f:\n        return f.read()\n",
        encoding="utf-8")
    Path(payload["response_path"]).write_text(
        "# 第 1 轮回应\n\n除零已加显式校验；路径遍历已用 realpath 收敛到 CONFIG_DIR 之下；"
        "异常不再吞掉，改为抛 ValueError。\n", encoding="utf-8")

    second = subprocess.run(base, capture_output=True, text=True,
                            timeout=timeout + 120, env=env)
    assert second.returncode in (rloop.EXIT_PASS, rloop.EXIT_NEEDS_WORK), (
        f"第二轮异常退出 {second.returncode}\n{second.stdout}\n{second.stderr}")
    p2 = json.loads(second.stdout)

    roots = loop_roots(project)
    assert len(roots) == 1, "第二次跑另起了 loop，没有续上"
    assert p2["round"] == 2 and p2["loop_id"] == payload["loop_id"]

    loop = rloop.Loop(roots[0])
    pack = (loop.round_dir(2) / "review-prompt.md").read_text(encoding="utf-8")
    assert "prior_findings_status" in pack, "没要求 reviewer 对上轮逐条裁决"
    assert "第 1 轮回应" in pack, "作者的回应没进第 2 轮的 context pack"
    assert p2["prior_findings_status"], "真 reviewer 没有对上轮 findings 给出裁决"

    assert [e["round"] for e in loop.state["history"]] == [1, 2]
