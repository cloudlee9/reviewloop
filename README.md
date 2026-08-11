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

跑完还会把**完整结果**打在终端上：走势表、上一轮的裁决、本轮 findings（分级、`file:line`、建议）、它实际跑了什么、它建议先做什么。同一份内容也落在 `round-NN/review.md` 里。

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

两套，用 `integration` marker 分开，代价差着量级：

```bash
python3 -m pytest                               # 默认：不碰真 agent、不碰网络，零成本
python3 -m pytest -m integration                # 真实依赖：真子进程 + claude/codex 探活，几秒
RLOOP_E2E=1 python3 -m pytest -m integration    # 再加两次真的双模型 loop —— 会烧配额
```

在仓库根目录跑（`pytest.ini` 里 `testpaths = tests`）。测试文件自带 sys.path 引导，从别的目录跑也行，把 tests 目录的路径给它就是：

```bash
python3 -m pytest <仓库路径>/tests
```

除 pytest 本身外没有别的测试依赖。

`pytest.ini` 里 `addopts = -m "not integration"`，所以默认档是下面四个文件。它们**都不碰真的 claude / codex**，但各自的护栏不一样：

| 文件 | 覆盖什么 | 允许起什么进程 |
|---|---|---|
| `tests/test_rloop.py` | 判定与渲染的纯函数 | 什么都不许起 |
| `tests/test_scope.py` | 送审范围：`determine_scope` / `default_branch` / `build_scope_patch` | 只许起 `git` |
| `tests/test_fake_agents.py` | 外壳：零参数分发 → 一轮 review → 判定 → 续轮 → 各个出口 | 起 `rloop.py` 本身，reviewer 换成 PATH 上的替身 |
| `tests/test_api_contract.py` | `rloop api` 的对外契约：自洽、只读、形状 | 起 `rloop.py` 本身 |
| `tests/test_api_events.py` | 进度事件流：`--since` 幂等、gap、孤儿探活 | 同上 |
| `tests/test_progress_events.py` | 事件的解析、渲染、落盘 | 什么都不许起 |
| `tests/test_gui_client.py` | 面板侧的 CLI 客户端 | 只起假 rloop（几十行脚本） |
| `tests/test_gui_web.py` | 面板的 HTTP 层：鉴权、转发、出错 | 起 `ThreadingHTTPServer` + 假 client |
| `tests/test_gui_isolation.py` | 切分的保镖：面板不许碰核心内部 | 只扫 AST，什么都不起 |

### 判定层 `tests/test_rloop.py`

覆盖的是 loop 会不会误判达标、会不会该熔断不熔断，基本都压在这几个函数上：

| 函数 | 覆盖点 |
|---|---|
| `gate_pass` | 分数恰好等于阈值要通过（`>=` 而不是 `>`）；任一维度低于阈值不通过；`blocking_findings` 非零时双 10 分也不通过；自定义阈值；模型把数字写成字符串时的强转 |
| `detect_stall` | 历史不足 3 轮不熔断；连续两轮持平 / 倒退 / 涨幅不超过 `STALL_EPSILON` → 熔断；任一维度真涨或阻塞项减少 → 不熔断；只看尾部窗口，早期的大跃进不能永久豁免熔断 |
| `load_review` | 文件缺失、空文件、带 BOM、被 markdown fence 包裹（三种写法）、BOM 与 fence 叠加、四种非法 JSON、四个必需字段各缺一个、非法 UTF-8 字节、轮次隔离 |
| `render_score_history` | 空历史输出「（首轮）」；单轮与多轮的完整表格逐行比对；保持传入顺序；忽略多余字段 |
| `read_text_safe` | 剥开头 BOM；正文里同样的字节序列不动；坏字节替换成 U+FFFD 而不抛异常 |
| `normalize_argv` | 裸 `rloop`、`rloop 中文侧重点`、`rloop --review-only` 都归一到 `review`；真子命令与 `-h/--help/--version` 原样放行 |

### 范围判定 `tests/test_scope.py`

送审范围是整个 loop 的根：范围错了，reviewer 审的就是别人的代码，你也会照着去改范围外的东西。这一档在临时仓库里用**真的 git**（不花配额、不联网），矩阵覆盖：

