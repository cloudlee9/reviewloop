# 测试与实证记录

这份文档回答一个问题：**凭什么信这东西**。

主文档 [README](../README.md) 讲的是它是什么、怎么用；这里讲的是每条说法背后有什么
撑着 —— 测试分几档、各档的护栏是什么、哪些结论来自真实链路而不是替身、以及**哪些
地方到现在也没被覆盖**。最后那一节是有意写的：已承认的取舍和没发现的漏洞是两回事，
读的人得能分清。

## 两套测试

用 `integration` marker 分开，代价差着量级：

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
| `tests/test_gui_markdown.py` | 面板那个 markdown 渲染器：注入、结构 | 起 `node`（没装就 skip） |

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

## 真实链路验证记录

下面几段按版本倒序。每一条结论都对应一次真跑，不是替身跑出来的。

### v0.4.0 —— 放开沙箱那一版

2026-08-11 本机实跑，审的就是"把 reviewer 从只读放开到 `workspace-write`"这笔改动本身，真实 codex 当 reviewer，303 秒。

**放开确实拿到了实证。** reviewer 自己跑了 `pytest -q`（135 秒、`5 failed, 255 passed, 17 errors`），跑了针对性子集（`10 passed`、`122 passed`），还**在一个临时 git 仓库里复现了它提的 finding**。同一份代码在只读档下它只能读——这就是放开换来的东西。

**它当场抓出一个真洞。** 指纹当时只记 `status` / `HEAD` / `git diff HEAD`，而未跟踪文件的内容会进送审补丁却不进 `git diff`——改一个已存在的未跟踪源码文件，三个维度纹丝不动。它建了临时仓库把这条复现了出来（`changed=[]`），列为 high。现在指纹多了 `untracked` 这一维（当时是逐文件记大小和 mtime；那个假设在下一轮被证伪，改成了内容哈希，见下），回归用例是 `test_editing_an_untracked_file_counts_as_touching_the_code`。

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

### v0.3.0 —— 会话驱动

2026-08-08 本机实跑 `RLOOP_E2E=1 … -k "full_loop or two_rounds"`，`2 passed in 166.76s`，真实 codex 当 reviewer。

`test_two_rounds_continue_the_same_loop_with_a_real_reviewer` 是这一版的关键证据：临时仓库里放一个未提交的 `util.py`（除零没防、`open(os.path.join('/etc/app', name))` 没校验、异常被吞），`-n 3 -m 9.5 --effort low`。第一轮真 reviewer 给出 findings、退出码 2、`can_continue: true`；测试随后**扮演开发会话**改代码并写 `response.md`；第二次跑 rloop 接在同一个 loop 上，轮次递增到 2，第 2 轮的 context pack 里确实带上了上轮 findings 与那份回应，真 reviewer 也给出了 `prior_findings_status`。

这条覆盖的正是新架构的核心假设：**循环由调用方驱动、跨轮连续性靠账本而不是靠 agent 的记忆**。

### v0.2.0 —— 自动 fixer 时期

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

### v0.1.0 —— task 模式时期

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

## 两个曾经的缺陷（已修，留了回归用例）

这两个洞是 rloop 用自己跑一遍自测时挖出来的：写测试的那个模型先发现并用 `xfail(strict=True)` 钉住，reviewer 独立确认后计入 findings，事后已修。

**单行 fence 会让 `load_review` 抛异常。** 原先剥 fence 用 `raw.split("\n", 1)[1]`，模型把整段 fence 挤在一行里输出时下标越界，`IndexError` 一路冒到 `drive()`，绕过"脏输入返回 `None`"的契约。现在改成截取最外层 `{...}`，顺带也能吃下前后带废话、带语言标注的输出。回归用例 `test_load_review_tolerates_single_line_fence`。

**闷声不响的 agent 不会被超时杀掉。** 原先超时判定写在 `for line in p.stdout` 的循环体里，只有收到新行才检查；子进程卡住又不输出时迭代永久阻塞，`timeout` 完全不生效，而且最后返回 0 被当成正常完成。现在读 stdout 交给守护线程，超时由 `p.wait(timeout=)` 判定，与有没有输出无关，超时后 `kill()` 再 `wait()` 回收。回归用例 `test_stream_subprocess_timeout_kills_a_silent_child`（第 2 档）。

## 还没被覆盖的部分

- **面板的前端 JS 只有一档有测试**。markdown 渲染器有
  `test_gui_markdown.py`（抠出来喂给 node 跑，重点是注入）—— 它渲染的是 reviewer
  的输出，即不可信文本，没测试不敢发。**其余那些（走势图、findings 折叠、
  diff 高亮、SSE 收敛）仍然只有手工验过**：它们错了就是显示不对，不会像 md
  渲染那样变成安全问题。补齐要引入一整套浏览器测试依赖，对一个单文件面板不划算
  —— 现状是明确的取舍，不是遗漏。

  好在切分之后这块的爆炸半径小了：前端错了就是显示不对，不会像以前那样
  牵动进程管理（那部分整个搬进核心了，有 `test_api_*` 守着）。


- `stalled` 出口只有纯函数层的 `detect_stall` 覆盖，没有端到端用例（要造连续 3 轮分数持平的剧本）。`converged` / `needs_work` / `exhausted` / `inconsistent` / `pinned_scope` / `failed` 都在 fake-agent 那一档实跑过。
- `notify()` 的 `macos` 与 `cmd` 两个分支、`rloop list/status/logs/report/replay/stop` 六个子命令，都还没有自动化用例。
- KeyboardInterrupt 中断路径没测。loop 被中途杀掉会停在 `status=running`，下一次裸调 `rloop` 会**接管**它（拿到锁却发现是 running，说明上个进程死了）；那一轮若没留下可用的 `review.json`，会退回去重跑该轮。
- 沙箱档位这件事，在 fake-agent 那一档只验到「命令行参数对不对」，两个 CLI **真的**放开到哪、`--no-verify` 真的拦不拦得住，靠的是上面的手工实证记录，没有自动化用例（要真跑模型）。
- **动手证据靠扫日志，不是靠内核**。`reviewer_write_evidence` 认的是 codex 的 `file_change` 事件和一小撮明确的写命令（`sed -i`、`git commit`、往源码文件重定向……）。刻意绕过它不难（比如用 python 脚本写文件），只是那已经不是「模型顺手改了一下」而是「模型在躲检测」了。claude 侧没有等价的事件流，扫不到东西，所以那边只会提醒、不会作废。
- **指纹是失败开放的**。git 挂了或者超时，指纹就拍不全，reviewer 照样带着写权限跑，只是没人核对它。这时 loop.log 里会明说拍到了几个维度 —— 但不会自动降级成只读。
- **放开之后仍有跑不了的测试**。`workspace-write` 不给网络也不给进程可见性，绑端口、起本地服务、`ps` 探进程的用例照样红。context pack 里教了 reviewer 怎么认出这类失败，但认错了它就会拿环境限制去开 finding —— 这一层没有自动化保障。
