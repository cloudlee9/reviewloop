# rloopgui — rloop 的显示层

**整个删掉，rloop 照常工作。** 核心（`../rloop.py`）不 import 这里任何东西，
这里也不 import 核心 —— 两边只通过 `rloop api <verb>` 的 JSON 说话。

```bash
rloop web                 # 核心把面板拉起来，并用 RLOOP_BIN 指回自己
python3 -m rloopgui web   # 或者直接起，自己找 rloop
python3 -m rloopgui check # 只做一次握手：核心是什么版本、有什么能力
```

## 为什么切开

上一版的面板 `import rloop`，直接调 `collect_loops`、`Loop`、`kill_pgid`。
后果不是「耦合」这种抽象说法，是三件具体的事：

1. **枚举各抄一份，抄漏了静默失效。** `STAT` 表把核心的 `running → accent`
   抄成了空字符串；`SEV_STYLE` 抄了一份；退出码映射存在三处；进度前缀
   （`$` `↳` `!` `·`）的匹配表两处各一份。改核心一个符号，两个面板同时变灰，
   而且不报错。
2. **进程管理散在界面层。** 两个面板各写一份 `Popen` + pump 线程 + 杀进程，
   其中 TUI 那份漏了 `start_new_session=True`，停的时候 codex 孙进程留下来接着跑。
3. **谁不拥有 reviewer 进程谁就看不见进度。** 进度只 print 到自己的 stdout，
   一个字节不落盘 —— 这才是两个面板都被迫自己起 rloop 的根因。

切开之后这三条各自有了归宿：配色随数据来、进程管理归 `api run`/`api stop`、
进度落盘成 `progress.ndjson` 由 `api events` 分发。

## 结构

| 文件 | 干什么 |
|---|---|
| `client.py` | **整个包里唯一知道 rloop 存在的文件。** 起子进程、解析 JSON |
| `contract.py` | 认得的契约版本 + 一份兜底枚举（运行时以 `api meta` 为准） |
| `errors.py` | 面板侧的错误。给用户看的话必须能照着做 |
| `web.py` | HTTP 层：鉴权、把请求翻成 CLI 调用、SSE 转发 |
| `page.html` | 页面本身。独立文件，不再内嵌在 Python 字符串里 |

## 调用总表（这就是全部的核心依赖，没有第八行）

| 界面动作 | 调用 |
|---|---|
| 启动探针 + 枚举表 | `rloop api meta` |
| 初始列表 / 跨终端感知 | `rloop api --api 1 events --state --follow`（长活） |
| 选中某个 loop | `rloop api --api 1 loop <id>` |
| diff / 日志 / 报告 / 各种产物 tab | `rloop api --api 1 file <id> --what W` |
| 进度 tab | 同一条 `events` 流加 `<id> --round N --since S` |
| 「审一轮」/「新 loop」 | `rloop api --api 1 run --project P [--new] [...]` |
| 「停」 | `rloop api --api 1 stop <id>` |

## 两条不能破的规矩

### 一、面板是观察者

**契约里没有任何写产物的 verb，尤其没有写 `response.md` 的。**
这不是遗漏，是这条约束的执行方式：能力不存在，任何语言写的面板都长不出
「处理 findings」的按钮。

理由：按下去的必然是个没有上下文的陌生会话，不知道这些代码为什么写成现在这样。
处理那一步归有上下文的那一方 —— 你的开发会话（见 `~/.claude/skills/rloop/`）。
面板给的是「复制为 markdown」按钮和 `response.md` 的完整路径。

### 二、判断在核心，颜色在面板

面板**不做任何** status → class 的判断。`status_class` / `severity_class` /
`verdict_class` / `delta` / `meets_gate` 都是核心算好随数据一起发的，
面板只做 class → 颜色这一层薄映射。

`contract.py` 里那份兜底表只在 `api meta` 拿不到时用。遇到表里没有的值，
渲染成中性色**并在界面上说一声**，不许静默降级 —— 静默变灰正是上一版栽的地方。

`tests/test_gui_isolation.py` 扫 AST 盯着这两条，不许 skip。

## 渲染层归属

一句话：**同一份数据、同一套语义分类，渲染按介质分。**

- 语义分类（status→class、severity→class、delta、meets_gate）**当数据发**。
- 文本布局：核心的 `render_detail` / `render_markdown` / `wrap` / `clip` 原地不动
  （核心自己是第一用户：终端输出和每轮的 `review.md` 都走它）；
  网页用 HTML 渲染，两者不再有调用关系。
- 想看与终端逐字一致的东西？`review.md` 和 `report` 两个 tab 就是核心自己
  渲染的成果，原样显示。

## 换语言重写

`tests/test_gui_client.py` 的 15 条用例全部不碰真 rloop（假 rloop 是个几十行
脚本）。这本身就是论点：只靠一份 JSON 契约就能测通，说明换 Go 或 TypeScript
重写是同样的工作量。

找 rloop 的顺序：`$RLOOP_BIN` → `PATH` 上的 `rloop` → 同仓库相邻的 `rloop.py`。

## 还没测的

`page.html` 里那 200 行前端 JS 只有手工验过 —— 补它要引入一整套浏览器测试
依赖，对一个单文件面板不划算。服务端那层有 `test_gui_web.py`，客户端有
`test_gui_client.py`，中间这一段是明确的取舍。