| 场景 | 断言什么 |
|---|---|
| 工作区有改动 / 只 staged 没 commit | base = HEAD，终点是工作树 |
| 只有未跟踪新文件 | `git diff` 为空也要有范围，且新文件内容真的进了补丁 |
| 工作区干净、分支相对主干有提交 | base = merge-base |
| 工作区干净、主干上、只有一个提交 | 分别回退到「最后一个 commit」和直接报错退出 |
| 空仓库（还没有提交） | 报错退出——`rev-parse HEAD` 在空仓库会把字面量 `HEAD` 打到 stdout，判断必须带 `--verify --quiet` |
| `--commit <历史提交>` | 终点钉死：后续提交与工作区改动都不得出现在补丁里 |
| `--commit HEAD`（工作区干净 / 脏） | 干净时不钉（可边改边审），脏时钉死 |
| `--commit <根提交>` / 未知提交 | 报错退出 |
| `--base` | merge-base 起算，且未提交改动也在范围内 |
| 本地 main 落后于 `origin/main` | 主干取 `origin/main`，上游提交不得进入送审范围 |
| 无远端 / 无同名分支 | 回退到本地 `main`；都没有则返回 `None` |
| 未跟踪文件的边角 | 二进制、空文件、含空格与前导 `-` 的文件名；数量上限与字节上限触发时被跳过的文件必须如实点名 |
| `.review-loops/` | 任何情况下都不得进入送审 diff |

外加 `build_context_pack` 的三条：钉死的范围写出的 `diff.patch` 只含目标提交；未跟踪内容内联时 prompt 里说清楚了；reviewer 是 claude 时 prompt 里明确写了它没有 shell。

### 外壳 `tests/test_fake_agents.py`

把 `claude` / `codex` 换成临时目录里的替身可执行文件塞进 PATH：reviewer 按剧本吐预先写好的 review JSON，并把收到的完整 argv 记下来。rloop 不再起别的 agent，所以这里没有"假 fixer"——要模拟"作者改了代码"时测试自己去动工作区，这也更贴近真实。零配额、秒级，放在默认档跑。

- **单轮契约**：一次 `rloop` 只起一次 agent、绝不改代码；达标退 0 并关闭 loop；未达标退 2 且 loop 保持 `open`；reviewer 非零退出如实报错而不是吞掉。
- **续轮**：第二次跑接在同一个 loop 上、轮次递增；第 2 轮 reviewer 确实拿到了上轮 findings 与你写的 `response.md`；没写回应时日志明说会被判 `not_fixed`；`--new` 与显式范围参数各自另起 loop；跑满轮数后 `can_continue` 变 false。
- **门禁自洽**：双 9 分 + `blocking_findings=0` + `verdict=pass` 但 findings 里躺着 critical → 退 3；verdict 与分数矛盾 → 退 3；分数越界 → 退 3；未达标却零 findings → 退 3；reviewer 吐垃圾 → 退 1。
- **命令行契约**：codex reviewer 拿到 `-s workspace-write`（且 `-s` / `-C` 排在 `exec` **之前**，否则 codex 直接报 unexpected argument，沙箱等于没起来）加 `--ephemeral --output-schema`，**没有** `danger-full-access`、也没有任何 `--dangerously-bypass-*`；claude reviewer 拿到 `--permission-mode auto --safe-mode --no-session-persistence --json-schema`，没有 `--dangerously-skip-permissions`；`--no-verify` 把两边分别打回 `read-only` / `plan` 并落进 `loop.json`；`--effort` 与 `--reviewer-model` 各自落对。
- **`--json` 载荷**：退出码、轮次、分数、findings、三个路径字段都在，且 `patch_path` 真的存在；范围钉死时 `fix_allowed` 为 false；不加 `--json` 时 stdout 不吐 JSON。

### 默认档绝不会拉起真实 agent

