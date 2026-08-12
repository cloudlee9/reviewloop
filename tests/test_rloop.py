#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rloop 纯函数测试。

只覆盖不联网、不拉 agent 的判定与渲染逻辑：
gate_pass / detect_stall / load_review / render_score_history / read_text_safe。

两条硬约束，靠下面的 _sandbox fixture 强制执行，而不是靠自觉：
1. 绝不启动真实的 claude / codex —— rloop 模块里的 subprocess 被换成会抛
   AssertionError 的替身，任何 Popen/run 都会当场炸掉而不是真的 fork。
2. 绝不碰 ~/.rloop —— RLOOP_HOME / REGISTRY 指向 tmp_path。

文件自带 sys.path 引导，所以 `python3 -m pytest` 在任何 cwd 下都能跑。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402 —— 必须等 sys.path 就位之后再导


# ─────────────────────────── 沙箱 ───────────────────────────


class _ForbiddenSubprocess:
    """rloop.subprocess 的替身：常量照常透传，能起进程的入口一律炸掉。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        # PIPE / STDOUT / DEVNULL / TimeoutExpired 这些还是要能拿到
        return getattr(self._real, name)

    def run(self, *args, **kwargs):
        raise AssertionError(f"测试禁止启动子进程：subprocess.run({args[:1]})")

    def Popen(self, *args, **kwargs):
        raise AssertionError(f"测试禁止启动子进程：subprocess.Popen({args[:1]})")


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """每个用例都跑在沙箱里：起不了子进程，也写不到真实注册表。"""
    monkeypatch.setattr(rloop, "subprocess", _ForbiddenSubprocess(subprocess))
    monkeypatch.setattr(rloop, "RLOOP_HOME", tmp_path / "rloop-home")
    monkeypatch.setattr(rloop, "REGISTRY", tmp_path / "rloop-home" / "registry.json")


# ─────────────────────────── 夹具 ───────────────────────────


def make_loop(tmp_path: Path) -> rloop.Loop:
    """一个只有目录、没有 loop.json 的 Loop。被测函数都不读状态文件。"""
    return rloop.Loop(tmp_path / "loop")


def write_review(loop: rloop.Loop, rnd: int, payload) -> Path:
    """把 raw 内容落到 round-NN/review.json，bytes 原样写、str 按 UTF-8 写。"""
    path = loop.round_dir(rnd) / "review.json"
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def review_obj(**overrides) -> dict:
    """一份合法的 review，字段齐全；用 overrides 打洞造异常样本。"""
    data = {
        "deliverable_maturity": 8.5,
        "production_readiness": 7.0,
        "blocking_findings": 1,
        "verdict": "needs_work",
        "summary": "测试用样本。",
        "findings": [],
        "prior_findings_status": [],
        "positive_evidence": [],
        "validation_commands": [],
        "next_priorities": [],
    }
    data.update(overrides)
    return data


def hist(rnd: int, deliverable: float, production: float, blocking: int,
         verdict: str = "needs_work") -> dict:
    """一条分数历史，字段与 drive() 写进 state['history'] 的完全一致。"""
    return {
        "round": rnd,
        "deliverable_maturity": float(deliverable),
        "production_readiness": float(production),
        "blocking_findings": int(blocking),
        "verdict": verdict,
        "at": "2026-08-08T12:00:00Z",
    }


# ─────────────────────────── 沙箱自检 ───────────────────────────


def test_sandbox_blocks_real_subprocess():
    """先证明护栏是活的：不然下面所有"没起 agent"的说法都不成立。"""
    with pytest.raises(AssertionError, match="测试禁止启动子进程"):
        rloop.subprocess.run(["claude", "-p", "x"])
    with pytest.raises(AssertionError, match="测试禁止启动子进程"):
        rloop.subprocess.Popen(["codex", "exec", "x"])


def test_sandbox_keeps_subprocess_constants():
    """替身只拦启动入口，常量必须照常透传，否则挡住的是真实性而不是副作用。"""
    assert rloop.subprocess.PIPE is subprocess.PIPE
    assert rloop.subprocess.TimeoutExpired is subprocess.TimeoutExpired


# ─────────────────────────── gate_pass ───────────────────────────


def test_gate_pass_scores_exactly_at_threshold():
    """边界：分数恰好等于阈值应当通过（>=，不是 >）。"""
    review = review_obj(deliverable_maturity=8.0, production_readiness=8.0,
                        blocking_findings=0)
    assert rloop.gate_pass(review, 8.0) is True


@pytest.mark.parametrize("deliverable, production", [
    (7.9, 9.0),   # 交付物差一点
    (9.0, 7.9),   # 生产就绪差一点
    (7.9, 7.9),   # 两边都差一点
])
def test_gate_pass_rejects_either_score_below_threshold(deliverable, production):
    """两个维度是与关系，任一低于阈值都不通过。"""
    review = review_obj(deliverable_maturity=deliverable,
                        production_readiness=production, blocking_findings=0)
    assert rloop.gate_pass(review, 8.0) is False


@pytest.mark.parametrize("blocking", [1, 2, 7])
def test_gate_pass_rejects_nonzero_blocking_findings(blocking):
    """边界：blocking_findings 非零，哪怕双 10 分也不通过。"""
    review = review_obj(deliverable_maturity=10.0, production_readiness=10.0,
                        blocking_findings=blocking)
    assert rloop.gate_pass(review, 8.0) is False


def test_gate_pass_passes_when_scores_exceed_and_no_blockers():
    review = review_obj(deliverable_maturity=9.5, production_readiness=8.25,
                        blocking_findings=0)
    assert rloop.gate_pass(review, 8.0) is True


@pytest.mark.parametrize("min_score, expected", [
    (6.5, True),    # 恰好等于自定义阈值
    (6.6, False),   # 高于分数
    (0.0, True),    # 退化阈值
])
def test_gate_pass_honours_custom_threshold(min_score, expected):
    review = review_obj(deliverable_maturity=6.5, production_readiness=6.5,
                        blocking_findings=0)
    assert rloop.gate_pass(review, min_score) is expected


def test_gate_pass_coerces_string_numbers():
    """模型偶尔把数字写成字符串，gate_pass 里的 float()/int() 要兜得住。"""
    review = review_obj(deliverable_maturity="8.0", production_readiness="9",
                        blocking_findings="0")
    assert rloop.gate_pass(review, 8.0) is True


def test_gate_pass_only_needs_the_keys_load_review_guarantees(tmp_path):
    """契约：load_review 校验的四个字段，正好覆盖 gate_pass 会读的字段。"""
    loop = make_loop(tmp_path)
    minimal = {
        "deliverable_maturity": 8.0,
        "production_readiness": 8.0,
        "blocking_findings": 0,
        "verdict": "pass",
    }
    write_review(loop, 1, json.dumps(minimal))

    loaded = rloop.load_review(loop, 1)
    assert loaded is not None
    assert rloop.gate_pass(loaded, 8.0) is True  # 不能抛 KeyError


# ─────────────────────────── detect_stall ───────────────────────────


@pytest.mark.parametrize("rounds", [0, 1, 2])
def test_detect_stall_needs_more_than_stall_rounds_of_history(rounds):
    """样本不足 STALL_ROUNDS+1 轮时不判停滞，避免开局就熔断。"""
    history = [hist(i + 1, 5.0, 5.0, 2) for i in range(rounds)]
    assert rloop.detect_stall(history) is False


def test_detect_stall_triggers_on_flat_scores():
    """连续两轮分数纹丝不动、阻塞项也没减少 → 停滞。"""
    history = [hist(1, 6.0, 5.0, 2), hist(2, 6.0, 5.0, 2), hist(3, 6.0, 5.0, 2)]
    assert rloop.detect_stall(history) is True


def test_detect_stall_triggers_when_scores_regress():
    """倒退当然不算进展。"""
    history = [hist(1, 7.0, 6.0, 1), hist(2, 6.5, 5.5, 2), hist(3, 6.0, 5.0, 3)]
    assert rloop.detect_stall(history) is True


def test_detect_stall_triggers_on_sub_epsilon_improvement():
    """涨幅不超过 STALL_EPSILON 视为没涨。0.0625 是二进制精确值，避免浮点噪声。"""
    assert rloop.STALL_EPSILON == 0.1
    history = [hist(1, 7.0, 6.0, 2), hist(2, 7.0625, 6.0625, 2),
               hist(3, 7.125, 6.125, 2)]
    assert rloop.detect_stall(history) is True


def test_detect_stall_not_triggered_by_deliverable_improvement():
    """最后一轮交付物涨了 0.125（> epsilon）→ 有进展，不熔断。"""
    history = [hist(1, 7.0, 6.0, 2), hist(2, 7.0, 6.0, 2), hist(3, 7.125, 6.0, 2)]
    assert rloop.detect_stall(history) is False


def test_detect_stall_not_triggered_by_production_improvement():
    """两个维度任一有进展就够了。"""
    history = [hist(1, 7.0, 6.0, 2), hist(2, 7.0, 6.0, 2), hist(3, 7.0, 6.5, 2)]
    assert rloop.detect_stall(history) is False


def test_detect_stall_not_triggered_when_blocking_findings_drop():
    """分数卡住但阻塞项在减少，也算在推进。"""
    history = [hist(1, 7.0, 6.0, 3), hist(2, 7.0, 6.0, 2), hist(3, 7.0, 6.0, 1)]
    assert rloop.detect_stall(history) is False


def test_detect_stall_triggers_when_blocking_findings_grow():
    """阻塞项变多不是进展。"""
    history = [hist(1, 7.0, 6.0, 0), hist(2, 7.0, 6.0, 1), hist(3, 7.0, 6.0, 2)]
    assert rloop.detect_stall(history) is True


def test_detect_stall_only_looks_at_the_trailing_window():
    """早期的大跃进不能永久豁免熔断：窗口只看最后 STALL_ROUNDS+1 条。"""
    history = [hist(1, 3.0, 2.0, 5), hist(2, 7.0, 6.0, 2),
               hist(3, 7.0, 6.0, 2), hist(4, 7.0, 6.0, 2)]
    assert rloop.detect_stall(history) is True


def test_detect_stall_reset_by_recent_progress_in_long_history():
    """窗口内最后一跳有进展 → 不熔断，哪怕更早两轮是平的。"""
    history = [hist(1, 3.0, 2.0, 5), hist(2, 7.0, 6.0, 2),
               hist(3, 7.0, 6.0, 2), hist(4, 7.5, 6.0, 2)]
    assert rloop.detect_stall(history) is False


# ─────────────────────────── load_review ───────────────────────────


def test_load_review_returns_none_when_file_missing(tmp_path):
    loop = make_loop(tmp_path)
    assert rloop.load_review(loop, 1) is None


@pytest.mark.parametrize("raw", ["", "   ", "\n\n  \t\n"])
def test_load_review_returns_none_for_blank_file(tmp_path, raw):
    """agent 建了文件但没写东西，等同于没产出。"""
    loop = make_loop(tmp_path)
    write_review(loop, 1, raw)
    assert rloop.load_review(loop, 1) is None


def test_load_review_parses_plain_json(tmp_path):
    loop = make_loop(tmp_path)
    payload = review_obj(summary="干净的 JSON。")
    write_review(loop, 1, json.dumps(payload, ensure_ascii=False))

    data = rloop.load_review(loop, 1)
    assert data is not None
    assert data["deliverable_maturity"] == 8.5
    assert data["summary"] == "干净的 JSON。"
    assert data["findings"] == []  # 必需字段之外的内容原样保留


def test_load_review_tolerates_utf8_bom(tmp_path):
    """容错：带 BOM。read_text_safe 负责剥掉 EF BB BF。"""
    loop = make_loop(tmp_path)
    body = json.dumps(review_obj(summary="带 BOM。"), ensure_ascii=False)
    write_review(loop, 1, b"\xef\xbb\xbf" + body.encode("utf-8"))

    data = rloop.load_review(loop, 1)
    assert data is not None
    assert data["summary"] == "带 BOM。"
    assert data["verdict"] == "needs_work"


@pytest.mark.parametrize("fence", ["```json", "```JSON", "```"])
def test_load_review_tolerates_markdown_fence(tmp_path, fence):
    """容错：被 markdown fence 包裹。三种常见写法都要能剥。"""
    loop = make_loop(tmp_path)
    body = json.dumps(review_obj(summary="被 fence 包住了。"), ensure_ascii=False)
    write_review(loop, 1, f"{fence}\n{body}\n```\n")

    data = rloop.load_review(loop, 1)
    assert data is not None
    assert data["summary"] == "被 fence 包住了。"


def test_load_review_tolerates_bom_and_fence_together(tmp_path):
    """两种脏法叠加也要能救回来。"""
    loop = make_loop(tmp_path)
    body = json.dumps(review_obj(), ensure_ascii=False)
    write_review(loop, 1, b"\xef\xbb\xbf" + f"```json\n{body}\n```\n".encode("utf-8"))

    assert rloop.load_review(loop, 1) is not None


@pytest.mark.parametrize("raw", [
    "{not json at all}",
    '{"deliverable_maturity": 8.0,}',      # 尾逗号
    '{"deliverable_maturity": 8.0',        # 截断
    "评审失败，我没有产出 JSON。",           # 纯散文
])
def test_load_review_returns_none_for_invalid_json(tmp_path, raw):
    """容错：非法 JSON 返回 None，让 drive() 判定为 reviewer 失败而不是崩掉。"""
    loop = make_loop(tmp_path)
    write_review(loop, 1, raw)
    assert rloop.load_review(loop, 1) is None


@pytest.mark.parametrize("missing", [
    "deliverable_maturity",
    "production_readiness",
    "blocking_findings",
    "verdict",
])
def test_load_review_returns_none_when_required_field_missing(tmp_path, missing):
    """容错：缺任一必需字段就当没产出——gate_pass 靠这四个字段判定。"""
    loop = make_loop(tmp_path)
    payload = review_obj()
    del payload[missing]
    write_review(loop, 1, json.dumps(payload, ensure_ascii=False))

    assert rloop.load_review(loop, 1) is None


def test_load_review_survives_invalid_utf8_bytes(tmp_path):
    """非法字节被 read_text_safe 替换成 U+FFFD，JSON 结构没被破坏就照常解析。"""
    loop = make_loop(tmp_path)
    body = '{"deliverable_maturity": 8.0, "production_readiness": 8.0, ' \
           '"blocking_findings": 0, "verdict": "pass", "summary": "'
    write_review(loop, 1, body.encode("utf-8") + b"\xff\xfe" + b'"}')

    data = rloop.load_review(loop, 1)
    assert data is not None
    assert "�" in data["summary"]


def test_load_review_reads_the_requested_round(tmp_path):
    """轮次隔离：第 2 轮读到的必须是 round-02 的产物。"""
    loop = make_loop(tmp_path)
    write_review(loop, 1, json.dumps(review_obj(deliverable_maturity=5.0)))
    write_review(loop, 2, json.dumps(review_obj(deliverable_maturity=9.0)))

    assert rloop.load_review(loop, 1)["deliverable_maturity"] == 5.0
    assert rloop.load_review(loop, 2)["deliverable_maturity"] == 9.0


def test_load_review_tolerates_single_line_fence(tmp_path):
    """契约期望：整段 fence 写在一行时也应返回 None 而不是抛异常。"""
    loop = make_loop(tmp_path)
    write_review(loop, 1, '```{"deliverable_maturity": 8.0}```')
    assert rloop.load_review(loop, 1) is None


# ─────────────────────── render_score_history ───────────────────────


def test_render_score_history_empty():
    """空历史 = 首轮，输出占位符而不是空表头。"""
    assert rloop.render_score_history([]) == "（首轮）"


def test_render_score_history_single_round():
    out = rloop.render_score_history([hist(1, 6.0, 4.0, 3)])
    assert out == (
        "| 轮次 | 交付物成熟度 | 生产就绪度 | 阻塞项 | 判定 |\n"
        "|---:|---:|---:|---:|---|\n"
        "| 1 | 6.0 | 4.0 | 3 | needs_work |"
    )


def test_render_score_history_multiple_rounds():
    history = [
        hist(1, 6.0, 4.0, 3),
        hist(2, 7.5, 6.0, 1),
        hist(3, 8.5, 8.0, 0, verdict="pass"),
    ]
    out = rloop.render_score_history(history)
    lines = out.splitlines()

    assert len(lines) == 2 + len(history)          # 表头 + 分隔行 + 每轮一行
    assert lines[0].startswith("| 轮次 |")
    assert lines[1] == "|---:|---:|---:|---:|---|"
    assert lines[2] == "| 1 | 6.0 | 4.0 | 3 | needs_work |"
    assert lines[3] == "| 2 | 7.5 | 6.0 | 1 | needs_work |"
    assert lines[4] == "| 3 | 8.5 | 8.0 | 0 | pass |"
    assert not out.endswith("\n")                  # 由调用方决定怎么拼


def test_render_score_history_preserves_round_order():
    """按传入顺序渲染，不重排——分数走势要能看出方向。"""
    history = [hist(3, 8.0, 8.0, 0), hist(1, 5.0, 5.0, 4), hist(2, 6.0, 6.0, 2)]
    rows = rloop.render_score_history(history).splitlines()[2:]
    assert [r.split("|")[1].strip() for r in rows] == ["3", "1", "2"]


def test_render_score_history_ignores_extra_keys():
    """history 条目里带 at 等额外字段，渲染时不受影响。"""
    entry = hist(1, 8.0, 8.0, 0, verdict="pass")
    entry["summary"] = "多余字段"
    assert "多余字段" not in rloop.render_score_history([entry])


# ─────────────────────────── read_text_safe ───────────────────────────


def test_read_text_safe_strips_bom(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "内容".encode("utf-8"))
    assert rloop.read_text_safe(path) == "内容"


def test_read_text_safe_replaces_invalid_bytes(tmp_path):
    """坏字节不能让整个 loop 崩在读文件这一步。"""
    path = tmp_path / "b.txt"
    path.write_bytes(b"ok\xff\xfetail")
    out = rloop.read_text_safe(path)
    assert out.startswith("ok") and out.endswith("tail")
    assert "�" in out


def test_read_text_safe_keeps_inner_bom_like_bytes(tmp_path):
    """只剥文件开头的 BOM，正文里的同样字节序列不动。"""
    path = tmp_path / "c.txt"
    path.write_bytes("头".encode("utf-8") + b"\xef\xbb\xbf" + "尾".encode("utf-8"))
    assert rloop.read_text_safe(path) == "头﻿尾"


# ─────────────────────────── CLI 冒烟 ───────────────────────────


def test_cli_version_smoke():
    """真的把 rloop.py 当程序跑一次：argparse 在 --version 就退出，不会拉起任何 agent。

    这里用的是测试模块自己 import 的 subprocess，不是被替身挡住的 rloop.subprocess；
    启动的是 python3 本身，与 claude / codex 无关。
    """
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "rloop.py"), "--version"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"rloop {rloop.VERSION}"


# ─────────────────────── render_codex_event ───────────────────────


def _ev(**kw):
    return json.dumps(kw, ensure_ascii=False)


def test_command_start_is_shown_so_you_can_see_what_it_is_doing():
    line = _ev(type="item.started",
               item={"id": "i1", "type": "command_execution",
                     "command": "/bin/zsh -lc \"python3 -m pytest -q\"", "status": "in_progress"})
    out = rloop.render_codex_event(line)
    assert out and "pytest" in out and out.strip().startswith("$")


def test_successful_command_is_not_echoed_twice():
    """成功的命令不再报一次，否则每条命令刷两行，进度全是噪音。"""
    line = _ev(type="item.completed",
               item={"id": "i1", "type": "command_execution", "command": "ls",
                     "exit_code": 0, "status": "completed"})
    assert rloop.render_codex_event(line) is None


def test_failed_command_surfaces_its_exit_code():
    line = _ev(type="item.completed",
               item={"id": "i1", "type": "command_execution", "command": "pytest",
                     "exit_code": 1, "status": "completed"})
    out = rloop.render_codex_event(line)
    assert out and "exit 1" in out


def test_agent_prose_is_shown_but_structured_result_is_not():
    prose = _ev(type="item.completed",
                item={"id": "i1", "type": "agent_message", "text": "我先看一下 diff。"})
    assert "diff" in (rloop.render_codex_event(prose) or "")

    payload = _ev(type="item.completed",
                  item={"id": "i2", "type": "agent_message",
                        "text": '{"deliverable_maturity": 8.0}'})
    assert rloop.render_codex_event(payload) is None, "把结构化结果当进度打出来会刷屏"


def test_turn_completion_reports_token_usage():
    line = _ev(type="turn.completed", usage={"output_tokens": 1234})
    out = rloop.render_codex_event(line)
    assert out and "1234" in out


def test_non_event_lines_are_ignored():
    for junk in ("", "   ", "Reading additional input from stdin...", "{不是 json", "[]"):
        assert rloop.render_codex_event(junk) is None


# ─────────────────────── CLI 数值参数校验 ───────────────────────


@pytest.mark.parametrize("flag, value, hint", [
    ("--max-rounds", "0", "至少是 1"),
    ("--max-rounds", "-3", "至少是 1"),
    ("--timeout", "0", "至少是 1 秒"),
    ("--min-score", "11", "0 到 10"),
    ("--min-score", "-1", "0 到 10"),
    ("--min-score", "nan", "0 到 10"),
])
def test_out_of_range_numeric_flags_are_refused(flag, value, hint, tmp_path):
    """这些值都会让 loop 白跑：-n 0 付费跑完第一轮才判 exhausted、-t 0 一起步
    就超时杀 reviewer、-m nan 让门槛永远达不到，一路烧到熔断。"""
    import subprocess as sp
    r = sp.run([sys.executable, str(REPO_ROOT / "rloop.py"), flag, value,
                "-C", str(tmp_path)],
               capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert hint in r.stderr, r.stderr


# ─────────────────── 呈现层的冒烟（防拆分踩空） ───────────────────


def test_presentation_layer_is_actually_callable(tmp_path, monkeypatch):
    """collect_loops / latest_review / 两个渲染器真的能调。

    回归用例：把这几个函数从 rloop_ui.py 收回 rloop.py 时，函数体里
    `rloop.registry_read()` 这种带模块前缀的调用没跟着改，而核心模块里
    并没有叫 rloop 的名字 —— 一调就 NameError。整套测试当时全绿，因为
    根本没人调过它们。
    """
    root = tmp_path / "proj" / rloop.LOOP_DIRNAME / "20260101-000000-c-abcd"
    root.mkdir(parents=True)
    (root / "loop.json").write_text(json.dumps({
        "id": "20260101-000000-c-abcd", "project": str(tmp_path / "proj"),
        "status": "open", "round": 1, "max_rounds": 5, "min_score": 8.0,
        "scope_desc": "测试范围", "started_at": "2026-01-01T00:00:00Z",
        "history": [hist(1, 7.0, 6.0, 2)],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "round-01").mkdir()
    (root / "round-01" / "review.json").write_text(
        json.dumps(review_obj(findings=[{
            "id": "F1", "severity": "high", "category": "correctness",
            "file": "a.py", "line": 3, "description": "示例问题。",
            "suggested_fix": "示例修法。"}]), ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(rloop, "REGISTRY", tmp_path / "registry.json")
    (tmp_path / "registry.json").write_text(json.dumps({
        "20260101-000000-c-abcd": {"root": str(root), "project": str(tmp_path / "proj"),
                                   "label": "x", "started_at": "2026-01-01T00:00:00Z"}
    }), encoding="utf-8")

    loops = rloop.collect_loops()
    assert len(loops) == 1 and loops[0]["deliverable"] == 7.0

    review = rloop.latest_review(loops[0])
    assert review is not None and review["findings"][0]["id"] == "F1"

    lines = rloop.render_detail(loops[0], review, 80)
    assert any("F1" in text for _, text in lines), "findings 没渲染进去"
    assert rloop.plain(lines).strip()

    md = rloop.render_markdown(loops[0], review)
    assert "# 第 1 轮 review" in md and "F1" in md and "| 轮 |" in md


def test_presentation_layer_survives_a_loop_with_no_review(tmp_path, monkeypatch):
    """还没跑出结果的 loop 也要能渲染，不能抛。"""
    root = tmp_path / "proj" / rloop.LOOP_DIRNAME / "20260101-000000-c-ffff"
    root.mkdir(parents=True)
    (root / "loop.json").write_text(json.dumps({
        "id": "20260101-000000-c-ffff", "project": str(tmp_path / "proj"),
        "status": "running", "round": 0, "max_rounds": 5, "min_score": 8.0,
        "scope_desc": "还没跑", "started_at": "2026-01-01T00:00:00Z", "history": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rloop, "REGISTRY", tmp_path / "registry.json")
    (tmp_path / "registry.json").write_text(json.dumps({
        "20260101-000000-c-ffff": {"root": str(root), "project": str(tmp_path / "proj"),
                                   "label": "x", "started_at": "2026-01-01T00:00:00Z"}
    }), encoding="utf-8")

    loops = rloop.collect_loops()
    assert rloop.latest_review(loops[0]) is None
    assert rloop.plain(rloop.render_detail(loops[0], None, 80)).strip()
    assert "没有可用的 review 结果" in rloop.render_markdown(loops[0], None)


def test_the_panel_imports_without_dragging_the_core_in():
    """面板的每个模块都能单独 import，且 import 完 sys.modules 里没有 rloop。

    上一版这条测试断言的正好相反 —— 那时面板 `import rloop` 拿数据层，测试
    盯着「有没有接上核心」。现在依赖方向反过来了：面板只经 CLI 说话，
    import 期把核心拉进来就说明有人抄了近路。

    （静态的那一侧由 tests/test_gui_isolation.py 扫 AST 挡着，这里补的是
    运行期：传递依赖也可能把核心带进来。）
    """
    import importlib
    import subprocess
    code = (
        "import sys;"
        "sys.path.insert(0, %r);"
        "import rloopgui, rloopgui.client, rloopgui.contract,"
        " rloopgui.errors, rloopgui.web;"
        "print('rloop' in sys.modules)"
    ) % str(REPO_ROOT)
    # 起个干净的解释器：本进程早就 import 过 rloop 了，在这儿查 sys.modules 没意义
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, f"面板 import 不起来：\n{out.stderr}"
    assert out.stdout.strip() == "False", "import 面板把核心也拉进来了"


def test_the_web_entry_point_finds_the_panel_by_its_own_location():
    """`rloop web` 顺着自己的真实位置找同级的 rloopgui/，不靠 cwd 也不靠 sys.path。

    装好之后 `rloop` 是 ~/.local/bin 里指向别处的符号链接，用户可能在任何目录
    下敲它 —— 早先用 `find_spec("rloopgui")` + 裸 `-m rloopgui`，那要求 cwd 正好
    是仓库根目录，换个目录就 `No module named rloopgui`。
    """
    src = (REPO_ROOT / "rloop.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_web"):]
    body = body[:body.index("\ndef ")]
    assert "resolve()" in body, "没解符号链接，装好之后 __file__ 指的是链接本身"
    assert "PYTHONPATH" in body, "没把 rloopgui 的所在目录传给子解释器"
    assert "RLOOP_BIN" in body, "没把 RLOOP_BIN 传下去，面板只能靠猜找回核心"
    assert "die(" in body, "找不到面板时该说人话，不是甩 ModuleNotFoundError"
    assert (REPO_ROOT / "rloopgui" / "__init__.py").exists()


# ─────────── loop 的名字 ───────────

def _diff(*chunks) -> str:
    """拼一个够用的 diff：每项是 (路径, 改动行数)。"""
    out = []
    for path, n in chunks:
        out.append(f"diff --git a/{path} b/{path}\nindex 1..2 100644\n"
                   f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{n} @@")
        out += [f"+第 {i} 行" for i in range(n)]
    return "\n".join(out) + "\n"


# 关键在于两种排法会给出不同答案：按**文件个数** tests 赢（3 个文件），
# 按**改动行数** rloopgui 赢（20 行）。名字要按行数来。
DIFF_SAMPLE = _diff(
    ("tests/test_a.py", 2), ("tests/test_b.py", 2), ("tests/test_c.py", 2),
    ("rloopgui/web.py", 20),
    ("rloop.py", 8),
)


def test_a_name_is_weighted_by_lines_not_file_count():
    """按改动行数排，不是按文件数。

    测试文件个数多、改得也勤，按个数排的话每个 loop 都叫「tests」，
    列表上一点区分度都没有 —— 那正是加名字要解决的问题。
    """
    got = rloop.infer_label(DIFF_SAMPLE)
    head = got.split("、")[0]
    assert head == "rloopgui", f"按文件数排的话 tests 会占头名，实际得到：{got}"
    assert got.split("、")[1] == "rloop.py", f"tests 挤掉了真正的改动：{got}"


def test_the_authors_own_words_win():
    """作者自己写的侧重最能说明「在做什么」，优先用它。"""
    got = rloop.infer_label(DIFF_SAMPLE, "这次把 GUI 从核心切了出来：新增 api 契约。还有别的")
    assert got == "这次把 GUI 从核心切了出来", got


def test_a_long_first_sentence_is_cut_with_an_ellipsis():
    long = "把" * 60
    got = rloop.infer_label("", long)
    assert len(got) == rloop.LABEL_MAX_CHARS + 1 and got.endswith("…")


def test_a_name_survives_a_diff_it_cannot_read():
    """diff 是空的或读不懂时给空串，不要抛。名字是锦上添花，不能挡住一轮 review。"""
    assert rloop.infer_label("") == ""
    assert rloop.infer_label("完全不是 diff 的一坨东西") == ""


def test_the_summary_exposes_the_name_without_recomputing(tmp_path):
    """title 直接读账本里的 label，不在查询路径上现算。

    --state 每 3 秒就要过一遍全部 loop；在那儿读 diff 算名字，面板一开着
    就是持续的磁盘负担。
    """
    root = tmp_path / "L1"
    root.mkdir()
    (root / "loop.json").write_text(json.dumps({
        "id": "L1", "project": str(tmp_path), "status": "open",
        "label": "GUI 切分", "round": 0, "max_rounds": 5,
    }), encoding="utf-8")
    summary = rloop.loop_summary(rloop.Loop(root))
    assert summary["title"] == "GUI 切分"

    (root / "loop.json").write_text(json.dumps({
        "id": "L1", "project": str(tmp_path), "status": "open",
        "round": 0, "max_rounds": 5,
    }), encoding="utf-8")
    assert rloop.loop_summary(rloop.Loop(root))["title"] == "", \
        "账本里没名字就该是空的，不许现算"


# ─────────── 大补丁 ───────────

def _big_loop(tmp_path, diff_bytes: int):
    """造一个 loop，让 build_scope_patch_detailed 返回指定大小的补丁。"""
    root = tmp_path / "L"
    (root / "round-01").mkdir(parents=True)
    loop = rloop.Loop(root)
    loop.save({"id": "L", "project": str(tmp_path), "diff_base": "HEAD",
               "diff_target": None, "scope_desc": "测试", "reviewer": "codex",
               "min_score": 8.0, "max_rounds": 5, "round": 0, "history": [],
               "status": "open", "focus": None})
    fake = "\n".join(f"+第 {i} 行的改动内容占点地方" for i in range(diff_bytes // 34))
    return loop, fake


def test_an_oversized_patch_tells_the_reviewer_it_cannot_read_it_all(tmp_path, monkeypatch):
    """补丁大到读不完时，必须**明说**，并要求 reviewer 交代覆盖范围。

    不说的话它读个开头就打分，而没有任何人知道这件事 —— 分数看起来和通读过
    一样可信。这比明说「只看了一部分」危险得多。
    """
    loop, fake = _big_loop(tmp_path, rloop.DIFF_LARGE_BYTES + 50_000)
    monkeypatch.setattr(rloop, "build_scope_patch_detailed", lambda *a: (fake, []))
    pack = rloop.build_context_pack(loop, 1)

    assert "你多半读不完" in pack
    assert "summary" in pack and "覆盖" in pack, "没要求它交代看了哪些部分"
    assert "production_readiness" in pack, "没说这件事要反映到分数上"


def test_a_normal_patch_says_nothing_about_size(tmp_path, monkeypatch):
    """正常大小别唠叨 —— 每轮都喊「读不完」会让这句话失去意义。"""
    loop, fake = _big_loop(tmp_path, rloop.DIFF_LARGE_BYTES // 4)
    monkeypatch.setattr(rloop, "build_scope_patch_detailed", lambda *a: (fake, []))
    assert "读不完" not in rloop.build_context_pack(loop, 1)


def test_an_oversized_patch_is_never_truncated(tmp_path, monkeypatch):
    """**不许截断**。截断等于悄悄丢掉一部分改动，比让 reviewer 挑着看更糟 ——
    丢掉的那部分谁都不知道存在过，连 reviewer 都没机会说「我没看这块」。"""
    loop, fake = _big_loop(tmp_path, rloop.DIFF_LARGE_BYTES + 50_000)
    monkeypatch.setattr(rloop, "build_scope_patch_detailed", lambda *a: (fake, []))
    rloop.build_context_pack(loop, 1)
    written = (loop.round_path(1) / "diff.patch").read_text(encoding="utf-8")
    assert written == fake, "补丁被截断了"


def test_the_context_pack_does_not_grow_with_the_patch(tmp_path, monkeypatch):
    """pack 里只给补丁的**路径**，不内联内容。

    这是「10 万行改动会不会让每轮都变慢」的答案：rloop 这一侧不会。
    """
    loop, small = _big_loop(tmp_path, 20_000)
    monkeypatch.setattr(rloop, "build_scope_patch_detailed", lambda *a: (small, []))
    a = len(rloop.build_context_pack(loop, 1))

    loop2, huge = _big_loop(tmp_path / "x", rloop.DIFF_LARGE_BYTES * 4)
    monkeypatch.setattr(rloop, "build_scope_patch_detailed", lambda *a: (huge, []))
    b = len(rloop.build_context_pack(loop2, 1))

    assert len(huge) > len(small) * 50, "样本没拉开差距，这条测试说明不了什么"
    assert b < a * 3, f"补丁大了 50 倍，pack 从 {a} 涨到 {b} —— 内容被内联进去了"


# ─────────── token 用量 ───────────

USAGE_LOG = """\
{"type":"item.started","item":{"type":"command_execution","command":"ls"}}
{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":50,"output_tokens":10}}
不是 JSON 的一行
{"type":"turn.completed","usage":{"input_tokens":900,"cached_input_tokens":800,\
"output_tokens":30,"reasoning_output_tokens":12}}
"""


def test_usage_takes_the_last_turn_not_the_first(tmp_path):
    """取最后一条：那是整个 turn 的累计。

    reviewer 每调一次工具就把整个上下文重发一遍，中间那些是过程量。
    """
    log = tmp_path / "reviewer.log"
    log.write_text(USAGE_LOG, encoding="utf-8")
    u = rloop.parse_reviewer_usage(log)
    assert u["input"] == 900 and u["cached"] == 800 and u["output"] == 30
    assert u["reasoning"] == 12


def test_usage_separates_what_is_actually_billed_as_new(tmp_path):
    """`fresh` 单独记一份。

    input 总数九成是缓存命中，只看它会以为贵十倍 —— 而这正是用户问「会不会
    很费 token」时真正想知道的那个数。
    """
    log = tmp_path / "reviewer.log"
    log.write_text(USAGE_LOG, encoding="utf-8")
    assert rloop.parse_reviewer_usage(log)["fresh"] == 100      # 900 - 800


def test_usage_is_absent_rather_than_zero_when_unavailable(tmp_path):
    """claude 那条路径拿不到用量，返回 None，账本里就没有这个字段。

    编一个 0 出来会让走势表显示「实付 0」，比不显示更误导。
    """
    log = tmp_path / "reviewer.log"
    log.write_text("claude 不吐 JSONL 事件流\n", encoding="utf-8")
    assert rloop.parse_reviewer_usage(log) is None
    assert rloop.parse_reviewer_usage(tmp_path / "根本不存在.log") is None


def test_the_history_table_reports_fresh_tokens_not_the_inflated_total():
    """走势表里报「实付」，不报 input 总量。"""
    h = [{**hist(1, 7.0, 6.0, 1),
          "usage": {"input": 1_000_000, "cached": 900_000, "fresh": 100_000,
                    "output": 5_000, "reasoning": 2_000}}]
    md = rloop.render_score_history(h)
    assert "100,000" in md, "没报实付"
    assert "90%" in md, "没说缓存命中率，读者会以为花了一百万"

    lines = rloop.plain(rloop.render_history(h, 8.0))
    assert "100,000" in lines and "1,000,000" not in lines.split("合计")[0]


def test_the_history_table_stays_clean_without_usage():
    """没有用量数据时不加空列 —— reviewer 是 claude 时就是这种情况。"""
    h = [hist(1, 7.0, 6.0, 1)]
    assert "实付" not in rloop.render_score_history(h)
    assert "实付" not in rloop.plain(rloop.render_history(h, 8.0))


# ─────────── reviewer 能不能跑命令验证 ───────────

def test_verify_is_on_by_default_and_can_be_turned_off():
    """默认让 reviewer 能跑测试。

    拿不到实证的评审只能靠读代码猜，production_readiness 也就永远封在 5 分 ——
    那正是这个工具长期以来最实的一个短板。
    """
    codex_on = rloop.reviewer_cmd("codex", Path("/p"), "P", Path("/s"), Path("/o"))
    assert "workspace-write" in codex_on, "默认没给 reviewer 验证能力"
    codex_off = rloop.reviewer_cmd("codex", Path("/p"), "P", Path("/s"), Path("/o"),
                                   verify=False)
    assert "read-only" in codex_off and "workspace-write" not in codex_off


def test_the_loosened_sandbox_is_the_smallest_one_that_works():
    """放开到 workspace-write 就够，不许用更宽的档。

    实测过这三件事：workspace-write 下 pytest 跑得通、/tmp 写得进、
    但写 HOME 会被内核拒（operation not permitted）。
    danger-full-access 和 --dangerously-bypass-approvals-and-sandbox 都是
    连 HOME 一起放开，没有理由为跑个测试付那个代价。
    """
    cmd = rloop.reviewer_cmd("codex", Path("/p"), "P", Path("/s"), Path("/o"))
    assert "danger-full-access" not in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--dangerously-bypass-hook-trust" not in cmd, "替仓库绕过了 hook trust"


def test_claude_falls_back_to_plan_when_not_verifying():
    on = rloop.reviewer_cmd("claude", Path("/p"), "P", Path("/s"), Path("/o"))
    off = rloop.reviewer_cmd("claude", Path("/p"), "P", Path("/s"), Path("/o"),
                             verify=False)
    assert on[on.index("--permission-mode") + 1] == "auto"
    assert off[off.index("--permission-mode") + 1] == "plan"
    # 两种模式下都不许放开自定义项：仓库里的 hook 在模型说第一句话之前就会跑
    assert "--safe-mode" in on and "--safe-mode" in off


def test_the_permission_note_matches_what_the_reviewer_can_actually_do():
    """说错的代价是具体的：以为自己只读就不去跑，以为自己能跑就谎称跑过。"""
    for agent in ("codex", "claude"):
        ro = rloop.reviewer_permission_note(agent, verify=False)
        rw = rloop.reviewer_permission_note(agent, verify=True)
        assert "只读" in ro or "写不了" in ro
        assert "绝不能改这个项目的代码" in rw, f"{agent} 的放开说明没写禁令"
    # claude 那边没有内核兜底，必须说明白
    assert "没有操作系统层面的沙箱" in rloop.reviewer_permission_note("claude", True)
    assert "内核" in rloop.reviewer_permission_note("codex", True)
    # 越界的后果也要说实话。codex 那边有事件流可查，作废是真会发生的；claude 那边
    # 扫不到执行记录，作废触发不了 —— 照抄"会作废"就是拿一句执行不了的威慑当保险。
    assert "整轮作废" in rloop.reviewer_permission_note("codex", True)
    claude_note = rloop.reviewer_permission_note("claude", True)
    assert "整轮作废" not in claude_note, "对 claude 许了一个执行不了的诺"
    assert "触发不了" in claude_note and "本来就不作数" in claude_note


def test_the_loop_total_reports_what_you_actually_pay():
    """报「实付」而不是 input 总数 —— 后者九成是缓存命中，照它看会以为贵十倍。"""
    hist = [
        {"round": 1, "usage": {"input": 843035, "cached": 741376,
                               "fresh": 101659, "output": 6926}},
        {"round": 2, "usage": {"input": 1367291, "cached": 1284096,
                               "fresh": 83195, "output": 8691}},
    ]
    tot = rloop.usage_total(hist)
    assert tot["fresh"] == 184854 and tot["output"] == 15617
    assert tot["input"] == 2210326 and tot["rounds"] == 2

    # 拿不到用量时是 None，不是 0 ——「不知道」和「没花钱」是两回事
    assert rloop.usage_total([]) is None
    assert rloop.usage_total([{"round": 1}, {"round": 2}]) is None

    # 混着来的时候，rounds 是**有数据的轮数**，不是总轮数：写成总轮数会让人
    # 以为每轮都这么便宜
    mixed = rloop.usage_total([{"round": 1, "usage": hist[0]["usage"]}, {"round": 2}])
    assert mixed["rounds"] == 1 and mixed["fresh"] == 101659


def test_the_fingerprint_notices_the_reviewer_touching_the_code():
    """指纹是放开写权限之后唯一的硬保险，不能只靠 prompt 里嘱咐一句。"""
    base = {"status": [" M a.py"], "head": "abc", "diff": "h1"}
    assert rloop.fingerprint_changed(base, dict(base)) == []
    assert rloop.fingerprint_changed(base, {**base, "diff": "h2"}) == ["diff"]
    assert rloop.fingerprint_changed(base, {**base, "head": "def"}) == ["head"]
    # 同一个文件被改第二次时 status 字符串不变，只有内容摘要能抓到
    assert rloop.fingerprint_changed(base, {**base, "diff": "h3"}) == ["diff"]


def test_editing_your_own_code_during_a_review_is_not_blamed_on_the_reviewer(tmp_path):
    """回归用例，来自一次真实的误判。

    第一版只看指纹：工作区在评审期间变了就整轮作废。可评审要跑好几分钟，而"边改边审"
    正是 rloop 的用法 —— 第一次实跑时作者在那五分钟里改了两行 `rloop.py`，303 秒的
    评审连同它跑出来的实证一起被判无效。指纹能证明工作区变了，证明不了是谁变的，
    所以作废必须有第二个信号：reviewer 自己的执行记录。
    """
    log = tmp_path / "reviewer.log"

    def ev(cmd):
        return json.dumps({"type": "item.completed",
                           "item": {"type": "command_execution", "command": cmd}},
                          ensure_ascii=False)

    # 一次规规矩矩的评审：读代码、跑测试、往 /tmp 写点东西
    log.write_text("\n".join([
        '{"type":"thread.started","thread_id":"x"}',
        ev("/bin/zsh -lc 'pytest -q'"),
        ev("/bin/zsh -lc \"git status --short && sed -n '1,620p' diff.patch\""),
        ev("/bin/zsh -lc 'python3 - <<PY > /tmp/probe.log\\nprint(1)\\nPY'"),
        ev("/bin/zsh -lc 'git diff --check'"),
    ]), encoding="utf-8")
    assert rloop.reviewer_write_evidence(log) == [], "把正常评审动作当成了动手改代码"

    # 检索自己的源码不是动手 —— reviewer 实测抓到的误判：
    # `rg -n apply_patch rloop.py` 曾经被当成写入证据，一条纯检索命令白烧一整轮。
    for cmd in ("/bin/zsh -lc \"rg -n apply_patch rloop.py\"",
                "/bin/zsh -lc 'grep -rn \"git commit\" README.md'",
                "/bin/zsh -lc \"nl -ba rloop.py | sed -n '600,700p'\"",
                "/bin/zsh -lc 'git status --short && git log --oneline'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log) == [], f"检索命令被判成动手：{cmd}"

    # 在临时目录里复现 finding 不是动手 —— reviewer 实测抓到的第二种误判：
    # 它为了复现问题在 /tmp 建仓库、写文件、commit，全是正当的评审动作；作者若
    # 恰好同时改了真工作区，两个互不相干的信号会凑成一次误作废。
    proj = tmp_path / "proj"
    for cmd in ("/bin/zsh -lc 'cd /tmp/probe && echo x > sample.py'",
                "/bin/zsh -lc 'cd /tmp/probe && git commit -am probe'",
                "/bin/zsh -lc 'git -C /tmp/probe commit -am x'",
                "/bin/zsh -lc 'd=$(mktemp -d) && cd $d && git commit -qm x'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj) == [], f"临时目录里的操作被判成动手：{cmd}"

    # 但项目自己目录下的同类命令照抓不误
    log.write_text(ev(f"/bin/zsh -lc 'cd {proj} && echo x > a.py'"), encoding="utf-8")
    assert rloop.reviewer_write_evidence(log, proj), "cd 回项目里的写操作被放过了"

    # 真动手的样子：写命令得出现在命令位置上
    for cmd in ("/bin/zsh -lc \"sed -i '' 's/foo/bar/' rloop.py\"",
                "/bin/zsh -lc 'echo x > rloop.py'",
                "/bin/zsh -lc 'git commit -am wip'",
                "/bin/zsh -lc 'pytest -q && git checkout -- rloop.py'",
                "apply_patch <<'EOF'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log), f"没认出动手证据：{cmd}"

    # codex 用自己的工具改文件时走事件，不经过 shell
    log.write_text(json.dumps({"type": "item.completed",
                               "item": {"type": "file_change", "path": "rloop.py"}}),
                   encoding="utf-8")
    assert rloop.reviewer_write_evidence(log)

    assert rloop.reviewer_write_evidence(tmp_path / "根本没有这个文件") == []


def test_an_incomplete_fingerprint_is_announced(tmp_path):
    """指纹这道保险失败开放 —— 那就必须说出口，两张快照都要说。

    变异测试发现：把这条提醒整个删掉，全套测试照样绿。也就是说「拍不全会告诉你」
    先前只是一句没人守的承诺。
    """
    loop = make_loop(tmp_path)
    loop.root.mkdir(parents=True, exist_ok=True)   # make_loop 只拼路径不建目录

    full = {k: "x" for k in rloop.FINGERPRINT_KEYS}
    assert rloop.note_incomplete_fingerprint(loop, full, "起跑前") is True

    partial = {"head": "abc"}
    assert rloop.note_incomplete_fingerprint(loop, partial, "跑完后") is False
    log = (loop.root / "loop.log").read_text("utf-8")
    assert "跑完后" in log and "不完整" in log
    for missing in ("status", "diff", "untracked"):
        assert missing in log, f"没点名缺了哪一维：{missing}"


def test_codex_patch_events_count_as_evidence_whatever_they_are_called(tmp_path):
    """codex 用自己的工具改文件时不经过 shell，只留一个事件。

    变异测试发现：把 patch_apply 这一支删掉，测试不红 —— 当时只有 file_change
    被覆盖着。两个名字都得认，认漏了就等于放过一整类动手方式。
    """
    log = tmp_path / "reviewer.log"
    for kind in ("file_change", "patch_apply"):
        log.write_text(json.dumps({"type": "item.completed",
                                   "item": {"type": kind, "path": "rloop.py"}}),
                       encoding="utf-8")
        hits = rloop.reviewer_write_evidence(log, tmp_path / "proj")
        assert hits and kind in hits[0], f"{kind} 事件没被当成证据"


def test_formatters_that_rewrite_code_count_as_tampering(tmp_path):
    """`ruff --fix` / `gofmt -w` 改起被审代码来和 sed -i 没有区别。

    这不是「刻意规避检测」那一类 —— reviewer 顺手跑个 formatter 想看看代码风格，
    就能把被审代码重写一遍，而先前的检测只认几个命令名，一个都抓不到。
    带 --check / --dry-run 的那些不算，它们不写盘。
    """
    log = tmp_path / "reviewer.log"
    proj = tmp_path / "proj"

    def ev(cmd):
        return json.dumps({"type": "item.completed",
                           "item": {"type": "command_execution", "command": cmd}},
                          ensure_ascii=False)

    for cmd in ("/bin/zsh -lc 'ruff check --fix .'",
                "/bin/zsh -lc 'gofmt -w ./...'",
                "/bin/zsh -lc 'black .'",
                "/bin/zsh -lc 'eslint --fix src/'",
                "/bin/zsh -lc 'npm run lint:fix'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj), f"改代码的工具没被认出来：{cmd}"

    for cmd in ("/bin/zsh -lc 'ruff check .'",
                "/bin/zsh -lc 'gofmt -l ./...'",
                "/bin/zsh -lc 'black --check .'",
                "/bin/zsh -lc 'npm run lint'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj) == [], f"只查不改被判成动手：{cmd}"


def test_read_only_git_commands_are_not_tampering_evidence(tmp_path):
    """自审抓到的第三种误判。

    `git apply --check` 是校验补丁能不能打、`git stash list` 是列表、
    `git commit --dry-run` 什么都不做 —— 全是只读动作。只按子命令名匹配会把它们
    连坐进来，而命中的后果是整轮作废：作者只要恰好在这五分钟里改了自己的代码，
    两个互不相干的信号就凑成一次误杀。
    """
    log = tmp_path / "reviewer.log"
    proj = tmp_path / "proj"

    def ev(cmd):
        return json.dumps({"type": "item.completed",
                           "item": {"type": "command_execution", "command": cmd}},
                          ensure_ascii=False)

    for cmd in ("/bin/zsh -lc 'git apply --check /tmp/p.diff'",
                "/bin/zsh -lc 'git apply --stat d.patch'",
                "/bin/zsh -lc 'git stash list'",
                "/bin/zsh -lc 'git commit --dry-run'",
                "/bin/zsh -lc 'git reset --help'",
                "/bin/zsh -lc 'git diff --stat && git status --short'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj) == [], f"只读 git 被判成动手：{cmd}"

    # 同一批子命令真去写的时候必须照抓
    for cmd in ("/bin/zsh -lc 'git apply /tmp/p.diff'",
                "/bin/zsh -lc 'git stash push -u'",
                "/bin/zsh -lc 'git commit -am wip'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj), f"没认出动手证据：{cmd}"


def test_writing_to_a_temp_path_is_not_tampering_evidence(tmp_path):
    """`echo x > /tmp/probe/foo.py` 没有 cd、也没提 mktemp —— 光看"切没切目录"漏得干净。

    这同样是 reviewer 复现问题时的常规动作，同样会凑成误作废。
    """
    log = tmp_path / "reviewer.log"
    proj = tmp_path / "proj"

    def ev(cmd):
        return json.dumps({"type": "item.completed",
                           "item": {"type": "command_execution", "command": cmd}},
                          ensure_ascii=False)

    for cmd in ("/bin/zsh -lc 'echo x > /tmp/probe/foo.py'",
                "/bin/zsh -lc 'cat > /private/var/folders/ab/t.py <<EOF'",
                f"/bin/zsh -lc 'echo x > {tmp_path}/elsewhere/other.py'"):
        log.write_text(ev(cmd), encoding="utf-8")
        assert rloop.reviewer_write_evidence(log, proj) == [], f"项目外的写被判成动手：{cmd}"

    # 项目自己的文件照抓
    log.write_text(ev(f"/bin/zsh -lc 'echo x > {proj}/a.py'"), encoding="utf-8")
    assert rloop.reviewer_write_evidence(log, proj), "项目内的写被放过了"


def test_test_droppings_are_named_not_treated_as_tampering():
    """跑测试必然掉产物；把那个当成"改了代码"，放开写权限这件事就白做了。

    但只是"多出文件"才不算 —— 未跟踪文件会进下一轮的送审范围，所以要点名。
    """
    # 未跟踪指纹行的格式是 `size\tdigest\t路径` —— 摘要在前、路径在后，因为路径里
    # 可能带 TAB，放前面会让解析从文件名中间断开。
    base = {"status": [" M a.py"], "head": "abc", "diff": "h1",
            "untracked": ["10\tsha-notes\tnotes.md"]}
    after = {**base, "status": [" M a.py", "?? out.log"],
             "untracked": ["10\tsha-notes\tnotes.md", "3\tsha-cache\t.pytest_cache/v/x",
                           "9\tsha-out\tout.log"]}

    assert rloop.tampered_dimensions(base, after) == [], "多出文件被判成了改代码"
    assert rloop.fingerprint_changed(base, after) == ["status", "untracked"]
    assert rloop.new_paths(base, after) == [".pytest_cache/v/x", "out.log"], "没点名多出来的是哪些"

    # 反过来：代码真被动了的时候，多出来的文件不能把它盖过去
    assert rloop.tampered_dimensions(base, {**after, "diff": "h2"}) == ["diff"]


def test_editing_an_untracked_file_counts_as_touching_the_code():
    """reviewer 自己抓出来的洞（0.4.0 第一次实跑，high）。

    未跟踪文件的内容一样会进送审补丁。可它改一个**已经存在**的未跟踪源码文件时，
    `?? path` 那行不变、`git diff HEAD` 根本不看它、HEAD 也没动 —— 前三个维度
    全静止。所以指纹必须单独记未跟踪文件的大小和 mtime。
    """
    base = {"status": ["?? new.py"], "head": "abc", "diff": "h1",
            "untracked": ["120\tsha-old\tnew.py"]}
    edited = {**base, "untracked": ["180\tsha-new\tnew.py"]}

    assert rloop.tampered_dimensions(base, edited) == ["untracked"], "改未跟踪源码没被抓到"
    assert rloop.fingerprint_changed(base, edited) == ["untracked"]
    assert rloop.new_paths(base, edited) == [], "改内容不该报成新增文件"

    # 删掉基线里的未跟踪文件同样是动送审内容 —— 下一轮那个文件就没了
    deleted = {**base, "status": [], "untracked": []}
    assert rloop.tampered_dimensions(base, deleted) == ["untracked"]

    # 判据是"基线里的条目还在不在、变没变"，所以 reviewer 自己生成又删掉的临时文件
    # 两张快照都看不见，不会牵连到它
    kept = {**base, "untracked": ["120\tsha-old\tnew.py", "99\tsha-obj\tbuild/out.o"]}
    assert rloop.tampered_dimensions(base, kept) == []


def test_a_missing_fingerprint_does_not_accuse_anyone():
    """拍不到指纹（不是 git 仓库、git 挂了）时不许诬告。

    但这道保险是**失败开放**的：拍不到就等于没人核对，reviewer 照样带着写权限跑。
    这不是可以默不作声的取舍，所以 run_reviewer 会在维度拍不全时明说
    （见 test_an_incomplete_fingerprint_is_announced_not_swallowed）。
    """
    assert rloop.fingerprint_changed({}, {"diff": "x"}) == []
    assert rloop.fingerprint_changed({"diff": "x"}, {}) == []
    assert rloop.tampered_dimensions({}, {"diff": "x"}) == []
    assert rloop.tampered_dimensions({"diff": "x"}, {}) == []
    # 缺的维度不参与比较，剩下的照常比
    assert rloop.fingerprint_changed({"head": "a"}, {"head": "a", "diff": "x"}) == []
    assert rloop.fingerprint_changed({"head": "a"}, {"head": "b", "diff": "x"}) == ["head"]
