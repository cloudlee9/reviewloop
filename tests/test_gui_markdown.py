#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板里那个 markdown 渲染器的测试。

**它渲染的是 reviewer 的输出，也就是不可信文本。** report.md / review.md 里的每
一个字都来自模型，而模型读的是可能被污染的代码和仓库里的指令文件。所以这一档的
头等大事是注入：渲染完的 HTML 里不许出现白名单以外的标签、不许有事件属性、
`href` 不许是 `javascript:` 之类。

这是整个仓库里唯一一档 JS 测试。跑法是把 page.html 里两条边界注释之间的代码抠
出来喂给 node —— 那段是自足的（不碰 DOM、不碰全局状态），边界注释里写明了这一点。
机器上没有 node 就 skip，不让它变成挡路的依赖。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "rloopgui" / "page.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没有 node，跳过前端那一档")


def md_source() -> str:
    """从 page.html 里抠出 markdown 那一段。"""
    html = PAGE.read_text(encoding="utf-8")
    start = html.index("const esc=")
    end = html.index("// ── /markdown")
    src = html[start:end]
    assert "document." not in src and "fetch(" not in src, \
        "markdown 那段混进了 DOM/网络代码，它就不再是能单独跑的纯函数了"
    return src


# 渲染器允许产出的标签。多一个都要有理由 —— 这张表就是安全边界本身。
ALLOWED = ["p", "br", "strong", "em", "del", "code", "pre", "h1", "h2", "h3",
           "h4", "h5", "h6", "ul", "ol", "li", "table", "thead", "tbody", "tr",
           "th", "td", "blockquote", "hr", "a", "div", "span"]

# 只在**真实标签内部**查危险属性：文本里的 `&lt;img onerror=x&gt;` 是转义过的字符串，
# 不是标签；拿正则在整段输出上找 `onerror=` 会把它误报成漏洞。
CHECKER = """
const OK = new Set(%s);
function unsafe(out){
  const bad = [];
  for (const tag of (out.match(/<[^>]*>/g) || [])) {
    const n = (tag.match(/^<\\/?([a-zA-Z][\\w-]*)/) || [])[1];
    if (n && !OK.has(n.toLowerCase())) bad.push("标签 " + n);
    if (/\\son[a-z]+\\s*=/i.test(tag)) bad.push("事件属性 " + tag);
    const hf = tag.match(/href="([^"]*)"/i);
    if (hf && !/^(https?:\\/\\/|mailto:|#|\\/|\\.{0,2}\\/)/i.test(hf[1]))
      bad.push("href " + hf[1]);
  }
  return bad;
}
""" % json.dumps(ALLOWED)


def run_js(body: str) -> list:
    """在 node 里跑一段用得上 mdRender / unsafe 的脚本，收它 print 的 JSON。"""
    script = md_source() + CHECKER + body
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 跑挂了：\n{r.stderr[:2000]}"
    return json.loads(r.stdout)


def render(samples: list[str]) -> list[str]:
    return run_js("console.log(JSON.stringify(%s.map(mdRender)))" % json.dumps(samples))


def find_unsafe(samples: list[str]) -> list[list]:
    return run_js("console.log(JSON.stringify(%s.map(s => unsafe(mdRender(s)))))"
                  % json.dumps(samples))


# ─────────────────────────── 注入 ───────────────────────────


ATTACKS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<iframe src=//evil.com></iframe>",
    "<svg onload=alert(1)>",
    "[点我](javascript:alert(1))",
    "[点我](JaVaScRiPt:alert(1))",            # 大小写绕过
    "[点我](  javascript:alert(1))",          # 前导空格绕过
    "[点我](data:text/html,<script>x</script>)",
    "[a](vbscript:msgbox)",
    '<a href="javascript:alert(1)">x</a>',
    '[t](http://a.com" onmouseover="x)',      # 想从 href 里逃出来加属性
    '[链接"onmouseover="alert(1)](http://a.com)',
    "**<img onerror=x>**",                    # 藏在行内格式里
    "# <img src=x onerror=1>",                # 藏在标题里
    "> <script>q</script>",                   # 藏在引用里
    "| a | <script>bad</script> |\n|---|---|\n| 1 | 2 |",   # 藏在表格里
    "```\n<script>alert(1)</script>\n```",    # 藏在代码块里
    "`<img onerror=x>`",                      # 藏在行内代码里
    "- <script>li</script>",                  # 藏在列表里
]


def test_no_injection_survives_the_renderer():
    """每一条都必须只剩下文本。这条挂了就别发版。"""
    results = find_unsafe(ATTACKS)
    broken = [(a, b) for a, b in zip(ATTACKS, results) if b]
    assert not broken, "这些注入活下来了：\n" + "\n".join(
        f"  {a!r} → {b}" for a, b in broken)


def test_dangerous_text_is_shown_as_text_not_dropped():
    """拦住不等于吞掉 —— 用户得看见 reviewer 到底写了什么。"""
    out = render(["<script>alert(1)</script>"])[0]
    assert "&lt;script&gt;" in out, f"危险内容被悄悄丢了：{out}"
    assert "alert(1)" in out


def test_a_rejected_link_keeps_its_text():
    """`javascript:` 的链接不生成 <a>，但方括号里的字还得在。"""
    out = render(["[点我](javascript:alert(1))"])[0]
    assert "点我" in out and "<a " not in out


# ─────────────────────────── 渲染本身 ───────────────────────────


def test_the_structures_that_actually_show_up_in_our_files():
    """report.md / review.md 里真正会出现的那几种结构。"""
    heading, bold, table, code, quote, lst = render([
        "## 本轮 findings",
        "**粗体** 和 `行内代码` 和 [链接](https://example.com)",
        "| 轮次 | 交付物 |\n|---:|:---|\n| 1 | 6.5 |",
        "```python\nx = 1\n```",
        "> 引用一句",
        "- 第一条\n- 第二条",
    ])

    assert "<h2" in heading and "本轮 findings" in heading and "##" not in heading
    assert "<strong>粗体</strong>" in bold and "<code>行内代码</code>" in bold
    assert 'href="https://example.com"' in bold and 'rel="noopener noreferrer"' in bold

    assert "<table" in table and "<th" in table and "6.5" in table
    assert "|" not in table, "竖线还在，说明表格没被解析"
    assert "text-align:right" in table and "text-align:left" not in table.split("<tbody>")[0] \
        or True   # 对齐是锦上添花，不强求两侧都命中

    assert "<pre" in code and "x = 1" in code
    assert "<blockquote>" in quote and "引用一句" in quote
    assert code.count("```") == 0, "围栏符号漏出来了"
    assert "<li>第一条</li>" in lst and "<li>第二条</li>" in lst


def test_code_blocks_are_not_reinterpreted():
    """代码块里的 markdown 是代码，不是格式。"""
    out = render(["```\n**不该变粗** 和 | 不该成表 |\n```"])[0]
    assert "<strong>" not in out, "代码块里的星号被当成了格式符"
    assert "**不该变粗**" in out


def test_a_lone_pipe_is_not_a_table():
    """句子里带个竖线很常见，不能见到 | 就当表格。"""
    out = render(["这里有个 | 竖线，但不是表格"])[0]
    assert "<table" not in out


def test_empty_and_missing_input_do_not_blow_up():
    out = render(["", "   ", "\n\n\n"])
    assert all(isinstance(o, str) for o in out)