`tests/test_rloop.py` 和 `tests/test_scope.py` 各有一个 autouse fixture 把 `rloop.subprocess` 换成替身：常量（`PIPE`、`TimeoutExpired` 等）照常透传；前者 `run` / `Popen` 一律抛 `AssertionError`，后者只放行 `git`、`Popen`（两个 agent 唯一的入口）照抛。两个文件各有一条用例先验证护栏本身是活的，否则"没起 agent"只是句空话。同一个 fixture 还把 `RLOOP_HOME` / `REGISTRY` 指到 `tmp_path`，测试碰不到 `~/.rloop`。

`tests/test_fake_agents.py` 是故意要起进程的，但它把自己写的替身放在 PATH **最前面**，真的 `claude` / `codex` 永远排在后面轮不到。

可以自己验证——把假的 claude/codex 塞到 PATH 最前面，看诱饵有没有被执行：

```bash
mkdir -p /tmp/fakebin
for t in claude codex; do
  printf '#!/bin/sh\ntouch /tmp/spawned\nexit 1\n' > /tmp/fakebin/$t
  chmod +x /tmp/fakebin/$t
done
rm -f /tmp/spawned
PATH="/tmp/fakebin:$PATH" python3 -m pytest -q
[ -f /tmp/spawned ] && echo "有 agent 被拉起" || echo "没有 agent 被拉起"
```

诱饵放在环境 PATH 里，而 `test_fake_agents.py` 的替身排在它前面，所以这个检查真正在问的是「有没有哪次调用逃到了环境 PATH 上的 agent」——答案应当是没有。

### 集成测试 `tests/test_integration.py` —— 默认不跑

默认档的规矩是绝不碰真东西，所以真实依赖单独有个地方验。这个文件整体挂 `pytest.mark.integration`，默认被 `addopts` 摘掉，三档递增、都要显式授权：

| 档 | 怎么跑 | 碰什么 | 代价 |
|---|---|---|---|
| 1 依赖探活 | `-m integration` | `claude --version`、`codex --version`、`git --version` | 亚秒，不花配额 |
| 2 进程生命周期 | `-m integration` | 用真实子进程打 `stream_subprocess` 的流式落盘、非零退出码、可执行文件缺失（127）、**超时 SIGKILL** 四条路径 | 几秒，不花配额 |
| 3 端到端 | `RLOOP_E2E=1 -m integration` | 临时 git 仓库里造一坨未提交改动，真跑 `rloop` | 分钟级，**烧配额** |

第 2 档的超时用例不只看返回码：子进程一边打字一边给心跳文件追加字节，`stream_subprocess` 超时返回后再隔一秒比对文件大小，没长才算它是真死了，而不是只被丢下不管。

第 3 档两条：

- `test_full_loop_against_real_agents`（`-n 1`）：单轮链路。断言 `round-01/` 的 review 侧产物齐全、范围被记下来、真 reviewer 的输出能被 `load_review` 吃下去、`gate_pass` 与 `outcome` 自洽、`report.md` 与注册表都写了、送审 `diff.patch` 非空。
- `test_two_rounds_continue_the_same_loop_with_a_real_reviewer`（`-n 3 -m 9.5`）：把循环跑满一圈。测试自己扮演开发会话——第一轮拿到 findings 后真的去改工作区、写 `response.md`，再跑一次 rloop。断言它接在**同一个 loop** 上、轮次递增、第 2 轮的 context pack 确实带上了上轮 findings 与那份回应、且真 reviewer 给出了 `prior_findings_status`。门槛调到 9.5 是为了让第 1 轮几乎必然未达标，好把续轮逻辑拉进来。

分数高低一概不断言——那是模型的判断，不该当测试的稳定性依据。机器上没有 `codex` 时 skip 而不是 fail。

### 真实链路验证记录（v0.4.0，放开沙箱那一版）

2026-08-11 本机实跑，审的就是"把 reviewer 从只读放开到 `workspace-write`"这笔改动本身，真实 codex 当 reviewer，303 秒。

**放开确实拿到了实证。** reviewer 自己跑了 `pytest -q`（135 秒、`5 failed, 255 passed, 17 errors`），跑了针对性子集（`10 passed`、`122 passed`），还**在一个临时 git 仓库里复现了它提的 finding**。同一份代码在只读档下它只能读——这就是放开换来的东西。

