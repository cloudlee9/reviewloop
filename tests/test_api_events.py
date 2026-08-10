#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`rloop api events` —— 面板拿实时进度的唯一通道。

这条流取代了网页面板此前那一整套自制机制：`Last-Event-ID` 裁剪补发、全局 `_seq`、
`gen` 代次、`_running["lines"]` 无上限内存缓冲、`lines[-200:]` 的补发窗口，
以及那条「所有调用点都在 `_run_lock` 内」的无断言不变量。它们解决的问题在这里
是一个 `--since` 参数。

所以这个文件里的每一条，都是那些机制曾经出过的错。
"""

from __future__ import annotations

import json
import os
import signal
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
from test_fake_agents import Harness, review  # noqa: E402


def events(*argv, env=None, timeout=60) -> list[dict]:
    """跑一次 events，把 NDJSON 解析成事件列表。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "rloop.py"), "api", "--api", "1", "events", *argv],
        capture_output=True, text=True, timeout=timeout, env=env)
    assert r.returncode == 0, f"events 退出码 {r.returncode}\n{r.stderr}"
    out = []
    for line in r.stdout.splitlines():
        if line.strip():
            out.append(json.loads(line))     # 每行都必须是合法 JSON
    return out


@pytest.fixture
def done_round(tmp_path):
    """一个跑完一轮、留下完整进度的 loop。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    return h, r


def test_it_replays_the_whole_round(done_round):
    h, r = done_round
    evs = events(r.state["id"], env=h.env)
    assert evs[0]["kind"] == "run.start"
    assert evs[-1]["kind"] == "run.end"
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))


def test_since_gives_you_only_what_you_have_not_seen(done_round):
    """重连不重复 —— 这是网页面板栽过两次的地方。

    第一次是断线重连时无条件补发最近 200 行，前端把它们追加第二次；
    修完之后又发现历史帧没有 id，客户端根本没法说清自己看到哪儿了。
    """
    h, r = done_round
    everything = events(r.state["id"], env=h.env)
    assert len(everything) >= 4

    cut = everything[2]["seq"]
    rest = events(r.state["id"], "--since", str(cut), env=h.env)
    assert [e["seq"] for e in rest] == [e["seq"] for e in everything if e["seq"] > cut]
    seen = {e["seq"] for e in everything[:3]}
    assert not ({e["seq"] for e in rest} & seen), "把调用方已经见过的事件又补了一遍"


def test_since_past_the_end_yields_nothing(done_round):
    h, r = done_round
    assert events(r.state["id"], "--since", "99999", env=h.env) == []


def test_a_truncated_history_is_announced_not_papered_over(done_round):
    """`--since` 落在被裁掉的区间时要发 gap。

    不发的话，面板显示的是一段看似连续、实则中间有洞的进度 —— 比明说少了一段更糟。
    """
    h, r = done_round
    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    kept = pf.read_text(encoding="utf-8").splitlines()[3:]   # 砍掉开头几条
    pf.write_text("\n".join(kept) + "\n", encoding="utf-8")

    evs = events(r.state["id"], "--since", "0", env=h.env)
    assert evs[0]["kind"] == "gap", f"没有告知缺口，第一条是 {evs[0]['kind']}"
    assert evs[0]["seq"] is None, "合成事件不该占用序列号"
    assert evs[0]["data"]["from"] == 1 and evs[0]["data"]["to"] >= 1
    assert evs[0]["level"] == "warn"


def test_state_sends_every_loop_on_the_first_scan(done_round):
    """面板的初始列表也从这条流里来，不用另外调 api loops。"""
    h, r = done_round
    evs = events("--state", "--project", str(h.project), env=h.env)
    states = [e for e in evs if e["kind"] == "state"]
    assert states, "--state 第一次扫描应当把全部 loop 发一遍"
    ids = {e["data"]["loop"]["id"] for e in states}
    assert r.state["id"] in ids
    one = states[0]["data"]["loop"]
    assert one["etag"] and "state" not in one, "state 事件里带的应是投影，不是原始状态"


def test_corrupt_lines_are_skipped_not_fatal(done_round):
    """契约明确要求读者跳过解析失败的行。写者崩在半行上也不能拖垮读者。"""
    h, r = done_round
    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    with pf.open("a", encoding="utf-8") as f:
        f.write('{"seq":9999, 这是半行\n')
    evs = events(r.state["id"], env=h.env)
    assert evs and evs[-1]["kind"] == "run.end"


def test_a_half_written_line_is_left_for_next_time(tmp_path):
    """没有换行符的尾巴是写者还没写完，这次别读。"""
    (tmp_path / "round-01").mkdir()
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    p.write_text('{"seq":1,"kind":"note"}\n{"seq":2,"kind":"no', encoding="utf-8")
    lines, offset, top = rloop.read_progress_since(p, 0)
    assert len(lines) == 1 and top == 1
    # 写者补完之后，接着读得到
    with p.open("a", encoding="utf-8") as f:
        f.write('te"}\n')
    more, offset, top = rloop.read_progress_since(p, top, offset)
    assert len(more) == 1 and top == 2


@pytest.mark.slow
def test_following_a_live_round_streams_and_then_stops(tmp_path):
    """跟一轮正在跑的 review：事件实时到达，run.end 之后自己退出。

    这是整个切分的关键 —— 面板**不拥有** reviewer 进程，却能看到全部进度。
    """
    h = Harness(tmp_path, [review()])
    # 先起一轮，拿到 loop id 之后再跟；fake reviewer 很快，所以这里跟的是回放，
    # 真正要验的是「follower 见到 run.end 会自己收工」，不会永远挂着。
    r = h.run()
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()

    started = time.monotonic()
    evs = events(r.state["id"], "--follow", env=h.env, timeout=60)
    took = time.monotonic() - started
    assert evs[-1]["kind"] == "run.end"
    assert took < 30, f"见到 run.end 之后没收工，跟了 {took:.1f}s"


@pytest.mark.slow
def test_an_orphaned_runner_does_not_hang_the_follower(tmp_path):
    """runner 被 SIGKILL 时没人写 run.end，follower 必须自己了断。

    不做这一条，面板那边的连接永远不释放 —— 用户看到的是一轮永远在转圈。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    lp = r.loop
    st = lp.state
    # 伪装成「有个 runner 正在跑」，但那个 pid 早就不存在了
    st["status"] = "running"
    st["runner_pid"] = 999999
    st["runner_started"] = "Mon Jan  1 00:00:00 2001"
    lp.save(st)
    pf = lp.round_path(1) / rloop.PROGRESS_FILE
    kept = [l for l in pf.read_text(encoding="utf-8").splitlines()
            if json.loads(l).get("kind") != "run.end"]
    pf.write_text("\n".join(kept) + "\n", encoding="utf-8")

    started = time.monotonic()
    evs = events(r.state["id"], "--follow", env=h.env, timeout=60)
    took = time.monotonic() - started
    assert took < 30, f"follower 挂住了，跟了 {took:.1f}s"
    assert evs[-1]["kind"] == "run.end"
    assert evs[-1]["data"]["outcome"] == "orphaned"
    assert evs[-1]["seq"] is None, "合成的收尾事件不该占序列号"


