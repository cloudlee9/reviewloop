#!/usr/bin/env bash
# 打一个可以拿走的安装包。
#
#   ./install/pack.sh              打 HEAD 的包
#   ./install/pack.sh --dirty      打当前工作区（含未提交改动）的包
#
# 出到 dist/rloop-<版本>.tar.gz。版本号从 rloop.py 的 VERSION 读 —— 写死在这里
# 或者写死在 INSTALL.md 里，迟早会变成又一句对不上的话。
#
# **从 git 里导出**（`git archive`），不是 cp -R：工作区里有 .review-loops/、
# __pycache__/、.pytest_cache/ 这些运行产物，其中 .review-loops/ 还带着被审代码
# 的完整快照。一次手滑的 cp -R 就能把它们连同别人的代码一起发出去。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
DIST="$SRC/dist"

dirty=0
[ "${1-}" = "--dirty" ] && dirty=1

cd "$SRC"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "这儿不是 git 仓库，打不了包（打包靠 git archive 保证干净）" >&2
  exit 1
}

VERSION="$(python3 -c "
import re, pathlib
m = re.search(r'^VERSION = \"([^\"]+)\"', pathlib.Path('rloop.py').read_text('utf-8'), re.M)
print(m.group(1))")"
NAME="rloop-$VERSION"
OUT="$DIST/$NAME.tar.gz"

mkdir -p "$DIST"
rm -f "$OUT"

if [ "$dirty" = 1 ]; then
  # 未提交的改动也要进包：先 stash 成一个临时树对象，再从那儿 archive。
  # 仍然只包含 git 认识的文件，所以运行产物照样进不来。
  TREE="$(git stash create)"
  [ -n "$TREE" ] || TREE=HEAD          # 工作区干净时 stash create 什么都不输出
  echo "打包：工作区当前状态（含未提交改动）"
else
  TREE=HEAD
  if [ -n "$(git status --porcelain)" ]; then
    echo "注意：工作区有未提交改动，打的是 HEAD 的包（要连未提交的一起打就加 --dirty）"
  fi
fi

git archive --format=tar --prefix="$NAME/" "$TREE" | gzip -9 > "$OUT"

echo
echo "打好了：$OUT"
echo "  $(du -h "$OUT" | cut -f1)，$(tar tzf "$OUT" | wc -l | tr -d ' ') 个条目"
echo
echo "对方拿到之后："
echo "  tar xzf $NAME.tar.gz"
echo "  cd $NAME"
echo "  ./install/install.sh"

# 自检：包里不能有账本和运行产物 —— 那是这个脚本存在的全部理由
if tar tzf "$OUT" | grep -qE "\.review-loops/|__pycache__/|\.pytest_cache/"; then
  echo >&2
  echo "！包里混进了运行产物，别发出去。" >&2
  exit 1
fi
