#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rloop 的范围判定测试：`determine_scope` / `default_branch` / `build_scope_patch`。

送审范围是整个 loop 的根：范围错了，reviewer 看的是别人的代码，fixer 会去改范围
外的东西。这层过去只有真实链路碰过，所以单独立一档来钉死。

这一档**用真的 git，但绝不起 agent**：下面的 `_git_only` fixture 把 rloop 模块里
的 `subprocess.run` 限制成只能执行 `git`，`Popen`（两个 agent 唯一的入口）一律
抛 AssertionError。所以它不花配额、不联网、跑得和纯函数一样快，默认档就跑。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rloop  # noqa: E402


# ─────────────────────────── 沙箱：只放行 git ───────────────────────────


class _GitOnlySubprocess:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, cmd, *args, **kwargs):
        if not (isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"):
            raise AssertionError(f"这一档只允许执行 git，拦下了：{cmd}")
        return self._real.run(cmd, *args, **kwargs)

    def Popen(self, cmd, *args, **kwargs):
        raise AssertionError(f"这一档禁止 Popen —— 那是 claude / codex 的入口：{cmd}")


@pytest.fixture(autouse=True)
def _git_only(monkeypatch, tmp_path):
    monkeypatch.setattr(rloop, "subprocess", _GitOnlySubprocess(subprocess))
    monkeypatch.setattr(rloop, "RLOOP_HOME", tmp_path / "rloop-home")
    monkeypatch.setattr(rloop, "REGISTRY", tmp_path / "rloop-home" / "registry.json")


def test_sandbox_allows_git_but_blocks_agents(tmp_path):
    """先证明护栏是活的，否则"没起 agent"只是句空话。"""
    repo = make_repo(tmp_path)
    assert rloop.run_git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    with pytest.raises(AssertionError, match="禁止 Popen"):
        rloop.subprocess.Popen(["codex", "exec", "x"])
    with pytest.raises(AssertionError, match="只允许执行 git"):
        rloop.subprocess.run(["claude", "-p", "x"])


# ─────────────────────────── git 夹具 ───────────────────────────


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """一个分支名固定为 main 的空仓库（`git init -b` 要 git 2.28+，这里不依赖它）。"""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    return repo


def commit(repo: Path, msg: str, **files: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").strip()


def scope_args(base=None, commit=None) -> argparse.Namespace:
    return argparse.Namespace(base=base, commit=commit)


def patch_for(repo: Path, args: argparse.Namespace) -> str:
    base, target, _ = rloop.determine_scope(repo, args)
    return rloop.build_scope_patch(repo, base, target)[0]


# ─────────────────── 零参数：逐级回退 ───────────────────


def test_dirty_worktree_is_reviewed_against_head(tmp_path):
    repo = make_repo(tmp_path)
    head = commit(repo, "init", **{"a.txt": "one\n"})
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")

    base, target, desc = rloop.determine_scope(repo, scope_args())

    assert base == head
    assert target is None, "diff 终点必须是工作树，否则 fixer 的改动进不了下一轮"
    assert "uncommitted" in desc
    assert "+two" in rloop.build_scope_patch(repo, base, target)[0]


def test_staged_but_uncommitted_changes_are_in_scope(tmp_path):
    """`git add` 过但没 commit 的改动同样算未定稿，必须送审。"""
    repo = make_repo(tmp_path)
    head = commit(repo, "init", **{"a.txt": "one\n"})
    (repo / "a.txt").write_text("one\nstaged\n", encoding="utf-8")
    git(repo, "add", "-A")

    base, target, _ = rloop.determine_scope(repo, scope_args())
    assert (base, target) == (head, None)
    assert "+staged" in rloop.build_scope_patch(repo, base, target)[0]


def test_untracked_only_worktree_still_has_a_scope(tmp_path):
    """只新增了未跟踪文件时，`git diff` 是空的，范围判定不能因此认为没东西可审。"""
    repo = make_repo(tmp_path)
    head = commit(repo, "init", **{"a.txt": "one\n"})
    (repo / "brand_new.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    base, target, _ = rloop.determine_scope(repo, scope_args())
    assert (base, target) == (head, None)
    assert rloop.scope_diff(repo, base, target).strip() == "", "前提：跟踪文件确实没动"

    patch, untracked, skipped = rloop.build_scope_patch(repo, base, target)
    assert untracked == ["brand_new.py"] and skipped == []
    assert "brand_new.py" in patch and "+    return 1" in patch, \
        "未跟踪文件的内容没进补丁，reviewer 等于什么都没看到"


def test_clean_tree_falls_back_to_branch_merge_base(tmp_path):
    repo = make_repo(tmp_path)
    root = commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "feat", **{"b.txt": "FEATURE\n"})

    base, target, desc = rloop.determine_scope(repo, scope_args())
    assert base == root and target is None
    assert "diverged" in desc
    assert "FEATURE" in rloop.build_scope_patch(repo, base, target)[0]


def test_clean_tree_on_trunk_falls_back_to_last_commit(tmp_path):
    repo = make_repo(tmp_path)
    first = commit(repo, "init", **{"a.txt": "one\n"})
    commit(repo, "second", **{"a.txt": "one\ntwo\n"})

    base, target, desc = rloop.determine_scope(repo, scope_args())
    assert base == first and target is None
    assert "last commit" in desc


def test_single_root_commit_and_clean_tree_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    with pytest.raises(SystemExit):
        rloop.determine_scope(repo, scope_args())


def test_repo_without_commits_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        rloop.determine_scope(repo, scope_args())


def test_review_loops_dir_never_enters_the_scope(tmp_path):
    """loop 自己的产物混进送审 diff 的话，reviewer 会开始审自己的 prompt。"""
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    (repo / rloop.LOOP_DIRNAME).mkdir()
    (repo / rloop.LOOP_DIRNAME / "loop.log").write_text("LOOP_ARTIFACT\n", encoding="utf-8")
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")

    base, target, _ = rloop.determine_scope(repo, scope_args())
    patch, untracked, _ = rloop.build_scope_patch(repo, base, target)
    assert "LOOP_ARTIFACT" not in patch and untracked == []


# ─────────────────── --commit：终点必须被钉死 ───────────────────


def test_commit_scope_excludes_later_commits_and_worktree(tmp_path):
    """回归用例。

    早先 `--commit` 只把父提交存成 base，diff 却一路做到工作树，于是补丁里同时出现
    目标提交、它之后的提交和未提交改动 —— 和「某个 commit 引入的改动」完全不是一回事，
    fixer 会因此去改根本不在范围里的代码。
    """
    repo = make_repo(tmp_path)
    commit(repo, "c1", **{"a.txt": "base\n"})
    target_sha = commit(repo, "c2", **{"target.txt": "TARGET_CHANGE\n"})
    commit(repo, "c3", **{"later.txt": "LATER_CHANGE\n"})
    (repo / "dirty.txt").write_text("WORKTREE_CHANGE\n", encoding="utf-8")

    base, target, desc = rloop.determine_scope(repo, scope_args(commit=target_sha))

    assert target == target_sha, "diff 终点没被钉在目标提交上"
    assert base == git(repo, "rev-parse", f"{target_sha}^").strip()
    assert "excluded" in desc

    patch, untracked, _ = rloop.build_scope_patch(repo, base, target)
    assert "TARGET_CHANGE" in patch
    assert "LATER_CHANGE" not in patch, "后续提交漏进了送审范围"
    assert "WORKTREE_CHANGE" not in patch, "未提交改动漏进了送审范围"
    assert untracked == [], "终点被钉死时，工作区的未跟踪文件不在范围内"


def test_commit_head_with_clean_tree_is_not_pinned(tmp_path):
    """目标就是 HEAD 且工作区干净时，钉不钉终点等价，那就不钉——留给 fixer 可改。"""
    repo = make_repo(tmp_path)
    commit(repo, "c1", **{"a.txt": "base\n"})
    head = commit(repo, "c2", **{"a.txt": "base\nchanged\n"})

    base, target, _ = rloop.determine_scope(repo, scope_args(commit="HEAD"))
    assert target is None
    assert base == git(repo, "rev-parse", f"{head}^").strip()
    assert "+changed" in rloop.build_scope_patch(repo, base, target)[0]


def test_commit_head_with_dirty_tree_is_pinned(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "c1", **{"a.txt": "base\n"})
    head = commit(repo, "c2", **{"a.txt": "base\nchanged\n"})
    (repo / "a.txt").write_text("base\nchanged\nDIRTY\n", encoding="utf-8")

    base, target, _ = rloop.determine_scope(repo, scope_args(commit="HEAD"))
    assert target == head
    assert "DIRTY" not in rloop.build_scope_patch(repo, base, target)[0]


def test_commit_root_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    root = commit(repo, "init", **{"a.txt": "one\n"})
    commit(repo, "second", **{"a.txt": "one\ntwo\n"})
    with pytest.raises(SystemExit):
        rloop.determine_scope(repo, scope_args(commit=root))


def test_unknown_commit_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    with pytest.raises(SystemExit):
        rloop.determine_scope(repo, scope_args(commit="deadbeefdeadbeef"))


# ─────────────────── --base ───────────────────


def test_base_scope_includes_uncommitted_work(tmp_path):
    repo = make_repo(tmp_path)
    root = commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "feat", **{"b.txt": "COMMITTED\n"})
    (repo / "c.txt").write_text("UNCOMMITTED\n", encoding="utf-8")

    base, target, desc = rloop.determine_scope(repo, scope_args(base="main"))
    assert base == root and target is None
    patch = rloop.build_scope_patch(repo, base, target)[0]
    assert "COMMITTED" in patch and "UNCOMMITTED" in patch


def test_base_without_merge_base_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    with pytest.raises(SystemExit):
        rloop.determine_scope(repo, scope_args(base="no-such-ref"))


# ─────────────────── 主干识别：远端 vs 落后的本地 ───────────────────


def clone_with_stale_local_main(tmp_path) -> Path:
    """造一个「本地 main 落后于 origin/main、feature 基于 origin/main」的仓库。"""
    origin = make_repo(tmp_path, "origin")
    commit(origin, "init", **{"a.txt": "one\n"})

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)],
                   check=True, capture_output=True)
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")

    commit(origin, "upstream", **{"upstream.txt": "UPSTREAM_CHANGE\n"})
    git(work, "fetch", "-q", "origin")           # origin/main 前进，本地 main 原地不动

    git(work, "checkout", "-q", "-b", "feature", "origin/main")
    commit(work, "feat", **{"feature.txt": "FEATURE_CHANGE\n"})
    return work