@pytest.mark.slow
def test_idle_timeout_ends_a_state_follower(done_round):
    """--state 的 follower 靠空闲超时收工；心跳不能把这个计时器一直顶住。

    回归用例：心跳本身也是发出去的事件，拿它重置空闲计时的话，任何大于心跳
    间隔的 --idle-timeout 都永远等不到。
    """
    h, r = done_round
    started = time.monotonic()
    evs = events("--state", "--follow", "--idle-timeout", "20",
                 "--project", str(h.project), env=h.env, timeout=90)
    took = time.monotonic() - started
    assert 18 < took < 45, f"空闲超时没按 20 秒生效，实际 {took:.1f}s"
    assert any(e["kind"] == "heartbeat" for e in evs), "20 秒里一次心跳都没发"


def test_a_gap_in_the_middle_is_reported_too(done_round):
    """序列中间的洞也要报，不是只看第一条。

    回归用例：gap 检查只比对回放的第一条与 `since+1`，于是「1、坏行、3」这种
    第一条仍是 1，缺口被完全放过 —— 下游拿到一段看着连续、实则丢了东西的进度。
    中间跳号是真会发生的：坏行被跳过、写盘失败（emit 里 seq 已经加过才失败）、
    文件被外部截断。
    """
    h, r = done_round
    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    lines = pf.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 6
    # 挖掉中间两条，首尾都留着
    del lines[2:4]
    pf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    evs = events(r.state["id"], env=h.env)
    gaps = [e for e in evs if e["kind"] == "gap"]
    assert gaps, "中间挖掉两条却一声不吭"
    assert gaps[0]["seq"] is None
    assert gaps[0]["data"]["from"] == 3 and gaps[0]["data"]["to"] == 4
    assert evs[0]["kind"] != "gap", "开头没缺，不该在最前面报缺口"