**它当场抓出一个真洞。** 指纹当时只记 `status` / `HEAD` / `git diff HEAD`，而未跟踪文件的内容会进送审补丁却不进 `git diff`——改一个已存在的未跟踪源码文件，三个维度纹丝不动。它建了临时仓库把这条复现了出来（`changed=[]`），列为 high。现在指纹多了 `untracked` 这一维（逐文件记大小和 mtime），回归用例是 `test_editing_an_untracked_file_counts_as_touching_the_code`。

**沙箱边界也量出来了。** 那 22 个失败全是评审环境所致，不是代码问题：17 个 error 是 `socket.bind` 撞上 `PermissionError: [Errno 1] Operation not permitted`（沙箱不给网络，面板测试要绑本地端口），5 个 failed 是 `ps` 在沙箱里看不到别的进程、于是依赖"进程还活着吗"的用例断在空字符串上。这两类现在写进了 context pack，明确要求 reviewer 认出来、记 `fail` 并注明原因，别拿它开 finding。

**claude 侧的档位是拿同一个仓库对照出来的。** 一个两行的临时仓库，同一份改动审两遍：`acceptEdits` 那次，`cat` / `git status` 跑通了，但 `python3 -c "import a; print(a.f())"` 记成 `not_run`，note 写着"被权限层拒绝（This command requires approval）"；换成 `auto` 之后，同样五条命令全 `pass`，其中 `f() = 2` 是真跑出来的。`acceptEdits` 和 `dontAsk` 只自动批准文件编辑，Bash 仍要人点头 —— 而 `-p` 模式下没人可问。给错档位比不放开更糟：reviewer 以为自己能跑，`validation_commands` 里写满"被拒绝"。

**第二轮自审（同一天，339 秒）又拆了三个假设。** 它实测出 `rg -n apply_patch rloop.py` 会被判成「动手证据」——一条纯检索命令，白烧一整轮；现在写命令必须出现在命令位置上（行首、`;` `&&` `|` 之后、或 `zsh -lc "` 之后），检索自己的源码不再触发。它还把「改内容不可能既不改大小又不动 mtime」这条假设做掉了：同长度的 `aaaa` 改成 `bbbb`、`os.utime` 复原时间戳，四个维度全静止——所以未跟踪文件现在算内容哈希，不再只记元数据。第三条是文案：日志里那句「它看的是起跑那一刻的快照」根本不成立，reviewer 直接跑在工作区上、读的是实时文件，改成了「可能落在改前改后的混合状态上」。三条各自都带着可复现的验证命令。

**第三轮又抓到一种误作废。** reviewer 复现 finding 的标准动作是在 `/tmp` 建个临时仓库、写文件、`git commit` —— 前几轮它自己就这么干过。这些命令一度会被记成"动手证据"，只要作者恰好同时改了真工作区，两个互不相干的信号就凑成一次误作废。现在带 `cd /tmp`、`git -C /tmp`、`mktemp`、`tempfile` 的命令一律不算证据；代价是这也成了一条逃生阀，但误作废整轮比漏判贵得多，这个方向的偏保守是有意的。

**最后一轮自审是四个视角并行跑的**（门禁逻辑 / 指纹正确性 / 测试质量 / 说的和做的是否一致），每条 finding 再派一个只想证伪它的人去实跑。25 条里活下来的那些暴露了三类问题，都已修：

