#!/usr/bin/env bash
# rloop 安装脚本。
#
#   ./install.sh              装核心 + 两边的 skill（有哪个装哪个）
#   ./install.sh --codex      只装到 Codex
#   ./install.sh --claude     只装到 Claude Code
#   ./install.sh --core-only  只装命令，不碰任何 skill
#   ./install.sh --uninstall  卸载
#
# 装到哪：
#   ~/.local/lib/rloop/       代码本体（rloop.py + rloopgui/）
#   ~/.local/bin/rloop        指向上面的符号链接
#   ~/.claude/skills/rloop/   Claude 侧的 skill（reviewer 用 codex）
#   ~/.codex/skills/rloop/    Codex  侧的 skill（reviewer 用 claude）
#
# 账本存在各自项目的 .review-loops/ 下，全局注册表在 ~/.rloop/。卸载不动它们。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"

LIB="$HOME/.local/lib/rloop"
BIN="$HOME/.local/bin"
CLAUDE_SKILL="$HOME/.claude/skills/rloop"
CODEX_SKILL="$HOME/.codex/skills/rloop"

want_claude=0
want_codex=0
core_only=0
uninstall=0

case "${1:-}" in
  --claude)    want_claude=1 ;;
  --codex)     want_codex=1 ;;
  --core-only) core_only=1 ;;
  --uninstall) uninstall=1 ;;
  "")          want_claude=1; want_codex=1 ;;   # 有哪个装哪个，下面再判断
  *)           echo "不认识的参数：$1"; sed -n '2,10p' "$0"; exit 2 ;;
esac

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }

# ─────────── 卸载 ───────────
if [ "$uninstall" = 1 ]; then
  echo "卸载 rloop"
  rm -f  "$BIN/rloop"       && ok "删掉 $BIN/rloop"
  rm -rf "$LIB"             && ok "删掉 $LIB"
  rm -rf "$CLAUDE_SKILL"    && ok "删掉 $CLAUDE_SKILL"
  rm -rf "$CODEX_SKILL"     && ok "删掉 $CODEX_SKILL"
  echo
  say "没动的：~/.rloop/（全局注册表）和各项目下的 .review-loops/（账本和历史）"
  say "要一并清掉就自己 rm -rf ~/.rloop 和项目里的 .review-loops"
  exit 0
fi

# ─────────── 依赖检查 ───────────
echo "检查依赖"

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  warn "找不到 Python 3.11+。rloop 用了 3.11 才有的语法（X | None），装一个再来。"
  exit 1
fi
ok "Python $("$PY" -c 'import platform;print(platform.python_version())')  →  $PY"

command -v git >/dev/null 2>&1 || { warn "找不到 git —— rloop 审的是 git 里的改动"; exit 1; }
ok "git $(git --version | awk '{print $3}')"

have_codex=0; have_claude=0
command -v codex  >/dev/null 2>&1 && { have_codex=1;  ok "codex  $(codex --version 2>/dev/null | head -1)"; }
command -v claude >/dev/null 2>&1 && { have_claude=1; ok "claude $(claude --version 2>/dev/null | head -1)"; }
if [ "$have_codex" = 0 ] && [ "$have_claude" = 0 ]; then
  warn "codex 和 claude 一个都没有。rloop 要靠它们当 reviewer，装一个再来。"
  exit 1
fi

# 没显式指定时，按机器上实际有什么来决定装哪边的 skill
if [ "$core_only" = 0 ] && [ "${1:-}" = "" ]; then
  [ -d "$HOME/.claude" ] || want_claude=0
  [ -d "$HOME/.codex" ]  || want_codex=0
fi

# ─────────── 装核心 ───────────
echo
echo "安装"
mkdir -p "$LIB" "$BIN"
rm -rf "$LIB/rloopgui"
cp "$SRC/rloop.py" "$LIB/rloop.py"
cp -R "$SRC/rloopgui" "$LIB/rloopgui"
chmod +x "$LIB/rloop.py"
ln -sf "$LIB/rloop.py" "$BIN/rloop"
ok "$LIB  ←  rloop.py + rloopgui/"
ok "$BIN/rloop  →  $LIB/rloop.py"

# rloop.py 的 shebang 是 /usr/bin/env python3；那个 python3 要够新
if ! /usr/bin/env python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  warn "PATH 上的 python3 低于 3.11，直接敲 rloop 会失败。"
  say  "改用 $PY $LIB/rloop.py，或者把新版 python3 放到 PATH 前面。"
fi

# ─────────── 装 skill ───────────
if [ "$core_only" = 0 ]; then
  if [ "$want_claude" = 1 ]; then
    mkdir -p "$CLAUDE_SKILL"
    cp "$HERE/SKILL.claude.md" "$CLAUDE_SKILL/SKILL.md"
    ok "$CLAUDE_SKILL/SKILL.md   （reviewer 用 codex）"
    [ "$have_codex" = 1 ] || warn "但这台机器上没有 codex，Claude 那边审不起来"
  fi
  if [ "$want_codex" = 1 ]; then
    mkdir -p "$CODEX_SKILL"
    cp "$HERE/SKILL.codex.md" "$CODEX_SKILL/SKILL.md"
    ok "$CODEX_SKILL/SKILL.md    （reviewer 用 claude）"
    [ "$have_claude" = 1 ] || warn "但这台机器上没有 claude，Codex 那边审不起来"
  fi
fi

# ─────────── 自检 ───────────
echo
echo "自检"
# 一律走**刚装好的那个**绝对路径，不要 command -v —— 机器上可能早就有一个
# 别处的 rloop 在 PATH 上，那样自检验的是它，跟这次装的没关系。
RL="$BIN/rloop"
if ok_ver="$("$RL" --version 2>/dev/null)"; then
  ok "rloop ${ok_ver##* }"
else
  warn "$RL 跑不起来"; exit 1
fi
"$RL" api meta >/dev/null 2>&1 && ok "api 契约应答正常" || warn "rloop api meta 没跑通"
( cd /tmp && "$RL" list >/dev/null 2>&1 ) && ok "在任意目录下都能跑" \
  || warn "换个目录就跑不了"
# 面板要能从别的目录找到自己同级的 rloopgui/（安装后最容易坏的一条）
if ( cd /tmp && "$RL" web --no-open --port 0 >/dev/null 2>&1 & sleep 3; kill %1 2>/dev/null ); then
  ok "面板能起来"
else
  warn "rloop web 起不来"
fi

if ! command -v rloop >/dev/null 2>&1; then
  echo
  warn "$BIN 不在 PATH 上。加这一行到 ~/.zshrc 或 ~/.bashrc："
  say  "export PATH=\"\$HOME/.local/bin:\$PATH\""
elif [ "$(command -v rloop)" != "$RL" ]; then
  echo
  warn "PATH 上的 rloop 是 $(command -v rloop)，不是刚装的这个。"
  say  "两个都留着的话，敲 rloop 走的是前者。"
fi

echo
echo "装好了。"
[ "$want_codex" = 1 ]  && say "Codex  里说「审一下」，它会用 claude 审"
[ "$want_claude" = 1 ] && say "Claude 里说「审一下」，它会用 codex 审"
say "看面板：rloop web"
say "卸载：  $HERE/install.sh --uninstall"
