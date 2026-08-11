# rloop

用另一个模型独立审当前工作区的改动。**跑一轮就返回**——循环由调用它的那个开发会话驱动。

```
你 vibe 完一堆改动
  ↓
会话跑 rloop --json     →  起一个无头、无状态的 reviewer 子进程（能跑测试，不改代码）
  ↓                        它给出结构化 findings + 双评分，退出码表达判定
会话读 findings、逐条判断、动手改、写回应
  ↓
再跑一次 rloop           →  reviewer 逐条裁决上轮 findings，重新打分
  ↓
直到退出码 0（达标）或熔断
```

改动始终发生在**一个你看得见、有完整上下文的会话里**，所以不需要交接文档、不需要给一个无头进程重建上下文、也不会往你的会话池里塞额外的会话。rloop 自己一行代码都不改。

## 装

```bash
./install/install.sh
```

装核心到 `~/.local/lib/rloop/`、软链到 `~/.local/bin/rloop`，再把驱动循环的 skill
装到 `~/.claude/skills/rloop/`（reviewer 用 codex）和 `~/.codex/skills/rloop/`
（reviewer 用 claude）——机器上有哪个装哪个。装完会自检一遍。

只要命令不要 skill：`./install/install.sh --core-only`。
卸载：`./install/install.sh --uninstall`（不动账本和历史）。
打一个能拿走的安装包：`./install/pack.sh` → `dist/rloop-<版本>.tar.gz`。

依赖：`git`、Python **3.11+**，以及 `codex` 和/或 `claude` 至少一个。
详见 [install/INSTALL.md](install/INSTALL.md)。

装好之后，在开发会话里说「审一下」或 `/rloop` 就会跑起来。

## 用

```bash
cd ~/Work/some-project     # 刚 vibe 完一堆改动
rloop                      # 就这样，不用输入任何东西
```

不给任务描述是有意的——reviewer 从 diff 和最近的 commit message 自己推断这坨改动想干嘛，并在报告开头写出它的理解。它要是理解错了，本身就是个信号。

给个侧重点也行：

```bash
rloop 重点看并发安全和错误处理
```

给会话/脚本调用时加 `--json`，stdout 上就只有一个 JSON 对象（日志自动改走 stderr）：

```bash
rloop --json
```

## 跑的时候能看见什么

reviewer 那几分钟不是黑盒。codex 用 `--json` 吐 JSONL 事件流，rloop 边跑边渲染成进度：

```
── 第 3 轮 ────────────────────────────────────────
  reviewer (codex) 开始评审…
    · 我先看一下这一轮的 diff 和上轮 findings。
    $ /bin/zsh -lc "git diff --stat 7b70725"
    $ /bin/zsh -lc "python3 -m pytest -q -p no:cacheprovider"
      ↳ exit 1
    · 本轮完成（输出 3184 tokens）
  codex 退出码=0 耗时=317s 日志=reviewer.log
  评分   交付物=7.8 生产就绪=6.9 阻塞项=1 → needs_work
```

成功的命令不回显（否则每条刷两行），失败的一定报 exit code。`--json` 模式下这些走 stderr，不污染 stdout 的 JSON。

跑完还会把**完整结果**打在终端上：走势表、上一轮的裁决（你改的那些它认不认）、本轮 findings（分级、`file:line`、建议）、它实际跑了什么、它建议先做什么。同一份内容也落在 `round-NN/review.md` 里。

**`--json` 时这份结果照打，只是走 stderr。** 会话驱动循环用的就是 `--json`，那条路径上人更需要看见 reviewer 到底说了什么——stdout 仍然只有那个 JSON 对象，解析的一方不受影响。

想要图表、折叠、diff 高亮的话：

```bash
rloop web
```

网页面板，走势折线图、findings 折叠着色、diff 高亮、实时进度、带参数起一轮。
它是一个**独立程序**（`rloopgui/`），零 `import rloop` —— 数据全部经 `rloop api`
拿。删掉整个 `rloopgui/`，rloop 照常工作。见 `rloopgui/README.md`。

## 退出码就是判定