def test_default_branch_keeps_the_remote_ref(tmp_path):
    work = clone_with_stale_local_main(tmp_path)
    assert rloop.default_branch(work) == "origin/main", \
        "把 refs/remotes/origin/HEAD 退化成本地 main，就会拿落后的分支当基准"


def test_stale_local_main_does_not_drag_upstream_commits_into_scope(tmp_path):
    """回归用例：本地 main 落后时，上游提交不能被算成"这次的改动"。"""
    work = clone_with_stale_local_main(tmp_path)
    stale_local = git(work, "rev-parse", "main").strip()
    upstream = git(work, "rev-parse", "origin/main").strip()
    assert stale_local != upstream, "前提没造出来：本地 main 并不落后"

    base, target, desc = rloop.determine_scope(work, scope_args())

    assert base == upstream and target is None
    assert "origin/main" in desc
    patch = rloop.build_scope_patch(work, base, target)[0]
    assert "FEATURE_CHANGE" in patch
    assert "UPSTREAM_CHANGE" not in patch, "上游提交漏进了送审范围"


def test_default_branch_falls_back_to_local_without_a_remote(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    assert rloop.default_branch(repo) == "main"


def test_default_branch_is_none_when_nothing_matches(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    git(repo, "branch", "-m", "main", "some-topic")
    assert rloop.default_branch(repo) is None


# ─────────────────── 未跟踪文件补丁的边角 ───────────────────


def test_untracked_patch_handles_binary_empty_and_odd_names(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (repo / "empty.txt").write_text("", encoding="utf-8")
    (repo / "a file with spaces.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "-leading-dash.txt").write_text("dash\n", encoding="utf-8")

    patch, untracked, skipped = rloop.build_scope_patch(repo, "HEAD", None)

    assert skipped == []
    assert set(untracked) == {"bin.dat", "empty.txt", "a file with spaces.py",
                              "-leading-dash.txt"}
    assert "Binary files" in patch and "bin.dat" in patch
    assert "empty.txt" in patch
    assert "+x = 1" in patch
    assert "+dash" in patch


def test_untracked_patch_caps_the_number_of_files(tmp_path, monkeypatch):
    monkeypatch.setattr(rloop, "UNTRACKED_MAX_FILES", 2)
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    for i in range(5):
        (repo / f"n{i}.txt").write_text(f"content {i}\n", encoding="utf-8")

    patch, untracked, skipped = rloop.build_scope_patch(repo, "HEAD", None)
    assert len(untracked) == 5
    assert len(skipped) == 3, "超出上限的文件必须被如实点名，不能悄悄丢掉"
    assert sum(f"content {i}" in patch for i in range(5)) == 2


def test_untracked_patch_caps_total_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(rloop, "UNTRACKED_MAX_BYTES", 200)
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.txt": "one\n"})
    (repo / "small.txt").write_text("small\n", encoding="utf-8")
    (repo / "huge.txt").write_text("H" * 5000 + "\n", encoding="utf-8")

    patch, _, skipped = rloop.build_scope_patch(repo, "HEAD", None)
    assert "huge.txt" in skipped
    assert "+small" in patch


# ─────────────────── 范围如何流进 context pack ───────────────────


def make_state_loop(tmp_path, repo: Path, base: str, target: str | None) -> rloop.Loop:
    loop = rloop.Loop(tmp_path / "loop")
    loop.root.mkdir(parents=True, exist_ok=True)
    loop.save({
        "id": "scope-test", "project": str(repo), "focus": None,
        "diff_base": base, "diff_target": target, "scope_desc": "测试范围",
        "reviewer": "codex", "fixer": "claude",
        "max_rounds": 3, "min_score": 8.0, "history": [],
    })
    return loop


def test_context_pack_writes_the_pinned_patch(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "c1", **{"a.txt": "base\n"})
    target_sha = commit(repo, "c2", **{"target.txt": "TARGET_CHANGE\n"})
    commit(repo, "c3", **{"later.txt": "LATER_CHANGE\n"})

    base = git(repo, "rev-parse", f"{target_sha}^").strip()
    loop = make_state_loop(tmp_path, repo, base, target_sha)
    pack = rloop.build_context_pack(loop, 1)

    patch = (loop.round_dir(1) / "diff.patch").read_text(encoding="utf-8")
    assert "TARGET_CHANGE" in patch and "LATER_CHANGE" not in patch
    # 权限那节归 test_the_pack_tells_the_reviewer_what_it_can_actually_do 守，
    # 这里只确认它确实被拼进来了 —— 档位由 loop 的 verify 决定，不该在这条里写死。
    assert "## 你的权限" in pack


def test_context_pack_tells_the_reviewer_untracked_content_is_inline(tmp_path):
    repo = make_repo(tmp_path)
    head = commit(repo, "c1", **{"a.txt": "base\n"})
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")

    loop = make_state_loop(tmp_path, repo, head, None)
    pack = rloop.build_context_pack(loop, 1)

    assert "未跟踪的新文件" in pack and "内联进补丁" in pack
    assert "+x = 1" in (loop.round_dir(1) / "diff.patch").read_text(encoding="utf-8")


def test_the_pack_tells_the_reviewer_what_it_can_actually_do(tmp_path):
    """权限说明必须和这一轮真实的能力一致。

    说错的代价是具体的：以为自己只读，它就不去跑那些本可以跑的命令，
    validation_commands 全是 not_run；以为自己能跑，它会声称跑过根本跑不了
    的东西 —— 那种假证据比没有证据更坏。
    """
    repo = make_repo(tmp_path)
    head = commit(repo, "c1", **{"a.txt": "base\n"})
    (repo / "a.txt").write_text("base\nmore\n", encoding="utf-8")
    loop = make_state_loop(tmp_path, repo, head, None)

    for agent in ("claude", "codex"):
        loop.update(reviewer=agent, verify=False)
        ro = rloop.build_context_pack(loop, 1)
        assert "只读" in ro or "执行不了 shell" in ro, f"{agent} 只读模式没说清"
        assert "not_run" in ro, f"{agent} 没被告知跑不了的命令怎么记"

        loop.update(verify=True)
        rw = rloop.build_context_pack(loop, 1)
        assert "绝不能改这个项目的代码" in rw, f"{agent} 放开后没给禁令"
        assert "封顶" in rw, f"{agent} 没被提醒打分的硬规则没变"
        # 越界的后果按 agent 说实话：codex 那边真会作废（有事件流可查），claude
        # 那边扫不到执行记录、触发不了作废，把"会作废"照抄给它就是拿空话当保险。
        if agent == "codex":
            assert "整轮作废" in rw
        else:
            assert "触发不了" in rw and "整轮作废" not in rw, "对 claude 许了一个执行不了的诺"


# ─────────── 工作区指纹（要真的跑 git）───────────


def test_the_ledger_is_excluded_from_the_fingerprint(tmp_path):
    """.review-loops 每轮都在写，把它算进指纹会次次误报。"""
    import subprocess as sp
    sp.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    for c in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        sp.run(["git", "-C", str(tmp_path), *c], check=True, capture_output=True)
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-qm", "i"], check=True, capture_output=True)

    before = rloop.workspace_fingerprint(tmp_path)
    (tmp_path / rloop.LOOP_DIRNAME).mkdir()
    (tmp_path / rloop.LOOP_DIRNAME / "loop.json").write_text("{}", encoding="utf-8")
    assert rloop.fingerprint_changed(before, rloop.workspace_fingerprint(tmp_path)) == [], \
        "账本目录让指纹误报了"

    (tmp_path / "a.py").write_text("y\n", encoding="utf-8")
    assert rloop.fingerprint_changed(before, rloop.workspace_fingerprint(tmp_path)), \
        "真改了源码却没被抓到"


def test_an_untracked_file_edited_in_place_still_moves_the_fingerprint(tmp_path):
    """reviewer 自己证伪的假设（0.4.0 第二轮，medium）。

    未跟踪文件一度只记大小和 mtime，理由是"改内容不可能既不改大小又不动 mtime"。
    它把同长度的 aaaa 改成 bbbb、再 os.utime 复原时间戳 —— 四个维度全静止。
    保留时间戳的解包/复制工具也会无意中撞上同一条路。所以那些会内联进送审补丁的
    未跟踪文件必须算内容哈希。
    """
    import os

    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    target = repo / "new.py"
    target.write_text("aaaa", encoding="utf-8")
    before = rloop.workspace_fingerprint(repo)
    st = target.stat()

    target.write_text("bbbb", encoding="utf-8")          # 同长度
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # 时间戳复原

    after = rloop.workspace_fingerprint(repo)
    assert rloop.fingerprint_changed(before, after) == ["untracked"], "同长度改写没被指纹看见"
    assert rloop.tampered_dimensions(before, after) == ["untracked"]


def test_binary_changes_do_not_silently_drop_the_diff_dimension(tmp_path):
    """自审抓到的漏判，是这批里最要命的一个。

    `git diff HEAD` 一度用 text=True 取输出：仓库里只要有一个二进制改动或非
    UTF-8 编码的源文件，解码就抛 UnicodeDecodeError，被 suppress 一吞，`diff`
    这个键干脆不存在 —— 而「缺的键不参与比较」意味着整条最重要的维度**静默消失**，
    reviewer 改任何已跟踪文件都抓不到。
    """
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "bin")

    before = rloop.workspace_fingerprint(repo)
    assert "diff" in before, "有二进制文件在，diff 维度就整个丢了"
    assert len(before) == len(rloop.FINGERPRINT_KEYS)

    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00BINARY-CHANGED")
    assert rloop.tampered_dimensions(before, rloop.workspace_fingerprint(repo)) == ["diff"]


def test_a_new_untracked_file_does_not_reclassify_its_neighbours(tmp_path):
    """算不算 hash 只看文件自己有多大，不看它排第几。

    先前按名次切（前 100 个算 hash），于是 reviewer 新建一个排序靠前的文件就能把
    原来的第 100 名挤成第 101 名 —— 那条记录从 sha256 变成 mtime，内容一个字节
    没动却看着像被改了。这是一条纯粹由邻居造成的误作废。
    """
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    for i in range(rloop.UNTRACKED_MAX_FILES + 5):
        (repo / f"m{i:03d}.txt").write_text(f"{i}\n", encoding="utf-8")

    before = rloop.workspace_fingerprint(repo)
    (repo / "AAA-first.txt").write_text("挤到最前面\n", encoding="utf-8")
    after = rloop.workspace_fingerprint(repo)

    assert rloop.tampered_dimensions(before, after) == [], "新增文件把邻居连坐了"
    assert rloop.new_paths(before, after) == ["AAA-first.txt"]


def test_a_tab_in_a_filename_does_not_collapse_the_untracked_map(tmp_path):
    """git 的 -z 输出不转义路径，路径里的 TAB 会把「摘要\t路径」的解析切错位。"""
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    (repo / "we\tird.txt").write_text("1", encoding="utf-8")
    (repo / "we\tirdX.txt").write_text("1", encoding="utf-8")

    before = rloop.workspace_fingerprint(repo)
    (repo / "we\tird.txt").write_text("22", encoding="utf-8")

    assert rloop.tampered_dimensions(before, rloop.workspace_fingerprint(repo)) == ["untracked"], \
        "文件名里的 TAB 让改动漏报了"


def test_a_symlink_to_a_character_device_does_not_hang_the_fingerprint(tmp_path):
    """指纹没有超时兜着 —— 一个指向 /dev/zero 的未跟踪符号链接就能让它永远读下去。"""
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    (repo / "trap").symlink_to("/dev/zero")

    fp = rloop.workspace_fingerprint(repo)          # 不许挂在这儿
    assert len(fp) == len(rloop.FINGERPRINT_KEYS)
    assert any("trap" in row for row in fp["untracked"]), "符号链接该被记下来，只是不读它"


def test_a_file_named_like_the_ledger_is_not_excluded(tmp_path):
    """账本排除按路径段判断，不是子串匹配。

    先前 `LOOP_DIRNAME not in x` 会把 `notes-about-.review-loops.md` 这种普通文件
    整个排除在指纹之外 —— reviewer 改它不留痕迹，而它照样会进送审补丁。
    """
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    decoy = repo / f"notes-about-{rloop.LOOP_DIRNAME}.md"
    decoy.write_text("一份普通文档\n", encoding="utf-8")

    before = rloop.workspace_fingerprint(repo)
    assert any(decoy.name in row for row in before["untracked"]), "名字像账本的普通文件被漏掉了"

    decoy.write_text("被改过\n", encoding="utf-8")
    assert rloop.tampered_dimensions(before, rloop.workspace_fingerprint(repo)) == ["untracked"]

    # 真的账本目录仍然要排除掉 —— 否则每轮都误报
    (repo / rloop.LOOP_DIRNAME).mkdir()
    (repo / rloop.LOOP_DIRNAME / "loop.log").write_text("x", encoding="utf-8")
    after = rloop.workspace_fingerprint(repo)
    assert not any(rloop.LOOP_DIRNAME + "/" in row for row in after["untracked"])


def test_a_non_repo_cannot_be_fingerprinted_and_that_is_visible(tmp_path):
    """指纹这道保险是**失败开放**的，所以"拍没拍全"必须能被看出来。

    不是 git 仓库、git 挂了，指纹就拍不全；这时 reviewer 照样带着写权限跑，
    只是没人核对它。run_reviewer 靠维度数少于 FINGERPRINT_KEYS 判断这件事并
    在 loop.log 里明说 —— 前提是拍不全时维度数真的会少。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    fp = rloop.workspace_fingerprint(plain)

    assert len(fp) < len(rloop.FINGERPRINT_KEYS), f"非 git 目录居然拍出了完整指纹：{fp}"
    assert rloop.fingerprint_changed(fp, rloop.workspace_fingerprint(plain)) == []

    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n"})
    assert len(rloop.workspace_fingerprint(repo)) == len(rloop.FINGERPRINT_KEYS), \
        "正常仓库都拍不全的话，「拍不全」这个信号就没意义了"


def test_what_running_tests_leaves_behind_does_not_read_as_tampering(tmp_path):
    """放开写权限就是为了让 reviewer 跑得动测试，那测试产物不能反过来判它越界。

    两条路各走一遍：被 gitignore 的产物压根不该进指纹；没被 ignore 的会让 status
    动一下，但 head / diff 不动 —— 那是"多了文件"，不是"改了代码"。
    """
    repo = make_repo(tmp_path)
    commit(repo, "init", **{"a.py": "x\n", ".gitignore": "__pycache__/\n"})
    before = rloop.workspace_fingerprint(repo)

    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    assert rloop.fingerprint_changed(before, rloop.workspace_fingerprint(repo)) == [], \
        "被 gitignore 的测试产物惊动了指纹"

    (repo / "junk.log").write_text("pytest 写的\n", encoding="utf-8")
    after = rloop.workspace_fingerprint(repo)
    assert rloop.fingerprint_changed(before, after) == ["status", "untracked"]
    assert rloop.tampered_dimensions(before, after) == [], "多出一个日志文件被判成了改代码"
    assert rloop.new_paths(before, after) == ["junk.log"]

    # 但改这个未跟踪文件的内容就是另一回事了 —— 它会进送审补丁
    (repo / "junk.log").write_text("被 reviewer 改过\n", encoding="utf-8")
    assert rloop.tampered_dimensions(after, rloop.workspace_fingerprint(repo)) == ["untracked"]