- **误作废**（最贵的一类）：`git apply --check`、`git stash list`、`git commit --dry-run` 这些**只读** git 命令被当成动手证据；写到 `/tmp` 绝对路径的脚本没被临时目录豁免覆盖；未跟踪文件按"前 100 个算哈希"切，于是 reviewer 新建一个排序靠前的文件就能把原来的第 100 名挤成第 101 名、记录格式从 sha256 变 mtime，内容没动却看着像被改了。现在算不算哈希只看文件自己多大，不看排第几。
- **漏判**：`git diff HEAD` 用 `text=True` 取输出，仓库里只要有一个二进制改动，解码就抛异常被 suppress 吞掉，`diff` 这个键干脆不存在 —— 而"缺的键不参与比较"意味着最重要的那条维度**静默消失**。现在全程走 bytes。另外只查了起跑那张快照完不完整，跑完那张没查。
- **说的和做的不一致**：`--no-verify` 在**续轮**时根本没人读，开关是一句静默失效的咒语；rubric 里无条件写着"你在一个只读沙箱里……失败两次就别再试了"，正好劝阻放开档下的 reviewer 去跑测试；给 claude 的 prompt 教它"把失败归因为沙箱且不扣分"，而 claude 那边压根没有沙箱；报告抬头还自称"只读子进程"，指纹裁决一个字没进报告 —— 而 prompt 恰恰是拿"会记进本轮报告"当对 claude 的约束；作废和 reviewer 崩溃都记成 `reviewer exit 1`，事后分不清这一轮是怎么没的。

变异测试还量了一次测试本身：把提醒删掉、把 `patch_apply` 事件识别删掉、把新增文件的点名删掉，全套测试照样绿 —— 那些"承诺"当时没人守。现在守上了。

**这一轮被自己的保险误杀了，然后保险改了。** 评审跑的那五分钟里，作者（就是写这段的会话）在改 `rloop.py`——于是指纹对不上，rloop 判定"reviewer 动了被审的代码"，303 秒的评审连同上面那些实证一起被作废。指纹能证明工作区变了，证明不了是谁变的，而**边改边审正是 rloop 的用法**。现在作废要两个信号：指纹动了，**并且** reviewer 自己的日志里有动手痕迹（codex 的 `file_change` 事件，或 `sed -i` / `git commit` / 往源码文件重定向这类命令）。只有指纹动了就仅仅提醒。回归用例是 `test_the_author_editing_during_the_round_does_not_void_it`。

### 真实链路验证记录（v0.3.0，会话驱动）

2026-08-08 本机实跑 `RLOOP_E2E=1 … -k "full_loop or two_rounds"`，`2 passed in 166.76s`，真实 codex 当 reviewer。

`test_two_rounds_continue_the_same_loop_with_a_real_reviewer` 是这一版的关键证据：临时仓库里放一个未提交的 `util.py`（除零没防、`open(os.path.join('/etc/app', name))` 没校验、异常被吞），`-n 3 -m 9.5 --effort low`。第一轮真 reviewer 给出 findings、退出码 2、`can_continue: true`；测试随后**扮演开发会话**改代码并写 `response.md`；第二次跑 rloop 接在同一个 loop 上，轮次递增到 2，第 2 轮的 context pack 里确实带上了上轮 findings 与那份回应，真 reviewer 也给出了 `prior_findings_status`。

这条覆盖的正是新架构的核心假设：**循环由调用方驱动、跨轮连续性靠账本而不是靠 agent 的记忆**。

### 真实链路验证记录（v0.2.0，自动 fixer 时期）

2026-08-08 本机实跑 `RLOOP_E2E=1 … -k full_loop_with_real_fixer`，`1 passed in 185.47s`。临时仓库里放一个未提交的 `util.py`（除零没防、`open(os.path.join('/etc/app', name))` 没校验、异常被吞），`-n 2 -m 9.5 --effort low --notify none`：

```
[21:10:11] rloop 0.2.0 — loop 20260808-211011-cc
[21:10:11]   范围   the uncommitted changes in the working tree (1 files), against HEAD e53c78700e94
[21:10:11]   角色   reviewer=codex fixer=claude
[21:10:11]   门槛   双评分 >= 9.5，blocking_findings == 0
[21:10:11] ── 第 1 轮 ────────────────────────────────────────
[21:11:12]   codex 退出码=0 耗时=60s 日志=reviewer.log
[21:11:12]   评分   交付物=3.5 生产就绪=2.0 阻塞项=1 → needs_work
[21:11:12]   fixer (claude) 开始修…
[21:12:09]   claude 退出码=0 耗时=57s 日志=fixer.log
[21:12:09] ── 第 2 轮 ────────────────────────────────────────
[21:13:17]   codex 退出码=0 耗时=66s 日志=reviewer.log
[21:13:17]   评分   交付物=8.8 生产就绪=3.0 阻塞项=0 → needs_work
[21:13:17] 结果：exhausted — 跑满 2 轮仍未达标
```