| 码 | 含义 | 调用方该做什么 |
|---|---|---|
| 0 | 达标 | 停，汇报 |
| 2 | 未达标，有 findings | 处理 findings，写回应，再跑一次 |
| 3 | **reviewer 自相矛盾** | 别当成达标。把矛盾交给人 |
| 1 | 出错 | 读 stderr，如实报告 |

退出码 2 不代表还能继续——跑满轮数或熔断也是 2。看 `--json` 里的 `can_continue`。

## --json 载荷

```jsonc
{
  "loop_id": "...", "round": 2, "max_rounds": 5,
  "scope": "the uncommitted changes in the working tree (3 files), against HEAD abc123",
  "exit_code": 2,
  "outcome": "needs_work",
  "can_continue": true,      // false 时别再调 rloop，把结论交给人
  "fix_allowed": true,       // false 表示范围钉在历史提交上，你改工作区也进不了送审 diff
  "consistency_errors": [],
  "scores": {"deliverable_maturity": 7.6, "production_readiness": 5.8,
             "blocking_findings": 2, "verdict": "needs_work"},
  "findings": [...], "prior_findings_status": [...],
  "report_path": "...", "response_path": "...", "patch_path": "..."
}
```

## 每轮之间怎么传话

reviewer 是无状态的：每轮新进程、新脑子，靠账本知道历史。所以两件事必须落到文件上。

**它给你的**：`findings` + `prior_findings_status`（对上轮每条的裁决）。

**你给它的**：`response_path` 指的那个 `response.md`。每条 finding 都要有交代——采纳了怎么改的、反驳了理由是什么。**写得含糊，下一轮它就判 `not_fixed`。** 不写的话日志会警告，但不会拦你。

## 审哪些改动

不带 `--base` / `--commit` 时按「审你还没定稿的东西」逐级回退：

| 情况 | 审什么 |
|---|---|
| 工作区有改动 | 未提交的改动（`git diff HEAD`） |
| 工作区干净，分支相对主干有提交 | 从 merge-base 到 HEAD 的全部改动 |
| 工作区干净，与主干无差异 | 最后一个 commit |
| 都没有 | 报错退出，不浪费配额 |

「工作区干净、分支相对主干有提交」这一档里的主干，优先取远端默认分支（`origin/main`）而不是同名的本地分支——本地主干经常落后于远端，拿它求 merge-base 会把早就属于上游的提交也算成"你这次的改动"。没有远端时才回退到本地 `main` / `master` / `develop`。

显式指定：

```bash
rloop --base main          # 相对 main 的全部改动（含未提交）
rloop --commit HEAD~2      # 只审这一个 commit 引入的改动
```

两者的 diff 终点不一样，这决定了能不能自动修：

| | diff 起点 | diff 终点 | 改了算不算数 |
|---|---|---|---|
| 零参数 / `--base` | HEAD 或 merge-base | **当前工作树** | 算。你改在工作区里，下一轮 diff 自然带上 |
| `--commit <历史提交>` | 该提交的父提交 | **钉死在该提交** | 不算，`--json` 里 `fix_allowed: false` |

`--commit` 是严格的 `git diff parent target`：它之后的提交、以及工作区里没提交的东西，都不算数。代价是你改在工作区里的东西永远进不了送审 diff，reviewer 会一遍遍看同一份补丁，循环不可能收敛——所以这种范围下 `fix_allowed` 是 false，只出报告别动手。（`--commit HEAD` 且工作区干净时终点等价于工作树，此时不钉，照常可以边改边审。）

未跟踪的新文件也会进 `diff.patch`：用 `git diff --no-index` 对 `/dev/null` 生成补丁，不碰你的索引。否则"只新建了几个文件"的工作区会得到一份 0 字节补丁，reviewer 等于什么都没看到，`replay` 也复现不出当时的内容。文件太多（>100）或总量太大（>400 KB）时超出的部分只列文件名，并在 prompt 里明确点名让 reviewer 自己去读，不会悄悄丢掉。

## reviewer 能跑测试，但不能改代码

这两件事是分开的。默认放开的是**执行**，不是**修改**：

