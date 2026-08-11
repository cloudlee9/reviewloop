# rloop 安装说明

用另一个模型独立审你刚写的代码，来回循环到达标或熔断。

- **在 Codex 里**说「审一下」→ 起一个无头的 **claude** 审你的改动
- **在 Claude Code 里**说「审一下」→ 起一个无头的 **codex** 审你的改动

被审的那个会话拿到结构化 findings，逐条处理、写回应，然后再跑一轮。
reviewer 每轮会对上一轮的每条 finding 给出 `fixed` / `partially_fixed` /
`not_fixed` / `rebutted_and_accepted`，所以糊弄不过去。

## 装之前

| 要什么 | 干嘛用 |
|---|---|
| Python **3.11+** | rloop 本体。用了 3.11 才有的 `X \| None` 语法 |
| git | 审的是 git 里的改动，没有 git 就没有范围 |
| `codex` 和/或 `claude` CLI | 当 reviewer。至少有一个；要两边互审就两个都要 |

装脚本会自己查这三样，缺了会说清楚缺哪个。

## 装

```bash
tar xzf rloop-0.4.0.tar.gz
cd rloop-0.4.0
./install/install.sh
```

不带参数 = 机器上有 `~/.codex` 就装 Codex 侧，有 `~/.claude` 就装 Claude 侧。
想只装一边：

```bash
./install/install.sh --codex      # 只装到 Codex（reviewer 用 claude）
./install/install.sh --claude     # 只装到 Claude Code（reviewer 用 codex）
./install/install.sh --core-only  # 只装命令，不碰任何 skill
```

装完的位置：

```
~/.local/lib/rloop/        代码本体（rloop.py + rloopgui/）
~/.local/bin/rloop         → 指向上面的符号链接
~/.codex/skills/rloop/     Codex 侧的 skill
~/.claude/skills/rloop/    Claude 侧的 skill
```

`~/.local/bin` 不在 PATH 上的话，脚本会提醒你加这一行：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

装完会自己跑一遍自检：版本、api 契约、换个目录还能不能跑、面板起不起得来。

## 用

### 在 Codex（或 Claude Code）会话里

写完一批代码之后，直接说：

> 审一下

会话会起 review、读 findings、动手改、写逐条回应，然后再跑一轮，直到双评分
都过 8.0 且没有阻塞项，或者熔断（跑满轮数、连续几轮没进展、reviewer 自相矛盾）。

**中途不需要你做什么**，它会一直跑。只有几种情况会停下来问你：判定不可信、
和 reviewer 在同一条 finding 上反复拉锯、或者要动的东西超出这次改动的范围。

### 直接敲命令

```bash
rloop                              # 审当前改动
rloop --reviewer claude            # 指定谁来审（Codex 侧的 skill 会自动带上）
rloop --base main                  # 审相对 main 的全部改动
rloop --label "GUI 切分"            # 起个名字，列表里好认
rloop 重点看并发安全                # 给 reviewer 一个侧重点

rloop list                         # 所有 loop
rloop status                       # 当前 loop 的走势
rloop report                       # 完整报告
rloop stop                         # 停掉正在跑的那一轮
```

审的范围是自动判的：工作区脏 → 未提交的改动；干净 → 相对主干；再没有 → 最后
一个 commit。

### 面板

```bash
rloop web
```

本地网页面板，只绑 127.0.0.1 + 随机 token。走势图、findings 折叠着色、diff
高亮、实时进度、带参数起一轮。页面零外部引用，在 Claude Code 的 Browser
面板里也能直接开。

面板是**观察者**——能看、能起一轮，但没有「处理 findings」的按钮。按下去的
必然是个没有上下文的陌生会话，那正是这套东西刻意避开的。

## 数据放在哪

```
<你的项目>/.review-loops/    每个 loop 的账本、每轮的 diff / findings / 回应
~/.rloop/registry.json       全局注册表（哪些 loop 在哪个项目）
```

`.review-loops/` 建议加进 `.gitignore`（或者 `.git/info/exclude`）。

卸载不动这两处：

```bash
./install/install.sh --uninstall
```

## 已知的限制

- **reviewer 默认能跑测试，也就能改文件。** 沙箱放开到 codex 的 `workspace-write`
  （claude 是 `auto`），它才跑得动 pytest；代价是「不改你的代码」这条不再由
  内核强制，而是靠 prompt 明令加工作区指纹核对——它真动了被审代码，这一轮会作废。
  审你不信任的代码时加 `--no-verify` 关回只读，那时它拿不到实证，
  `validation_commands` 会如实标 `not_run`。
- **跑测试会掉产物。** 没进 `.gitignore` 的那些会被指纹看见，rloop 点名提醒但不作废；
  要留意它们会进下一轮的送审范围。
- **只有 codex 有细粒度进度。** reviewer 是 claude 时，面板上看不到它执行的每条
  命令（claude 不吐那种事件流），只有轮次开始/评分/结束几个节点。`rloop api meta`
  里如实声明了这一点。
- **面板的前端 JS 没有自动化测试。** 服务端和客户端都有（253 条），中间那 200 行
  页面脚本靠手工验。
- **单机工具。** 没做多机、多用户、远程访问。

## 出问题时

| 症状 | 多半是 |
|---|---|
| `command not found: rloop` | `~/.local/bin` 不在 PATH |
| `SyntaxError` 一堆 | PATH 上的 python3 低于 3.11 |
| `nothing to review` | 工作区没改动，且相对主干也没有差异 |
| 面板 `No module named rloopgui` | `~/.local/lib/rloop/rloopgui/` 被删了，重装 |
| Codex 里审了半天是自己审自己 | skill 没带 `--reviewer claude`，检查 `~/.codex/skills/rloop/SKILL.md` |

跑一轮出错时，`rloop status` 看状态，`rloop logs` 看日志，
`.review-loops/<id>/round-NN/reviewer.log` 是 reviewer 的原始输出。