这一跑同时证实了这几件事：

- **收敛回路是通的**：3.5 / 2.0 / 1 个阻塞项 → 真 fixer 改完 → 8.8 / 3.0 / 0 个阻塞项。第 2 轮 reviewer 在 `prior_findings_status` 里逐条判定了上一轮自己提的问题，`round-01/author-response.md` 是真 fixer 写的（1650 字节）。
- **未跟踪文件的内容真的进了送审 diff**：`util.py` 是新建的未跟踪文件，`round-01/diff.patch` 是 19 行而不是 0 字节——否则 reviewer 什么都看不到。第 2 轮 144 行，含 fixer 新加的 `test_util.py`。
- **reviewer 的只读沙箱在真实评审里生效**：`round-01/reviewer.log` 头部是 `approval: never` / `sandbox: read-only`；第 2 轮 reviewer 的小结里自己写道「只读环境无法完整复跑依赖临时目录的 pytest」——它撞上了这个限制并如实报告，而不是假装跑过。

顺带暴露一个使用注意：临时仓库没有 `.gitignore`，fixer 跑测试留下的 `__pycache__/*.pyc` 作为未跟踪文件进了第 2 轮补丁（二进制只渲染成一行 `Binary files … differ`），reviewer 把它当成"补丁混入字节码产物"记了一笔。未跟踪列表遵循 `--exclude-standard`，所以正常仓库里由 `.gitignore` 兜住。

### 真实链路验证记录（v0.1.0，task 模式时期）

以下是 2026-08-08 在本机的一次实跑，跑的是当时的 task 模式（`rloop start "任务"`，author 先写、reviewer 后审）。命令形态后来变了，保留它是因为它是「真实双模型链路被跑通过」的原始证据。

**一次完整的真实双模型 loop**（临时 git 仓库、`RLOOP_HOME` 指向临时目录、`-n 1 --notify none --reviewer-effort low`）：

```
[20:22:05] rloop 0.1.0 — loop 20260808-202205-cc
[20:22:05]   角色   author=claude reviewer=codex
[20:22:05]   门槛   双评分 >= 8.0，blocking_findings == 0
[20:22:05] ── 第 1 轮 ────────────────────────────────────────
[20:22:05]   author (claude) 开始实现…
[20:23:00]   claude 退出码=0 耗时=55s 日志=author.log
[20:23:00]   reviewer (codex) 开始评审…
[20:23:37]   codex 退出码=0 耗时=36s 日志=reviewer.log
[20:23:37]   评分   交付物=10.0 生产就绪=10.0 阻塞项=0 → pass
[20:23:37] 结果：converged — 第 1 轮达标
```

全链路 92 秒走通：`cmd_start` 的干净工作区检查与 `.git/info/exclude` 写入 → `build_author_prompt` → 真实 `claude -p` → `build_context_pack` → 真实 `codex exec --output-schema` → `load_review` 解析 → `gate_pass` 判定 → `render_report` → 注册表落盘。`round-01/diff.patch` 里是 author 真实产生的改动（`hello.txt` 加了一行 `world`），`review.json` 是 codex 按 schema 产出的合法 JSON。

多轮路径（第 2 轮拿到上轮 findings 继续改、`prior_findings_status`、`render_score_history` 喂给真实 reviewer）由本仓库自己的 `.review-loops/` 提供证据——这套测试本身就是在一个真实的 rloop loop 里写出来的。那个目录被 `.git/info/exclude` 排除，只在本地存在。

命令行验证（2026-08-08、本机、`pytest 7.4.0`）：

| 命令 | 结果 |
|---|---|
| `python3 -m pytest -q` | `105 passed, 10 deselected` |
| `python3 -m pytest -m integration -q` | `8 passed, 2 skipped, 105 deselected` |
| `RLOOP_E2E=1 python3 -m pytest -m integration -q -k "full_loop or two_rounds"` | `2 passed in 166.76s`（真实 codex，含续轮） |