| | 跑什么 | 能执行命令吗 | 能改你的代码吗 |
|---|---|---|---|
| reviewer = `codex`（默认） | `codex -s workspace-write exec` | 能，测试真的跑得起来 | 内核层面能，但改了就整轮作废 |
| reviewer = `claude` | `claude -p --permission-mode auto` | 能 | 同上，且这边**没有**内核兜底 |
| 加 `--no-verify` | `-s read-only` / `--permission-mode plan` | codex 能跑但写操作全被拒；claude 连 shell 都没有 | **不能**，由沙箱 / 模式强制 |

0.3 里 reviewer 是硬只读的，代价在自审时暴露得很清楚：`--sandbox read-only` 连 `.pytest_cache` 都写不了，而 rloop 自己的测试几乎全靠 pytest `tmp_path` 造临时 git 仓库——只读下一律失败。于是 reviewer 只能静态读代码，`validation_commands` 里一片 `not_run`，`production_readiness` 封在低位上不去。放开写权限之后它能真把测试跑一遍，findings 带得上实证。

放开的档位卡在"够跑测试"那一层，不再往上：codex 用 `workspace-write`（工作区与临时目录可写、可执行命令，**HOME 由内核挡着**），不用 `danger-full-access`，更不给 `--dangerously-bypass-*`；claude 用 `auto`，不给 `--dangerously-skip-permissions`。**这个档位是实测定的**：同一句「跑 `python3 -c 'print(6*7)'`」，`acceptEdits` 和 `dontAsk` 都答 BLOCKED（它们只自动批准文件编辑，Bash 仍要人点头，而 `-p` 模式下没人可问），只有 `auto` 真跑出了 42。给错档位比不放开更糟——reviewer 以为自己能跑，`validation_commands` 里会写满「被权限层拒绝」。

「reviewer 不改你的代码」这件事，靠的不是它自觉：

1. **prompt 里的明令**——context pack 直接写着它绝不能改这个项目的代码，以及越界的后果。
2. **工作区指纹 + 执行记录，两个信号**——起 reviewer 之前给工作区拍一张指纹（`git status --porcelain` + `HEAD` + `git diff HEAD` 的摘要 + 每个未跟踪文件的内容哈希，全程走 bytes 不解码，排除 `.review-loops/`），它退出后再拍一张比对：
   - 指纹里 `HEAD`、已跟踪文件的内容、或**基线里已有的未跟踪文件**变了（未跟踪文件的内容一样会进送审补丁），**并且**它自己的日志里有动手痕迹（codex 的 `file_change` 事件，或 `sed -i`、`git commit`、往源码文件重定向这类命令；只读的那些不算 —— `git apply --check`、`git stash list`、任何带 `--dry-run` 的，以及写在项目目录之外的，都是正当的评审动作）→ **整轮作废**（退 1）。这一轮的判断建立在一份它自己动过的代码上，"把测试改绿了"和"代码本来就对"在结果里长得一模一样。
   - 指纹变了但没有动手痕迹 → 只提醒。**评审要跑好几分钟，你在这期间接着改自己的代码是 rloop 的正常用法**；指纹知道工作区变了，不知道是谁变的，所以第二个信号是必须的（这条是被一次真实误判逼出来的，见下）。
   - 只是多出些未跟踪文件 → 点名告诉你多了哪些。跑测试掉产物是常态，够不上作废；但未跟踪文件**会进下一轮的送审范围**，所以该 `.gitignore` 的得加上。被 `.gitignore` 的产物本来就不进 `git status`，不会惊动指纹。

   不对称之处：动手痕迹来自 codex 的 `--json` 事件流，claude 那边没有等价的东西可扫，所以 claude 当 reviewer 时只会得到提醒，不会被作废。**给 claude 的 prompt 里也照实这么写** —— 把「改了就作废」原样说给一个触发不了作废的路径，那是拿一句执行不了的威慑当保险。

**为什么不干脆让 reviewer 跑在快照里。** 复制一份工作区（或 `git worktree add`）让 reviewer 在里面跑，作者改原工作区互不干扰，它改了也无所谓 —— 两轮自审里 reviewer 都把这个当成首选修法提了。没这么做的原因很具体：快照带不走被 `.gitignore` 掉的东西，而那正是 venv、`node_modules`、构建缓存的所在。在一个新 worktree 里 `pytest` 多半直接起不来 —— 那就把这次放开的**唯一目的**掐掉了。所以现状是明摆着的取舍：要真跑测试，就得跑在真工作区上；代价是「谁改的」只能靠两个信号推断，而不是靠隔离消除。