def test_a_corrupt_line_in_the_middle_shows_up_as_a_gap(done_round):
    """坏行造成的洞同样要报出来。"""
    h, r = done_round
    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    lines = pf.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"seq":3, 这行坏了'
    pf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    evs = events(r.state["id"], env=h.env)
    gaps = [e for e in evs if e["kind"] == "gap"]
    assert gaps and gaps[0]["data"]["from"] == 3 and gaps[0]["data"]["to"] == 3


def test_duplicate_or_backwards_sequence_numbers_do_not_invent_gaps(tmp_path):
    """序号重复或倒退时跳过它，别据此编出一个缺口来。"""
    (tmp_path / "round-01").mkdir()
    p = tmp_path / "round-01" / rloop.PROGRESS_FILE
    p.write_text('{"seq":1,"kind":"note"}\n{"seq":1,"kind":"note"}\n'
                 '{"seq":2,"kind":"note"}\n{"seq":2,"kind":"note"}\n'
                 '{"seq":3,"kind":"note"}\n', encoding="utf-8")
    lines, offset, top = rloop.read_progress_since(p, 0)
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "gap" not in kinds, f"重复序号被当成缺口了：{kinds}"
    assert [json.loads(l)["seq"] for l in lines] == [1, 2, 3]


@pytest.mark.slow
def test_a_finished_round_without_a_closing_event_does_not_hang(tmp_path):
    """账本已经进终态、但文件里没有收尾事件时，follower 也要能收工。

    回归用例：兜底只查「账本说 running 但进程没了」这一种。进度写盘失败、
    撞了上限、或者文件被外部截断时，账本是 open/done 而文件里没有 run.end ——
    那条路径下 follower 会永远挂着，面板的连接跟着不释放。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    assert r.rc == rloop.EXIT_NEEDS_WORK, r.fail_msg()
    assert r.state["status"] == "open", "这条用例要的是终态账本"

    pf = r.loop.round_path(1) / rloop.PROGRESS_FILE
    kept = [l for l in pf.read_text(encoding="utf-8").splitlines()
            if json.loads(l).get("kind") != "run.end"]
    pf.write_text("\n".join(kept) + "\n", encoding="utf-8")

    started = time.monotonic()
    evs = events(r.state["id"], "--follow", env=h.env, timeout=60)
    took = time.monotonic() - started
    assert took < 30, f"follower 挂住了，跟了 {took:.1f}s"
    assert evs[-1]["kind"] == "run.end"
    assert evs[-1]["seq"] is None, "合成的收尾事件不该占序列号"
    assert "没有收尾记录" in evs[-1]["text"]


@pytest.mark.slow
def test_reconnecting_past_the_end_does_not_fake_a_closing_event(done_round):
    """带 --since 越过收尾事件重连时，不许合成一条假的「没有收尾记录」。

    回归用例（浏览器实测抓到的，单元测试当时漏了）：兜底判据写成「这次读到的
    行里有没有 run.end」，而面板重连时 `--since` 会跳过全部历史 —— 收尾事件
    明明在文件里、面板也早见过了，follower 却每 2 秒合成一条假的收尾，
    面板那边被反复拉回这个 loop。
    """
    h, r = done_round
    evs = events(r.state["id"], env=h.env)
    top = max(e["seq"] for e in evs if isinstance(e["seq"], int))

    started = time.monotonic()
    again = events(r.state["id"], "--since", str(top), "--follow",
                   env=h.env, timeout=60)
    took = time.monotonic() - started
    assert took < 30, f"没收工，跟了 {took:.1f}s"
    fake = [e for e in again if e["kind"] == "run.end"]
    assert not fake, f"给一个正常收尾的轮次合成了假的收尾事件：{fake}"


@pytest.mark.slow
def test_a_state_follower_keeps_going_after_the_round_ends(done_round):
    """带 --state 的订阅是长活的：那一轮收尾了也不能退出。

    回归用例：三处收尾路径都无条件 `return 0`，没看 --state。而网页面板的
    follower 全都带 --state —— 核心一退出，浏览器的 EventSource 立刻重连，
    变成「每两秒起一个 follower 子进程 + 全量扫一遍状态」的空转，
    与 README 承诺的单条长活订阅正好相反。
    """
    h, r = done_round
    evs = events(r.state["id"], "--state", "--follow", "--idle-timeout", "20",
                 "--project", str(h.project), env=h.env, timeout=90)
    # 撑到空闲超时才退，而不是见到 run.end 就走
    assert any(e["kind"] == "heartbeat" for e in evs), \
        "带 --state 却在那一轮收尾时就退出了，没能活到发心跳"


@pytest.mark.slow
def test_a_state_follower_survives_an_orphaned_round(tmp_path):
    """孤儿那条路径同样要遵守 --state 的长活规则。"""
    h = Harness(tmp_path, [review()])
    r = h.run()
    lp = r.loop
    st = lp.state
    st["status"] = "running"
    st["runner_pid"] = 999999
    st["runner_started"] = "Mon Jan  1 00:00:00 2001"
    lp.save(st)
    pf = lp.round_path(1) / rloop.PROGRESS_FILE
    kept = [l for l in pf.read_text(encoding="utf-8").splitlines()
            if json.loads(l).get("kind") != "run.end"]
    pf.write_text("\n".join(kept) + "\n", encoding="utf-8")

    evs = events(r.state["id"], "--state", "--follow", "--idle-timeout", "20",
                 "--project", str(h.project), env=h.env, timeout=90)
    ends = [e for e in evs if e["kind"] == "run.end"]
    assert len(ends) == 1, f"孤儿收尾应当只发一次，实际 {len(ends)}"
    assert ends[0]["data"]["outcome"] == "orphaned"
    assert any(e["kind"] == "heartbeat" for e in evs), "发完孤儿收尾就退出了"


def test_without_state_a_finished_round_still_ends_the_follower(done_round):
    """反过来别矫枉过正：没有 --state 时该退还是要退。"""
    h, r = done_round
    started = time.monotonic()
    evs = events(r.state["id"], "--follow", env=h.env, timeout=60)
    assert time.monotonic() - started < 30
    assert evs[-1]["kind"] == "run.end"


@pytest.mark.slow
def test_a_runner_that_dies_without_writing_shows_up_in_the_state_stream(tmp_path):
    """runner 没了要能从状态流里看出来，哪怕 loop.json 一个字节没变。

    回归用例：状态流拿 `etag`（loop.json 的 mtime+size）判断有没有变化，可
    `running` 来自 pid 探活，而 `cmd_stop` **明确一个字节都不写状态文件**——
    于是 stop 或 SIGKILL 之后 etag 纹丝不动，订阅方一直以为它在跑，
    面板上「审一轮」永远是灰的，只能刷新页面。
    """
    h = Harness(tmp_path, [review()])
    r = h.run()
    loop = r.loop

    # 起一个名字里带 rloop 的真进程冒充 runner（pid_is_our_runner 要认命令行）
    sleeper_py = tmp_path / "rloop_sleeper.py"
    sleeper_py.write_text("import time; time.sleep(120)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(sleeper_py)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        st = loop.state
        st["status"] = "running"
        st["runner_pid"] = proc.pid
        st["runner_started"] = rloop.pid_field(proc.pid, "lstart=")
        loop.save(st)
        assert rloop.pid_is_our_runner(proc.pid, st["runner_started"]), \
            "冒充的 runner 没被认出来，这条用例的前提不成立"
        etag_before = rloop.loop_summary(loop)["etag"]

        # 起 follower，中途把那个进程杀掉 —— 全程不碰 loop.json
        argv = [sys.executable, str(REPO_ROOT / "rloop.py"), "api", "--api", "1",
                "events", "--state", "--follow", "--idle-timeout", "25",
                "--project", str(h.project)]
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True,
                             bufsize=1, env=h.env)
        time.sleep(5)
        proc.kill()
        proc.wait(timeout=10)
        out, _ = p.communicate(timeout=90)
    finally:
        if proc.poll() is None:
            proc.kill()

    states = [json.loads(l) for l in out.splitlines()
              if l.strip() and json.loads(l).get("kind") == "state"]
    mine = [e["data"]["loop"] for e in states
            if e["data"]["loop"]["id"] == r.state["id"]]
    assert len(mine) >= 2, f"runner 死了却没再发状态，只收到 {len(mine)} 条"
    assert mine[0]["running"] is True, "一开始就没认出它在跑，前提不成立"
    assert mine[-1]["running"] is False, "runner 已经没了，状态流还说它在跑"
    assert mine[-1]["etag"] == etag_before, \
        "loop.json 被改过了，这条用例就没验到「etag 不变」那个点"