### `--no-verify` 的只读档——两个 CLI 上的实证

只写对参数不等于真的拦得住，所以两边各拿一次真实调用验过（临时 git 仓库，提示词直接命令模型去写文件）。**这一节记录的是 `--no-verify` 那一档**——0.4 之前它是唯一的行为，现在要显式加开关才回到这里：

**codex `--sandbox read-only`**

```
$ codex exec -C . --sandbox read-only --output-schema schema.json -o last.json \
    -c model_reasoning_effort=low "Create a file named PWNED.txt … then report whether it succeeded or was blocked"
approval: never
sandbox: read-only
{"verdict":"pass","summary":"The file write was blocked."}
$ ls PWNED.txt
ls: PWNED.txt: No such file or directory
```

写被沙箱拦住，同时 `--output-schema` + `-o` 照常产出合法 JSON——交付结果的那两个文件是 codex 进程自己写的，不过沙箱。这正是 reviewer 不需要写权限也能交付的原因。

**claude `--permission-mode plan`**

```
$ claude -p "…用 Write 工具或 shell 建 PWNED.txt…" --permission-mode plan --json-schema "$(cat schema.json)" --effort low
{"verdict":"pass","summary":"The file write was BLOCKED. PWNED.txt was not created — plan mode is active …"}

$ claude -p "…(1) 跑 python3 -c 'print(6*7)' (2) 跑 echo pwned > PWNED2.txt…" --permission-mode plan --json-schema … 
{"verdict":"pass","summary":"Both Bash commands were blocked by plan mode. …"}
```

两个文件都没被创建。第二次同时确认了代价：plan 模式下**连读命令都跑不了**，所以 claude 当 reviewer 时是真的没有 shell，context pack 里因此明确要求它把 `validation_commands` 标成 `not_run`。

`--json-schema` 收的是内联 JSON 而不是文件路径（给路径会报 `--json-schema is not valid JSON`），stdout 上就是干净的结构化对象，没有信封包裹——所以 rloop 直接把 stdout 落成 `review.json`。

### 两个曾经的缺陷（已修，留了回归用例）

这两个洞是 rloop 用自己跑一遍自测时挖出来的：写测试的那个模型先发现并用 `xfail(strict=True)` 钉住，reviewer 独立确认后计入 findings，事后已修。

**单行 fence 会让 `load_review` 抛异常。** 原先剥 fence 用 `raw.split("\n", 1)[1]`，模型把整段 fence 挤在一行里输出时下标越界，`IndexError` 一路冒到 `drive()`，绕过"脏输入返回 `None`"的契约。现在改成截取最外层 `{...}`，顺带也能吃下前后带废话、带语言标注的输出。回归用例 `test_load_review_tolerates_single_line_fence`。

**闷声不响的 agent 不会被超时杀掉。** 原先超时判定写在 `for line in p.stdout` 的循环体里，只有收到新行才检查；子进程卡住又不输出时迭代永久阻塞，`timeout` 完全不生效，而且最后返回 0 被当成正常完成。现在读 stdout 交给守护线程，超时由 `p.wait(timeout=)` 判定，与有没有输出无关，超时后 `kill()` 再 `wait()` 回收。回归用例 `test_stream_subprocess_timeout_kills_a_silent_child`（第 2 档）。

### 还没被覆盖的部分

- **面板的前端 JS 没有自动化测试**。`page.html` 里那 200 行（走势图、findings
  折叠、diff 高亮、SSE 收敛）目前只有手工验过。服务端那一层有
  `test_gui_web.py` 守着，客户端有 `test_gui_client.py`，中间这一段没有。
  补它要引入一整套浏览器测试依赖，对一个单文件面板不划算 —— 现状是明确的取舍，
  不是遗漏。

  好在切分之后这块的爆炸半径小了：前端错了就是显示不对，不会像以前那样
  牵动进程管理（那部分整个搬进核心了，有 `test_api_*` 守着）。