什么时候关掉：**审你不信任的代码时用 `--no-verify`。** 放开写权限的同时也就放开了"仓库里一句提示注入能让 reviewer 做什么"的上界。

还有两条不随档位变：

- **项目级扩展始终关着，用户级配置始终开着**。claude 侧 `--safe-mode` 关掉 hook、MCP、插件、自定义命令与 agent、`CLAUDE.md`；codex 侧不传 `--dangerously-bypass-hook-trust`（未经 trust 的仓库 hook 不会执行）、`--ignore-rules` 丢掉仓库的 execpolicy、`--ephemeral` 不落会话。这一层必须单独堵：仓库 hook 是在模型说第一句话之前就跑掉的 `command`，plan 模式和沙箱档位都管不着它。
- **这不是对抗恶意仓库的强隔离**。两边各自的用户级配置（`~/.claude/settings.json`、`~/.codex/config.toml` 及其中已 trust 的 hook）照常生效；沙箱挡写不挡读——reviewer 会读遍整个仓库，提示注入的入口一直都在。审**别人的**、你不信任的代码，请自己套一层容器或独立 worktree。

## 双评分

| 维度 | 含义 |
|---|---|
| `deliverable_maturity` | 写出来的东西的质量：结构、文档、契约、测试覆盖、脚本、内部一致性 |
| `production_readiness` | 真实运行系统的就绪度：安全、基础设施、数据、运维、可观测性、端到端证据 |

**硬规则**：mock、demo、fixture、本地跑绿的检查，只能抬高第一个分数。真实依赖从没被跑通过时，`production_readiness` 封顶 5 分，代码写得再漂亮也一样。

这条规则是防止两个模型互相吹捧刷分的主要手段。

## 为什么 reviewer 是无状态的

每轮都重新拼一份完整 context pack 喂给一个全新的 reviewer 进程，而不是让一个 reviewer 会话一直跟着跑。

1. 长会话的 reviewer 会为自己前几轮的判断辩护，倾向于确认自己没错。无状态的每轮都是新鲜眼睛。
2. 可复现、可调试——`rloop replay N review` 能看到当时到底喂了什么。
3. 上下文不需要"对齐"，因为它是显式构造的，不会漂移。

处理 findings 的那一侧正好相反——它**该**有状态，所以那份活交给你自己的开发会话：它记得代码为什么写成这样，也不需要谁给它重建上下文。

context pack 每轮包含：意图（你给的侧重点，或让它自己从 diff + git log 推断）、分数走势、送审 diff（+ 上轮 diff 路径供对比）、上轮自己提的 findings（要求逐条判定是否已修）、你写的 `response.md`、rubric、checklist。

## 反驳机制

处理 findings 的会话被要求独立判断：同意就改，不同意就在 `round-NN/response.md` 写反驳理由，下一轮 reviewer 会读到并在 `prior_findings_status` 里判定是 `fixed` / `partially_fixed` / `not_fixed` / `rebutted_and_accepted`。

盲从和硬顶都不是目标，有证据的分歧是合法结果。

skill 里还写死了两条约束：**只改 findings 点到的地方**（不许顺手重构），以及**想反驳必须拿证据**（去复现、去跑命令、去引行号）。后一条是因为代码往往就是那个会话上一轮刚写的，它会本能地觉得 reviewer 小题大做。

## 产物

```
<project>/.review-loops/<id>/
  loop.json          状态、配置、分数历史
  loop.log           全过程
  report.md          最终报告
  review-schema.json reviewer 的输出契约
  round-01/
    review-prompt.md   喂给 reviewer 的 context pack
    review.json        reviewer 的结构化输出（门禁校验读它）
    review.md          同一份内容的人类可读版（你读它）
    reviewer.log
    diff.patch         本轮送审的 diff
    untracked-manifest.json  未跟踪文件逐条账目
    response.md        你对这一轮 findings 的逐条回应（下一轮 reviewer 会读）
```

