#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切分的保镖。**这个文件不许 skip。**

`rloopgui/` 和核心之间只有一条通路：起 rloop 子进程、读它 stdout 上的 JSON。
这里挡的是三种腐烂：

1. `import rloop` —— 最明显的一种，也最容易被发现。
2. **绕过契约直接读文件** —— 真正危险的那种。负载里既然给了绝对路径，
   「我 `Path(x).read_text()` 一下更省事」是每个人都会动的念头，而任何
   import 检查都抓不到它。所以这里直接扫 `loop.json` / `.review-loops` /
   `round-` 这些字样。
3. **面板长出写能力** —— 契约里没有写产物的 verb，尤其没有写 `response.md` 的。
   这不是遗漏，是「面板是观察者」的执行方式：能力不存在，任何语言写的面板
   都长不出「处理 findings」的按钮。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUI_DIR = REPO_ROOT / "rloopgui"

# 只有它一个可以知道 rloop 存在
THE_ONLY_FILE_THAT_KNOWS = "client.py"


def gui_python_files() -> list[Path]:
    if not GUI_DIR.is_dir():
        pytest.skip("rloopgui/ 还不存在")
    return sorted(GUI_DIR.rglob("*.py"))


def code_text(path: Path) -> str:
    """文件里**真正会执行**的那部分文本：字符串字面量 + 标识符 + 属性名。

    注释和 docstring 排除在外。理由：这些禁令本身需要在注释里写清楚
    （「不许直接读 loop.json」），扫全文的话，解释禁令的那句话自己就先违规了。
    危险的是代码里的 `"loop.json"`，不是文档里的。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            docs.add(id(node.body[0].value))

    parts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            parts.append(getattr(node, "module", "") or "")
            parts.extend(a.name for a in node.names)
    return "\n".join(parts)


# 故意不含 `replace`：`Path.replace` 确实是写，但 `str.replace` 太常见
# （页面注入 token 就要用），光看方法名分不开，误报的代价比漏报大。
# 真正的写入口是 write_text / open(mode=w) / mkdir，那几个盯住就够了。
WRITE_CALLS = {"write_text", "write_bytes", "mkdir", "makedirs", "unlink",
               "rmtree", "remove", "touch"}


def writes_to_disk(path: Path) -> set:
    """这个文件里所有会往磁盘写东西的调用。

    只禁写，不禁读 —— 面板要读自己的 `page.html`，那是它自己的资源。
    `open()` 单看名字判断不了，得看 mode：默认是读，带 w/a/x/+ 才是写。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else \
            fn.attr if isinstance(fn, ast.Attribute) else ""
        if name in WRITE_CALLS:
            found.add(name)
        elif name == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if any(c in mode for c in "wax+"):
                found.add(f"open(mode={mode!r})")
    return found


def test_the_panel_never_imports_the_core():
    """一条 `import rloop` 都不许有。

    有了它，核心的任何重构都会牵动面板，「切分开」就只剩个说法。
    """
    for f in gui_python_files():
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] != "rloop", f"{f.name} 里 import 了 rloop"
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "rloop", \
                    f"{f.name} 里 from rloop import"


def test_the_panel_never_touches_the_ledger_behind_the_contract():
    """除 client.py 外，任何文件都不许出现内部布局的字样。

    这条比 import 检查狠，也更要紧：`Path(summary["root"]) / "loop.json"`
    读起来毫无违和感，却把 loop.json 的内部结构变成了面板的依赖 —— 核心从此
    不能改自己的状态文件。
    """
    forbidden = ("loop.json", "registry.json", ".review-loops", "round-",
                 "review.json", "progress.ndjson")
    for f in gui_python_files():
        if f.name == THE_ONLY_FILE_THAT_KNOWS:
            continue
        text = code_text(f)
        for word in forbidden:
            assert word not in text, (
                f"{f.name} 里出现了 `{word}` —— 内部布局只能由 client.py 经契约拿，"
                f"直接拼路径会把核心的实现细节焊死成面板的依赖")


def test_even_the_client_only_speaks_through_the_cli():
    """client.py 可以知道 rloop 存在，但也只能靠起进程说话。"""
    text = code_text(GUI_DIR / THE_ONLY_FILE_THAT_KNOWS)
    assert "subprocess" in text, "client 不起子进程，那它怎么跟核心说话？"
    for word in ("loop.json", ".review-loops", "registry.json"):
        assert word not in text, f"client.py 也不该直接碰 `{word}`，它有 api"


def test_the_panel_has_no_way_to_write_an_artifact():
    """面板不许有写产物的能力 —— 尤其是 response.md。

    处理 findings 归有上下文的那一方（开发会话里的 skill）。面板上按下去的
    必然是个没有上下文的陌生会话，不知道这些代码为什么写成现在这样。
    """
    for f in gui_python_files():
        assert "response.md" not in code_text(f), (
            f"{f.name} 提到了 response.md。findings 的出口是「复制为 markdown」"
            f"加一个路径，不是面板自己写")
        bad = writes_to_disk(f)
        assert not bad, f"{f.name} 里有写磁盘的调用：{sorted(bad)}"


def test_the_contract_declares_no_write_verb():
    """连契约里都不该有写产物的 verb —— 别的语言写的面板也长不出这个按钮。"""
    sys.path.insert(0, str(REPO_ROOT))
    import rloop
    methods = set(rloop.api_meta()["methods"])
    forbidden = {"write", "respond", "response", "save", "edit", "fix"}
    assert not (methods & forbidden), f"契约里出现了写能力：{methods & forbidden}"


def test_the_client_is_the_only_place_that_locates_the_core():
    """找 rloop 的逻辑只有一处，别处不许再猜路径。"""
    for f in gui_python_files():
        if f.name == THE_ONLY_FILE_THAT_KNOWS:
            continue
        text = code_text(f)
        assert "RLOOP_BIN" not in text, f"{f.name} 自己找核心了，该走 client.find_core"
        assert "shutil.which" not in text, f"{f.name} 自己找核心了"