- `stalled` 出口只有纯函数层的 `detect_stall` 覆盖，没有端到端用例（要造连续 3 轮分数持平的剧本）。`converged` / `needs_work` / `exhausted` / `inconsistent` / `pinned_scope` / `failed` 都在 fake-agent 那一档实跑过。
- `notify()` 的 `macos` 与 `cmd` 两个分支、`rloop list/status/logs/report/replay/stop` 六个子命令，都还没有自动化用例。
- KeyboardInterrupt 中断路径没测。loop 被中途杀掉会停在 `status=running`，下一次裸调 `rloop` 会**接管**它（拿到锁却发现是 running，说明上个进程死了）；那一轮若没留下可用的 `review.json`，会退回去重跑该轮。
- 沙箱档位这件事，在 fake-agent 那一档只验到「命令行参数对不对」，两个 CLI **真的**放开到哪、`--no-verify` 真的拦不拦得住，靠的是上面的手工实证记录，没有自动化用例（要真跑模型）。
- **动手证据靠扫日志，不是靠内核**。`reviewer_write_evidence` 认的是 codex 的 `file_change` 事件和一小撮明确的写命令（`sed -i`、`git commit`、往源码文件重定向……）。刻意绕过它不难（比如用 python 脚本写文件），只是那已经不是「模型顺手改了一下」而是「模型在躲检测」了。claude 侧没有等价的事件流，扫不到东西，所以那边只会提醒、不会作废。
- **指纹是失败开放的**。git 挂了或者超时，指纹就拍不全，reviewer 照样带着写权限跑，只是没人核对它。这时 loop.log 里会明说拍到了几个维度 —— 但不会自动降级成只读。
- **放开之后仍有跑不了的测试**。`workspace-write` 不给网络也不给进程可见性，绑端口、起本地服务、`ps` 探进程的用例照样红。context pack 里教了 reviewer 怎么认出这类失败，但认错了它就会拿环境限制去开 finding —— 这一层没有自动化保障。

## 需要知道的

- **rloop 自己不改代码**。reviewer 子进程默认能跑测试，但动了被审的代码这一轮就作废。改动由调用它的会话做，你看得见、能打断、能否决。
- **跑之前最好先 commit 一次**。送审范围会干净很多，事后也能用 `git diff` 看出这一轮到底动了什么。
- **配额是共享的**。reviewer 不占用你的交互会话，但会跟它抢同一个账号的 rate limit。`--effort xhigh` 挖得深很多，也贵很多。
- **reviewer 有写权限，隔离也不是绝对的**。`--safe-mode` / `--ignore-rules` 关掉了仓库定制，`workspace-write` 让内核挡在 HOME 前面，可两家 CLI 的隔离边界都不是为对抗恶意仓库设计的，claude 那边更是压根没有内核这一层。审你完全不信任的代码时加 `--no-verify`，最好再套一层容器或独立 worktree。
- **codex 并发**：本机 `SessionEnd` hook 会 nohup 拉起 `auto-ingest.sh` → `codex exec` 做 wiki 摄入。rloop 跑着时若会话结束，会有两个 codex 同时在跑。目前没有互斥。
- **裸调时同一项目只跑一个 loop**。第二个裸调会明确报 busy，不会另起一个并行 loop 跑同一份范围（首次启动那一小段由项目级锁保护，续轮由 per-loop 锁保护）。**显式 `--new` 会绕过这个限制**，允许并行——那是有意留的逃生口，代价是同时烧两份配额、留下两个互相独立的账本，之后 `status` / `logs` 默认只跟最近那个。
- **`rloop stop` 只发信号，不写任何状态**。它收掉 reviewer（连同其派生的 shell、测试进程）和 rloop 自己，然后就结束了；发信号前会用 `ps` 核对 pid 的命令行，免得 pid 被系统回收复用后误杀你别的活。
- **被停掉或被杀之后，状态停在 `running`，下次裸调自动接管**，那一轮若没留下可用的 `review.json` 就退回去重跑该轮。「被你停的」和「进程崩了」走同一条路——对下一步而言两者没有区别，所以 rloop 不去区分它们。这也是状态文件始终只有一个写者的原因：跨进程协调一旦出现，「谁写终态、字条何时落」那一整类竞态就跟着来了。