`.review-loops/` 会被自动写进 `.git/info/exclude`（不动你的 `.gitignore`），所以它不会混进待评审的 diff。

全局注册表在 `~/.rloop/registry.json`，只存"哪个 loop 在哪个目录"，供 `rloop list` 跨项目查。

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--json` | 关 | 结构化结果打到 stdout，日志改走 stderr |
| `--new` | 关 | 强制开新 loop，不续当前项目里开着的那个 |
| `--base` / `--commit` | — | 显式指定评审范围 |
| `-n, --max-rounds` | 5 | 轮数上限 |
| `-m, --min-score` | 8.0 | 双评分阈值 |
| `-t, --timeout` | 2400 | 单个 agent 单轮超时（秒） |
| `--reviewer-effort` | medium | reviewer 推理档位。你的 codex 全局是 `xhigh`，每轮都那么跑很贵，这里默认降档 |
| `--effort` | — | `--reviewer-effort` 的简写，例如 `--effort xhigh` |
| `--notify` | macos | `macos` / `cmd` / `none` |
| `--notify-cmd` | — | `--notify cmd` 时执行，环境变量 `RLOOP_TITLE` / `RLOOP_BODY` / `RLOOP_ROOT` |

飞书通知：

```bash
rloop --notify cmd --notify-cmd 'lark-cli im ... "$RLOOP_TITLE: $RLOOP_BODY"' 
```

## 测试

默认档 298 条，不碰真 agent、不碰网络，零成本：

```bash
python3 -m pytest
```

分四类护栏，从「什么进程都不许起」到「起 rloop.py 本身、reviewer 换成替身」；
真跑双模型的那两条用 `integration` marker 隔开，默认不跑。

怎么分层、每一档拦住什么、以及**真实链路上验过什么、哪些地方到现在还没被覆盖**，
都在 [docs/testing.md](docs/testing.md)。

## 需要知道的

- **rloop 自己不改代码**。reviewer 子进程默认能跑测试，但动了被审的代码这一轮就作废。改动由调用它的会话做，你看得见、能打断、能否决。
- **跑之前最好先 commit 一次**。送审范围会干净很多，事后也能用 `git diff` 看出这一轮到底动了什么。
- **配额是共享的**。reviewer 不占用你的交互会话，但会跟它抢同一个账号的 rate limit。`--effort xhigh` 挖得深很多，也贵很多。
- **reviewer 有写权限，隔离也不是绝对的**。`--safe-mode` / `--ignore-rules` 关掉了仓库定制，`workspace-write` 让内核挡在 HOME 前面，可两家 CLI 的隔离边界都不是为对抗恶意仓库设计的，claude 那边更是压根没有内核这一层。审你完全不信任的代码时加 `--no-verify`，最好再套一层容器或独立 worktree。
- **codex 并发**：本机 `SessionEnd` hook 会 nohup 拉起 `auto-ingest.sh` → `codex exec` 做 wiki 摄入。rloop 跑着时若会话结束，会有两个 codex 同时在跑。目前没有互斥。
- **裸调时同一项目只跑一个 loop**。第二个裸调会明确报 busy，不会另起一个并行 loop 跑同一份范围（首次启动那一小段由项目级锁保护，续轮由 per-loop 锁保护）。**显式 `--new` 会绕过这个限制**，允许并行——那是有意留的逃生口，代价是同时烧两份配额、留下两个互相独立的账本，之后 `status` / `logs` 默认只跟最近那个。
- **`rloop stop` 只发信号，不写任何状态**。它收掉 reviewer（连同其派生的 shell、测试进程）和 rloop 自己，然后就结束了；发信号前会用 `ps` 核对 pid 的命令行，免得 pid 被系统回收复用后误杀你别的活。
- **被停掉或被杀之后，状态停在 `running`，下次裸调自动接管**，那一轮若没留下可用的 `review.json` 就退回去重跑该轮。「被你停的」和「进程崩了」走同一条路——对下一步而言两者没有区别，所以 rloop 不去区分它们。这也是状态文件始终只有一个写者的原因：跨进程协调一旦出现，「谁写终态、字条何时落」那一整类竞态就跟着来了。
