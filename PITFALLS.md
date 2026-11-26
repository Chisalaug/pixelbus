# 踩坑记录（PITFALLS）

这里记的是做 pixelbus 过程中**真实踩过、真实查了半天**的坑。每条都写清楚：现象 → 原因 → 怎么修的 → 怎么提前发现。

写它的原因很简单：pixelbus 这类"像素级判定 + 屏幕抓取 + 状态机"的东西，坑都藏在**看起来理所当然的假设**里——"阈值取最大方差的点就行""两个字之间肯定有空白""`on:` 肯定是字符串""小区域抓图本来就便宜"。这些假设错得悄无声息，读数永远是 0、状态永远不触发、CPU 莫名其妙高，但代码不报错。

按里程碑编号。新坑继续往下加。

---

## M0：骨架与判据

### 0.1 大津法在纯黑白图上把"墨"当成了阈值

**现象**：合成一张"白底 + 纯黑方块"的图，`ink_ratio` 恒等于 0，`glyph_runs` 永远是 0 簇。图明明是黑字白底。

**原因**：Otsu 的思路是"让分出来的两类的类间方差最大"。可纯黑白图只有 0 和 255 两个灰度值，任何把 0 和 255 分到两边的阈值（t=1 … 254）类间方差**完全相等**，是一整段平台。`np.argmax` 取第一个最大值，落到 t=0 或 t=1 —— 阈值等于"墨"本身的灰度，`gray < thr` 一个像素都不剩。

**修法**：取等高分平台的**中点**，而不是第一个最大值：

```python
plateau = np.flatnonzero(sigma >= best * 0.98)   # 98% 以内都算"一样好"
return int(plateau[len(plateau) // 2])
```

再加一道兜底：`best <= 0`（整张图一个颜色）时返回 128。

**自检**：单测里专门放一张纯黑白图，断言阈值落在 (10, 245) 区间内。只测"有墨/无墨"的图发现不了这个坑，因为真实照片有灰阶过渡时 argmax 恰好是对的。

### 0.2 两个字被当成"一个字形"

**现象**：`glyph_runs: 1` 想判"这里是不是一个箭头"，结果弹窗里"危急值"三个字也被判成 1 簇，箭头判据恒为命中。

**原因**：簇的合并逻辑用"两簇之间有没有空隙"来判断。实测微软雅黑 60px 的「正确」两个字之间只有 **2 列空隙**，而且其中 **1 列带淡墨**（抗锯齿的灰边）。只要"缝隙里有墨就合并"，中文字间距必然被吃掉。

**修法**：两级遮罩 + 严格限制桥接宽度：

- `strong` 遮罩（确定的墨）用来**定簇**；
- `weak` 遮罩（阈值放宽 45 个灰阶）只用来回答一个问题："这条缝是不是抗锯齿？"；
- `bridge_max` 默认 **1**：只允许 1 列淡墨缝隙被桥接。宁可让 2 列的字间距裂成两簇（判"多字形"正确），也不能把一句话合成一个箭头（判"单字形"错误）。
- 另外 `gap_px`（无条件合并）默认 0，`min_area`(40) / `min_width` 过滤噪点。

**顺带**：文本光标（1px 竖线）会额外贡献 1 簇，所以加了 `denoise`（开运算）。开运算当时也踩了个小坑：`_erode` / `_dilate` 的初值不能写 `np.ones` / `np.zeros`，要 `mask.copy()` 再逐方向 AND / OR —— 否则边缘会被 padding 的常量吃掉一圈。

**自检**：单测同时放"箭头"和"两个汉字"两张图，断言前者的 `runs == 1`、后者 `>= 2`。只测箭头永远发现不了。

### 0.3 PyYAML 把 `on:` 解析成了布尔 `True`

**现象**：YAML 里写

```yaml
actions:
  - {on: alarm_on, do: {webhook: "..."}}
```

加载后报错 `action 里不认识 [True]`，而 `alarm_on` 明明定义了。

**原因**：PyYAML 走的是 YAML **1.1** 的隐式类型解析表，`on` / `off` / `yes` / `no` 都会被转成布尔。`on:` 这个键本身变成了 `True` 这个 key，于是整个 action 的字段名都错位了。契约里 `on:` 是语义字段名，不能动。

**修法**：自定义 loader，把 bool resolver 剥掉，只保留真正的 `true/True/TRUE/false/False/FALSE`（YAML 1.2 语义）：

```python
class _Loader(yaml.SafeLoader):
    pass

_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
```

`loads_rules()` 一律走这个 loader，不再用 `yaml.safe_load`。

**自检**：单测断言 `rules.actions[0].on == "alarm_on"`（字符串），而不是 `True`。

### 0.4 契约把 `text` 写在 `do:` 里面

**现象**：`do: {webhook: "https://...", text: "危急值弹窗"}` 被校验器拒绝，说"不认识 action 类型 text"。

**原因**：第一版把 `do` 的每个键都当成"动作类型"，于是 `text` 也被当成一种动作。

**修法**：`kinds = set(do) - {"text"}`，`text` 作为动作的**属性**单独取（`item.get("text")` 优先，其次 `do.get("text")`）。这样 `{on: ..., do: {webhook: ..., text: ...}}` 和顶层写 `text:` 两种写法都成立。

**自检**：单测覆盖两种写法，都断言解析出同一段文本。

---

## M1：引擎与 CLI

### 1.1 测试里写 `{"r": whites := white_frame()}` 直接语法错误

**现象**：`SyntaxError: invalid syntax`，整份 `test_engine.py` 收集失败。

**原因**：海象运算符 `:=` 只能出现在"允许表达式"的位置。字典**值**位置的赋值表达式需要括号：`{"r": (whites := white_frame())}`。而 `{"r": whites := ...}` 会被解析成"字典值 + 注解/关键字参数混用"，直接语法错误。

**修法**：把变量提到外面单独赋值。这类错误连 import 都过不去，`pytest` 只会告诉你"收集失败"，不告诉你是哪一行语义错了——所以报错行号一定要看，别以为是测试逻辑问题。

### 1.2 `Snapshot.to_dict()` 引用了一个不存在的字段

**现象**：`AttributeError: 'Snapshot' object has no attribute '_dpi'`，跑 snapshot 相关用例才炸。

**原因**：`to_dict` 里写 `self._dpi`，但 `Snapshot` 是个纯数据类，从来没存过 `_dpi`（`_dpi` 是 `Engine` 的私有字段）。写代码时"脑子里想的是 Engine"。

**修法**：`Snapshot` 显式加 `capture_dpi: str = ""` 字段，`Engine.snapshot()` 填入，`to_dict()` 读它。数据类就用数据类的字段，别顺手去摸别处。

**自检**：单测断言 `set(snapshot.to_dict())` 的键集合 —— 少一个字段就挂。

### 1.3 引擎把"定义了的区域"全抓了一遍

**现象**：单测 `test_one_grab_per_region_even_with_many_signals` 失败：2 个区域、2 帧，期望抓 4 次，实际抓了 6 次。也解释了为什么示例规则里那个没人用的区域白吃 CPU。

**原因**：`step()` 里遍历 `rules.regions`，而不是"被 signal 引用到的区域"。

**修法**：`_build()` 里算

```python
self._used_regions = sorted({cfg.region for cfg in rules.signals.values() if cfg.region})
```

`step()` 只抓这些。定义区域不花钱，用区域才花钱。

**自检**：假抓屏器数调用次数 —— 这是唯一能发现"多抓了一次"的方式，看代码看不出来。

### 1.4 "首帧只是基线"是个错误的直觉

**现象**：一堆去抖用例按"第一帧不算"来写，13 个用例集体失败。

**原因**：引擎的真实语义是"**首帧命中就报 appear**"——如果你的程序启动时告警弹窗已经在屏幕上，那必须在第一帧就告诉你，等到第二帧才知道就是漏报。同理 `stable_frames: 1` 的 state 首帧即 active；同一 tick 内后面的 state 可以引用前面刚成立的 state（按声明顺序级联）。

**修法**：改测试，不改代码。但顺手修了两个**真的不一致**的地方：

- `state` 的消失原先"第一帧没命中就掉"，和"连续 N 帧才算"不对称 → 加了 `miss` 计数，`stable_frames` 双向生效（出现要连续 N 帧，消失也要连续 N 帧）。
- `diff` 在 `set_rules()` 之后基线会重置，所以重载后的**第一帧变化率必然是 0**，第二帧才看得出变化。这不是 bug，是"重载后重新建立基线"的设计，测试要按这个语义写。

**教训**：去抖/边沿这类逻辑，"每帧做什么"的语义最好写进 docstring，测试按 docstring 写，别按直觉写。

### 1.5 `ctypes.windll.psapi` 静默返回全 0

**现象**：基准脚本报"内存峰值 0.0MB / 结束 0.0MB"，像是没测；实际上进程占着几十兆。

**原因**：`ctypes.windll.psapi.GetProcessMemoryInfo(handle, ...)` 不声明 `argtypes` 时，Python 会把 `GetCurrentProcess()` 返回的 HANDLE（64 位指针）按 `int` 传，`GetProcessMemoryInfo` 直接失败，返回值是 0，而结构体里啥也没写。**调用失败但不抛异常**，于是读到全 0。

**修法**：显式声明签名：

```python
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
psapi.GetProcessMemoryInfo.restype = ctypes.c_int
ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(info), info.cb)
```

并且**检查返回值** `ok`，失败就 `nan`，不要假装拿到 0。

### 1.6 用"墙钟 ÷ 帧数"测 CPU，能测出 3% 也能测出 7%

**现象**：同一份代码、同一台机器，两次基准跑出 5.1% 和 7.3%；换个写法测又变成 3.5%。

**原因**：`process_time 增量 / perf_counter 增量` 把**调度抖动、别的进程抢 CPU、虚拟机被宿主抢占**全都算了进去。屏幕还在播动画时，抓屏线程被调度得忽快忽慢，比值就不稳。

**修法**：**逐帧计时**。每帧前后各取一次 `process_time()`，把差累加，再折算：

```python
per_frame_ms = cpu_total / steps * 1000
cpu_pct = per_frame_ms * fps / 10.0    # 毫秒/帧 × 帧/秒 → 千分之一秒/秒 → 百分数
```

这样测的是"我们这帧花了多少 CPU"，不受调度噪声影响。**顺带一个单位坑**：`ms × fps` 得到的是"毫秒每秒"，要除以 10 才是百分比，第一次写成 `ms * fps` 直接报出 27%。

### 1.7 mss 的 `CAPTUREBLT` 让小区域抓图贵 5~6 倍

**现象**：499×108 的小区域，抓一次 0.52ms；关掉 `CAPTUREBLT` 之后 0.08~0.21ms。区域越小，差距越离谱（全屏反而几乎没差别，因为瓶颈是带宽）。

**原因**：mss 在 Windows 下固定用 `SRCCOPY | CAPTUREBLT` 做 BitBlt。`CAPTUREBLT` 的作用是"把分层窗口（`WS_EX_LAYERED`）也合成进来"，代价是每次 BitBlt 要做额外的合成工作，于是**每次调用的固定开销**从 ~0.08ms 涨到 ~0.5ms。三个区域 15fps，这就是 1.5% 的 CPU 白扔。

**修法**：自己控制这个 flag —— 判据是"到底有没有分层窗口内容会被漏掉"，而不是"猜"：

1. `Grabber` 找到 mss 内部那个模块级常量（`mss.windows.gdi.CAPTUREBLT`），抓图时临时置 0、抓完恢复；
2. 启动时做一次**自检**：全屏连抓三张 —— `fast` → `compat` → `fast`。第 1、3 张不一样说明屏幕自己在动、这次测不准；一样的话，只要 `compat` 和 `fast` 不同，就说明确实有分层窗口内容会被漏掉，于是永久退回兼容模式；
3. 之后每 30 秒复检一次；测不准就**指数退避**（5s → 10s → … → 60s），否则每次复检 3 张全屏抓图（约 120ms）反而拖慢主循环、丢帧。

**坑中坑**：最初的做法是"枚举所有分层窗口，看区域有没有被盖住"。这台机器上枚举出来 8 个分层窗口，其中一个 **1920×1080 全屏的 Qt 工具窗口**（`Qt6120QWindowToolSaveBits`，做窗口 backing store 用的，实际几乎全透明）。于是**每个区域都被判成"被盖住"**，快路径一次都没走成。按窗口类名/alpha 去过滤都不可靠（`UpdateLayeredWindow` 的窗口拿不到全局 alpha），所以最后改成"实测比对像素"——只信事实，不信窗口属性。

**注意**：改第三方模块的模块级常量是进程级副作用，多线程同时抓图会打架。pixelbus 只有一条抓屏线程，够用；如果你要在多线程里用，自己加锁。

### 1.8 `np.array_equal` 复用判据结果的边界

**现象**：想省 CPU，加了个"这块区域整帧没变就复用上一帧判据结果"的快路径。结果在这台机器上 CPU 反而涨了（5.1% → 6.8%），因为桌面一直在动，**永远不命中**，却每帧都付了"复制 + 全图比对"的钱。

**修法**：
- 只存**引用**不复制 —— mss 每次抓图都给新数组，不会被原地改；只有测试里传进来的帧才 `copy()`（用户可能原地改）。
- 命中的时候才更新缓存；不命中就是"复制 + 比较 + 全量重算"三重成本，所以这条路径只在**真的静止**的界面上赚（HIS/报表这种静态界面命中率很高，命中时每帧从 1.8ms 掉到 0.26ms）。
- 别用"抽样哈希"代替全图比对：漏掉一个像素的变化，就等于漏掉一次告警。

### 1.9 tkinter 的坐标是"逻辑像素"，抓屏要的是"物理像素"

**现象**：125% 缩放下框选一块区域，`probe` 立刻报"区域里什么都没有"。

**原因**：开了 DPI 感知后，进程按物理像素工作，但 tkinter 报的 `winfo_screenwidth()` / 鼠标事件坐标来自 Windows 的**逻辑坐标系**（缩放后的）。两者差一个缩放系数，框小的区域抓到的位置就偏了。

**修法**：用 mss 报的物理宽度除以 tk 报的逻辑宽度，得到真实缩放比，再把所有坐标换算过去，并在 stderr 明确打印一行"检测到缩放 X%，坐标已换算"——**别静默换算**，不然用户永远不知道为什么框偏了。另外遮罩用 `overrideredirect(True)` + `attributes("-alpha", 0.28)` 全屏铺开，才看得见底下的界面同时能拖框。

### 1.10 `python -m pixelbus` 需要 `__main__.py`

**现象**：`python -m pixelbus validate rules/example.yaml` 报 `No module named pixelbus.__main__`。

**原因**：`pixelbus` 是个包，`python -m 包名` 要求包里有 `__main__.py`（或在 `__init__.py` 里塞 `__main__` 逻辑）。光配了 `[project.scripts]` 只能保证装完之后有 `pixelbus` 命令，不保证 `-m` 能用。

**修法**：加一个两行的 `pixelbus/__main__.py`，`from .cli import main; sys.exit(main())`。开发期（没 pip install）也就能跑了。

### 1.11 Windows 上端口锁要用 `SO_EXCLUSIVEADDRUSE`

**现象**（预防性）：用 `SO_REUSEADDR` 做"单实例锁"，第二个进程照样能绑上同一个端口，锁形同虚设。

**原因**：Windows 的 `SO_REUSEADDR` 语义和 Linux 不同，它允许**更宽松**地抢占，不像 Linux 那样是"允许 TIME_WAIT 复用"。要做互斥得用 `SO_EXCLUSIVEADDRUSE`。

**修法**：`PortLock` 在 Windows 上用 `SO_EXCLUSIVEADDRUSE`，其它平台用 `SO_REUSEADDR`。另外绑成功≠"是我们自己"，所以还要 `probe_owner()` 打一发 `GET /health` 校验 `body["app"] == "pixelbus"` —— 不然占了 8788 的可能是别的程序，用户会看到"已经有 pixelbus 在跑了"这种误导提示。

### 1.12 抓屏失败不要崩，要能自愈

**现象**（预防性）：改分辨率、拔副屏之后，`mss` 抓一个已失效的矩形会抛异常，一崩整个循环就死。

**修法**：`_grab()` 里 catch 住、计 `grab_errors`、**重算一次 bbox 再试一次**；还失败就当这一帧没数据（`None`），signal 保留上一帧状态而不是归零——否则一次显示器切换会伪造出"弹窗消失了"的事件流。

---

## M2：HTTP 与 SSE

### 2.1 跨线程投递：`queue.put_nowait` 在别的线程里是不生效的

**现象**：`/events` 连上了、`retry:` 也发了、`/state` 里明明有事件在产生，但客户端**一条 `data:` 都收不到**，只有每 15 秒一行 `: ping`。

**原因**：引擎跑在 `Engine.start()` 起的线程里，uvicorn 的 event loop 在另一个线程。`asyncio.Queue` 不是线程安全的，在引擎线程里直接 `put_nowait` 不会唤醒 loop 上的 waiter —— 事件进了队列但没人被通知取走，看起来就是"静默丢事件"。

**修法**：`Broker` 在 app 的 `startup` 钩子里 `bind_loop(asyncio.get_running_loop())`，发布时用 `loop.call_soon_threadsafe(_safe_put, q, body)`。**loop 一定在 startup 里拿**，不能在 `create_app()` 时拿 —— 那一刻 uvicorn 的 loop 还没跑起来，`get_event_loop()` 给的是另一个（甚至已关闭的）loop，投进去等于扔进黑洞，而且**不会报错**。

### 2.2 SSE 被"攒到 4KB 再吐"

**现象**（预防性，别人踩过）：浏览器里 EventSource 一直不触发，`curl` 却是通的。

**原因**：中间代理/某些容器会缓冲响应体，凑够一块才发出去。

**修法**：三件事一起做 —— `media_type="text/event-stream"`、响应头加 `X-Accel-Buffering: no`、正文第一行先发 `retry: 3000`（既是重连间隔，也顺手把"首字节"顶出去）。再加 15 秒一次的 `: ping` 注释行，免得空闲连接被中间设备当成死链掐掉。

### 2.3 慢客户端不能把引擎拖死

**现象**（预防性）：一个卡住不读的 SSE 客户端，会让整条事件流水线堵在 `await q.put()` 上。

**修法**：订阅队列定长 512，发布方永远 `put_nowait`，满了就**丢最老的那条**而不是阻塞或丢最新 —— 状态类事件的价值在于"最新"，历史可以不要。这条在单测里钉死了（`test_broker_drops_oldest_when_client_lags`）。

### 2.4 `curl -N ... | Select-Object -First 20` 会把流掐断

**现象**：想验证 SSE，跑 `curl.exe -s -N http://127.0.0.1:8787/events | Select-Object -First 20`，只看到 `retry: 3000` 和一行空行就结束，误判成"没有事件"。

**原因**：`Select-Object -First N` 拿够行就关闭管道，`curl` 被 SIGPIPE 掉，后面的 `data:` 还没吐出来就断了。

**修法**：验证 SSE 用 `--max-time` 限时（`curl.exe -s -N --max-time 6 http://.../events`），让服务端自己决定什么时候结束。

### 2.5 TestClient 需要 `httpx`，而它不在核心依赖里

**现象**：`python -m pytest tests/test_server.py` 直接 `ModuleNotFoundError: No module named 'httpx'`。

**原因**：`fastapi.testclient` 是 httpx 的薄封装；`httpx` 只在 `[dev]` extra 里，`pip install -e .` 不会带上。

**修法**：写 HTTP 测试前先 `pip install -e ".[dev]"`（或单独 `pip install httpx`）。另外 `starlette.testclient` 目前会提示"用 httpx 已弃用、请装 httpx2"，属于上游噪音，不影响功能。

### 2.6 拿一整个屏幕当 `diff` 区域，10fps 打不满

**现象**：临时规则用全屏 `rect: [0,0,1,1]` + `diff: {min: 0.002}` 验证 SSE，`--duration 10` 只跑到 **8.4fps**（87 帧 / 10.3s），而 `fps: 10`。

**原因**：全屏 `diff` 单独量出来就是 **~35ms/帧**（1200×1920，`downsample: 2`，含抓图）。小区域 `diff` 是 0.2~2ms，差两个数量级 —— 像素量摆在那里。

**修法**：`diff` 只用来盯"某个小方块有没有变"（状态栏、按钮、表格一格）。要大范围感知变化，就把区域切小、把 `downsample` 提到 4、或者接受更低 fps。**别指望全屏 diff 跑 10fps。**

### 2.7 `on_event` 在新版 FastAPI 里是"弃用但还能用"

**现象**：用 `@app.on_event("startup")` 绑 loop，暂时没有任何警告，但文档已经不推荐。

**修法**：这个版本照用没问题（lifespan 端点写法在多文件/多 app 场景反而更容易把 loop 拿错线程）。如果后续升 FastAPI 到移除 `on_event` 的版本，改成 `lifespan=asynccontextmanager` 上下文，**但 loop 依然要在 lifespan 入口里取**（见 2.1）。

### 2.8 `Issue.where` 是字符串，JSON 里被逐字符拆成了 `s/i/g/n/a/l/s`

**现象**：`POST /reload` 传一份错的 YAML，`message` 里明明写着 `signals.s.region`，但 `issues[0].where` 返回 `"s/i/g/n/a/l/s/./s/./r/e/g/i/o/n"`。

**原因**：`Issue.where` 在这个项目里是**点分字符串**（`"signals.s.region"`），而我按"路径是元组"的直觉写成了 `"/".join(str(p) for p in issue.where)` —— 对字符串来说就是逐字符拼接。Python 里 `for p in "abc"` 不报错，所以这个 bug 一路无声地活到了 `curl`。

**修法**：先判类型，字符串原样用，序列才 join。单测里直接断言 `where == "signals.red.region"`，把"是不是被拆开"钉死——**只断言 message 里有错词的测试，抓不到这个 bug**。

### 2.9 `run` 启动比 `--no-server` 慢约 0.9 秒

**现象**：`run` 敲下去之后要等一会儿才开始打帧；`--no-server` 明显更快。

**原因**：`import fastapi, uvicorn` 单算 **1.47s**，而核心依赖 `numpy + mss + yaml` 是 **0.55s**。HTTP 不是白来的。

**修法**：`server.py` 只在 `run` 需要时懒导入（`probe` / `doctor` / `validate` 完全不碰 fastapi），所以只有 `run` 付这笔钱。如果你只是想看事件、不打算订阅，`--no-server` 能省掉这 0.9 秒。

### 2.10 端口被占时 uvicorn 只往 stderr 打一行 ERROR，主线程"假装跑起来了"

**现象**：故意用别的程序占住 `--port`，`run` 照样打印 `state: http://...`、`events(SSE): http://...`，然后正常跑完、`exit 0`。用户以为服务起来了，`curl` 却连不上。唯一的线索是混合在输出里的一行 uvicorn `ERROR: [Errno 10048]`。

**原因**：两层都没接住 ——
1. uvicorn 绑定失败时是 `sys.exit(1)`（`SystemExit`，属 `BaseException`），我的 `except Exception` 抓不到，线程静默死掉；
2. `_wait_ready` 的判断是"端口能连上就算成功"，而且"超时不算失败"。别人开的监听 socket 能被 `create_connection` 连上（假成功），超时又直接放过（真失败也当成功）。

**修法**：`_run` 改成 `except BaseException`；`_wait_ready` 只认 uvicorn 自己的 `Server.started` 标志，超时**必须抛**；抛之前把 `SystemExit: 3` 翻译成"端口大概被别的程序占了"。CLI 收到异常就打印建议（换端口 / `--no-server`）并 `exit 1`，而不是继续跑。

**自检**：单测里开一个真监听 socket 占住端口，断言 `ServerThread.start()` 抛 `RuntimeError`；另一条用例反向验证"起来了能 `GET /health`，停了之后端口立刻能重新独占绑定"。

---

## 环境：框选窗口（M2 验收时才炸出来的）

### 2.11 `import tkinter` 成功 ≠ 能开窗：`Can't find a usable init.tcl`

**现象**：让用户试 `pixelbus probe rules.yaml --pick alarm`，用户反馈"啥都没有，控制台也没有"。进程确实起来了、也立刻退了，控制台窗口闪一下就没（所以连报错都没看见）。

**原因**：这台机器上 pixelbus 跑在某个 AI 运行时自带的 **relocatable Python** 里。它把 `tkinter` 模块和 `tk86t.dll` 都带上了，但 **Tcl 的脚本数据（`init.tcl`、`tk.tcl`）被裁掉了**。于是：

```python
import tkinter          # 成功
tkinter.Tk()            # _tkinter.TclError: Can't find a usable init.tcl in the following directories: ...
```

也就是说"有没有 tkinter"这个判断维度是错的 —— **能 import 只是说明 .pyd 在，能不能开窗取决于 Tcl 的运行时数据在不在**。

**修法**（三层）：

1. **GUI 抽成独立文件** `pixelbus/_pick_gui.py`，只用标准库、只往 stdout 打一行 JSON、兼容老版本语法（可能落到 3.8 的解释器上）。父进程 `picker.py` 只负责"挑解释器 + 解析结果"。
2. **挑解释器时真跑一次 `tkinter.Tcl()`**（不弹窗）：先 `PIXELBUS_TK_PYTHON`（手动兜底），再 `sys.executable`，再 `py -3.14 / -3.13 / -3.12 / -3`，再 PATH 里的 `python3/python`。第一个 `returncode == 0` 的胜出，结果缓存，避免每次框选都探半天。
3. **子进程失败必须抛错，不能当成"用户按了 Esc"**：两者语义完全不同 —— 一个该提示"换个解释器"，一个该安静返回 `None`。`_run_gui` 里把子进程 stderr 最后一行抬成 `RuntimeError`。

**自检**：用 Win32 的 `SetCursorPos + mouse_event` 在**绝对像素**上发一次拖拽（绕开任何坐标缩放层），拖 `600,400 → 1000,700`，拿到的结果是 `rect [0.314, 0.371, 0.207, 0.277]` / `bbox 603,401,397,299` —— 和期望的 600/400/400/300 只差 1~3px（鼠标事件采样误差）。坐标换算是对的，不是"框完偏到别处去"。

**顺带记两条**：

- `doctor` 里那条 `tkinter ✓` 以前只 `import tkinter`，在这个环境里会给出**假的通过**。现在改成打印"框选会用哪个解释器"，`✗` 时提示设 `PIXELBUS_TK_PYTHON`。
- 别把窗口做成"父进程里直接开 Tk"再配一堆 fallback —— 一旦父解释器开不了窗，所有 fallback 都还在同一个坏解释器里。**只有换进程才换得掉"哪个 Python 在开窗"这件事。**

---

## M3：动作分发

### 3.1 `engine ↔ actions` 循环导入，收集测试就炸

**现象**：`engine.py` 顶层 `from .actions import ActionRunner`，`actions.py` 顶部又 `from .engine import Event`，pytest 收集测试直接 `ImportError: cannot import name 'ActionRunner' from partially initialized module`。

**原因**：两个模块在导入阶段互相拿对方的类。`actions.py` 其实只把 `Event` 当类型标注用（运行时读 `.kind/.name/.t/.to_dict()`），并不需要真的 import engine。

**修法**：断掉反向依赖——`actions.py` 不 import engine，事件参数标成 `Any`，靠 duck-type 读属性。类型层面"引用"不等于"导入"，需要收紧时再从一个不依赖引擎的地方把事件结构挪出去。

**自检**：`python -m pytest --collect-only` 必须先过；收集阶段炸的循环导入，跑到用例里反而更难定位。

### 3.2 `log: "消息"` 的值是正文，被当成了动作类型的额外参数

**现象**：规则写 `{log: "告警弹窗"}`，动作确实触发了，但打出来的是兜底描述 `appear state:hit lvl=1.00`，正文"告警弹窗"丢了。

**原因**：初版 `_log` 只认 `action.text`（写在 `do.text` 里的那条），而 `log` 这个键的值本身就是日志消息。`kind_of` 已经把 `text`/`payload` 排除在动作类型之外，消息提取却没跟着一起放宽。

**修法**：优先级 `action.text` → `do.log` 的字符串值 → 事件描述兜底。webhook / mqtt 的正文取值同理：`text` → 专用 `payload` → 事件 JSON。

### 3.3 `worker=False` 时动作被塞进队列，却没有线程去消费

**现象**：单测里构造 `ActionRunner(..., worker=False)`，引擎 `step` 之后 `status.fired == 0`，一条动作都没跑。

**原因**：`handle()` 里只看 `sync` 标志决定"内联还是入队"，没看"有没有工作线程"。关掉工作线程只是不 `start()` 线程，队列照样在塞，于是请求永久躺在 `queue.Queue` 里。

**修法**：分发条件改成 `self._worker is None or sync` —— 没有工作线程就必须内联执行，`sync=True` 只是"默认带线程也同步跑"的口子。

### 3.4 引擎已经分发过一次动作，测试里又手动 `handle` 一遍，同一条事件触发两次

**现象**：`test_log_action_fires_on_state_appear` 断言 sink 只有 1 条，实际 2 条；`when: disappear` 的用例里 appear 步和 disappear 步各记了一条，多出一条"消失"。

**原因**：`engine.step()` 内部本来就会对每个事件调 `runner.handle(event)`。测试为了断言"动作被调了什么"，又拿事件列表手动喂了一遍 runner —— 两条路径各触发一次。`when=disappear` 那条看似是"appear 误触发"，其实也是同一条 disappear 事件被喂了两次。

**修法**：引擎集成测试里**不要再手动调 `handle`**，直接 `step` 然后断言 sink / status；只有"不经过引擎、直接喂 Event"的纯 runner 用例才手动调。判读这类失败先想"是不是哪里重复投递了"，别急着改 `_kind_match`。

### 3.5 YAML 双引号字符串里的 Windows 路径反斜杠被当成转义序列

**现象**：`exec` 的命令列表在 YAML 里写成 `["C:\Users\swagger\...\python.exe", ...]`，规则校验直接报 `expected escape sequence of 8 hexadecimal numbers, but found 's'`（`\s` 不是合法 YAML 转义）。

**原因**：YAML 双引号串和 JSON 一样认 `\uXXXX` / `\xXX` 这类转义；Windows 路径里的 `\U`、`\s` 恰好踩中。单引号串不解析转义，或者路径用正斜杠都能绕。

**修法**：单测里凡是带绝对路径的命令，**绕过 YAML 直接构造 `ActionCfg`**，不把路径拼进 YAML 文本；规则文档里 `exec` 优先给列表写法，路径含反斜杠时用 YAML 单引号包起来。

### 3.6 可选依赖缺失要翻译成"装哪个 extra"，不能裸抛 ImportError

**现象**：本机没装 `paho-mqtt`，动作失败信息如果直接透出 `ModuleNotFoundError: No module named 'paho.mqtt.client'`，用户不知道该 `pip install` 什么。

**修法**：`press` / `mqtt` 在 `except ImportError` 里主动 `raise ActionError("需要 paho-mqtt：pip install pixelbus[mqtt]")`，把包名和 extra 名字一起给出来。单测用 `monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)` 强制制造缺依赖环境，断言报错文案里同时有包名和 extra 名——不真的连 broker。

---

## M4：MCP server

### 4.1 mcp 2.x 把 `FastMCP` 改名成了 `MCPServer`，import 路径都变了

**现象**：按 v1 教程写 `from mcp.server.fastmcp import FastMCP`，装完最新 `mcp` 直接 `ModuleNotFoundError`，报错里让你去看迁移指南；反过来先按 2.x 写，老版本环境又装不上。

**原因**：mcp SDK 2.0 做了一次破坏性更名：`FastMCP` → `MCPServer`，模块从 `mcp.server.fastmcp` 挪到 `mcp.server.mcpserver`。装饰器 `@server.tool()`、`server.run(transport=...)` 的用法两边基本一致，但 import 路径不兼容。

**修法**：`_import_mcp_server()` 先试 2.x 的 `MCPServer`，`ImportError` 再回退 v1 的 `FastMCP`；extra 放开 `mcp>=1.2`，两条路都能跑。单测里注册工具只调 `list_tools()`，不真正起 stdio 循环。

### 4.2 业务壳不能 import mcp，否则没装 extra 连测试都收集失败

**现象**：如果 `Bus` 里直接 `from mcp.server.fastmcp import ...`，开发环境没装 `mcp` 时 `tests/test_mcp.py` 一收集就 `ImportError`，等于把可选依赖变成了硬依赖。

**原因**：MCP SDK 是 extra（`pixelbus[mcp]`），核心引擎和动作分发不该被它绑死；测试又必须在没装 SDK 的 CI 上能跑。

**修法**：拆两层——`Bus` 只持有 `Engine`，方法返回普通 dict，完全不碰 mcp；`build_server(bus)` 才懒加载 SDK 做工具注册。`tests/test_mcp.py` 大部分用例直接挂合成引擎测 `Bus`；只有"四个工具都注册上了"那条用 `pytest.importorskip("mcp.server.mcpserver")`，缺 SDK 就跳过。

### 4.3 `wait_for` 必须先查当前状态，再订阅事件队列

**现象**：Agent 调 `wait_for("alarm_on")`，可弹窗其实早就挂在屏幕上了；只靠"订阅之后再来的事件"会一直等到超时。反过来，上一轮等待留下的老事件没清掉，下一次等待一进队列就被立刻匹配上。

**原因**：事件是边沿触发的——状态成立期间不会反复发 `appear`。等的人开始盯的时候，目标可能已经在命中态；而队列里残留的旧事件对新一次等待没有意义。

**修法**：进 `wait_for` 先 `engine.state_of(name)` 看当前值：`appear/any` 且已命中、`disappear` 且已不成立，立刻返回（带 `already_active/already_inactive` 标记）；否则加条件变量等待，**进入等待时先清空 pending**。匹配条件是"事件名 + 事件类型"，`any` 不看类型。

### 4.4 stdio 下 SDK 把 stdout 重定向到了 stderr，杂散输出不会污染报文

**现象**：本地手动起 server 调试时，往 `stdout` 打日志想看输出，结果 MCP 客户端收不到报文；反过来担心动作里 `print` 会把 JSON-RPC 报文冲乱。

**原因**：stdio 传输里 fd 1 被 SDK 接管重定向到 stderr，JSON-RPC 走的是另一份分流后的句柄；进程里其它代码的杂散 `print` 落到 stderr，不会进报文流。

**修法**：MCP 模式下动作的 `log` 走默认 print 是安全的；但工具返回值必须是 JSON 可序列化的普通 dict——别把 Engine 对象、numpy 数组、Event 实例直接抛给 MCP 层（`wait_for` 里统一用 `event.to_dict()` 转换）。

---

## M5：demo 与 GIF

### 5.1 真实截图含界面隐私，只进本地 shots/，不进 git

**现象**：demo 要对照"命中态 / 普通态"调阈值，截图里可能有真实患者信息、账号、界面布局；直接提交到仓库会把这些带出去。

**修法**：每个 demo 的 `shots/` 目录自带 `.gitignore`（`*` 只放行 `.gitkeep`），截图只留在本机；随仓库发的只有裁过区域、压成循环 GIF 的 `demo.gif`。录 GIF 的工具也只抓规则里那一小块区域，不录全屏。

### 5.2 录 GIF 前必须预热，否则 diff 判据第一帧就误命中

**现象**：`diff` 判据要记住上一帧当基线。冷启动第一帧没有基线，直接开录容易把"静止界面"也判成变化；GIF 开头几帧角标乱跳。

**修法**：`tools/record_gif.py` 先跑一段 `--warmup`（默认 1 秒）把引擎基线和判据状态稳住，再开始存帧。录之前把目标界面先调到要演示的状态，别边操作边录。

### 5.3 demo 规则里的坐标 / 阈值必须 probe 对着真机调，不能抄

**现象**：样例 YAML 里的相对坐标和颜色阈值是照"想当然的布局"写的，换到别的分辨率、主题、字体上必然不准；README 要是把这些数字写成"推荐值"，后人照着抄全是误判。

**修法**：三个 demo 的坐标全部用脚本在真机上标定过（找红条 / 找白板里的暗块 / 找粉底行），阈值取 probe 打出来的 hit / idle 中间值，并且把实测数字直接写进 YAML 注释（例如"命中 0.835 / 空闲 0.000"），后人一看就知道该怎么核对。单测只校验结构合法 + 几个"改坏了就必然误判"的关键字段（显式 threshold、pixel_tol、底图存在），不断言具体分数。

### 5.4 `diff` 的参考图没跟当前帧同采样率，变化率直接飙到 0.53

**现象**：给 `diff` 配了 `reference: baseline.png` 且 `downsample: 2`，一张"完全静止"的画面却读出 0.53 的变化率，一直 HIT。

**原因**：`Diff._current()` 会把当前帧按 `downsample` 抽稀，但 `_load_reference()` 读进来的底图是**全分辨率**。形状不一致时代码走"截断对齐"分支：`base[:cur_h, :cur_w]` —— 变成拿"底图左上角那一块"去和"降采样后的整帧"比，两者根本不是同一片像素。这个坑最阴的地方是"看起来有数、数还很大"，不像报错那样一眼能发现。

**修法**：读底图时按同一个 `downsample` 抽稀再返回（`pixelbus/predicates.py`），并补一条回归测试：同一张底图配 `downsample: 2`、同一画面进两帧，变化率必须是 0。

### 5.5 近白底 + 浅灰格线：auto（Otsu）阈值会把格线当成墨迹

**现象**：白板 demo 里箭头明明没出现，`glyph_runs` 却读出 1 个字形、`aspect` 读出 1.9，判据形同虚设。

**原因**：白板是纯白底 + `#eef0f3` 格线。自动阈值对"几乎全白"的图很敏感：Otsu 落在 247 这种极高位，浅灰格线（约 240）全被算成"深色墨迹"，一整格十字线正好被连成一个宽高比 1.9 的"字形"。

**修法**：`glyph_runs` / `aspect` / `ink_ratio` 全部显式写 `ink: dark, threshold: 120`，让箭头（灰度约 38）进、格线（约 240）出。单测里钉住这三个 threshold，删了就会红。

### 5.6 别拿 `centroid_dx` 当"箭头朝左"的判据

**现象**：想用"墨迹质心偏左"识别朝左的箭头，实测朝左的箭头 `centroid_dx` 反而是 **+0.041**（看着像偏右），阈值写成 `<-0.10` 就永远 miss。

**原因**：三角头 + 细尾巴这种箭头，按列统计墨量时**尾部那几条细长列反而更靠右**，而三角头的墨量随列递增（尖在左、最粗在右），重心自然被拉向右边。`centroid_dx` 描述的是"墨量重心的水平偏移"，不是"图形指向"。

**修法**：白板 demo 改成 `glyph_runs`（单字形）+ `aspect`（瘦长）+ `ink_ratio`（面积占比合理）三条，语义改成"白板上出现了一个箭头形状的深色标记"，并在 YAML 注释里写明为什么不判朝向。真要判方向得另写判据（例如左右两半的墨量差、端点粗细），不在 M5 范围内。

### 5.7 屏幕上会自己动的东西，会把固定底图和判据区域一起毁掉

**现象**：报表 demo 用固定底图做 diff，同一张底图有时读 0.000、有时读 0.10~0.43，怎么调都不稳。

**原因**：桌面右下角有个宠物浮层（Q 版小人）会到处飘，飘进监视区域就把底图污染成"另一个画面"；底图里如果正好录到它，之后它一走开，静止画面也一直"在变"。

**修法**：把区域裁到它飘不进去的位置（本机最终只取表格左侧"床号 + 姓名"两列），并且**录底图的那一刻**也要确认它不在区域里；`--save-baseline` 只抓一帧就走，所以脚本里加了个"等到空闲态再存"的判断。真实项目里这条经验更狠：任何带时钟、滚动跑马灯、浮动广告的界面，都得先把会动的部分裁出去。

### 5.8 GIF 角标不能画在画面上，会压住被判定的内容

**现象**：`HIT/idle` 角标画在左上角，报表 demo 里正好盖住 "11 王某" 那行，GIF 看着像界面坏了。

**修法**：`record_gif.py` 改成在图像上方拼一条 20px 的深色条，角标和区域名都画在条子上，画面本体一个像素都不动。

### 5.9 mock 页面用 `display: none` 会让下面的内容整体跳位

**现象**：新行用 `display:none` 隐藏，隐藏时下面的"共 N 条记录"往上跳 33px；底图是在隐藏态存的，一切换到显示态，**连状态栏都在动**，diff 里混进一堆无关变化，区域也永远对不齐。

**修法**：改用 `visibility: hidden` —— 行不显示但占着同样的高度，表格和状态栏位置不动，"多出来一行"这件事只影响它自己那一格。真实老系统里行数变化当然会让下面的内容往下推，那种场景下区域要框住"整块会动的表"，不能只框一行。

### 5.10 Pillow 存 GIF 会自动合并相同帧，但时长会累加

**现象**：录了 100 帧，存出来的 `demo.n_frames` 只有 5，怀疑丢帧。

**原因**：`PIL` 保存 GIF 时会把连续完全相同的帧合并成一张，并把时长加到前一张上（2400ms + 200 + 5400 + 200 + 1800 = 10s，一帧不少）。对"静止一段 → 变一下 → 再静止"的 demo 来说这是好事，体积从几百 KB 掉到十几 KB。

**自检**：用 `Image.open(gif).n_frames` 看不到真实帧数，得把每帧 `info["duration"]` 加起来对比录制时长。

### 5.11 Chrome 的 `--window-position` 对后开的窗口不生效；截图要分清"虚拟桌面"

**现象**：三个窗口用 `chrome --new-window --window-position=...` 打开，结果全叠在 (0,0)；用豆包自带的截图接口抓到的只有壁纸，没有窗口。

**原因 / 口径**：Chrome 只在部分启动方式下采纳 `--window-position`，多开时后续窗口继承同一位置；另外 agent 侧调起的窗口落在**系统默认桌面**，而某些截图接口抓的是"虚拟桌面"。

**修法**：窗口位置一律用 Win32 `SetWindowPos` 强制摆放（`SWP_NOZORDER | SWP_SHOWWINDOW`）；像素标定一律以 `.NET CopyFromScreen` / `PIL.ImageGrab` / `mss` 抓到的主桌面为准，`cu.screenshot()` 那种拿到虚拟桌面的接口不能用来标定。取证截图优先用系统的 `Win+Shift+S`。

### 5.12 别为了"拿到截图"跟抓屏工具死磕——该让人来截

**现象**：为了给 demo 拍素材，agent 侧反复调截图接口、反复 `Start-Process chrome` 摆窗口，抓到的永远是壁纸；循环了十几轮，素材一张没拿到，还把上下文烧完了。

**原因**：agent 调起的窗口落在**系统默认桌面**，而某些截图接口抓的是另一块"虚拟桌面"，两边根本不是同一个显示会话；这种情况下无论怎么调参数、怎么等，都截不到。这不是"多试几次就能成"的问题，是**信息不在可达范围内**。

**修法 / 口径**：像素素材这类"必须看见"的东西，**一次试探失败就交给人**：让人用系统自己的截图键（Windows `Win+Shift+S`）截好发进来，agent 只负责裁切、标定、写规则。人截一张 5 秒，agent 折腾 15 轮还不一定成——把"取证"和"处理"分开，才是这类任务正确的分工。

### 5.13 全景截图比单窗口截图更好用（标定口径要统一）

**现象**：先收到三张单窗口截图（947×446 / 946×447 / 945×448），每张尺寸都不一样，各自标出来的相对坐标互相对不上；换成两张**同屏全景截图**（1906×1063 / 1906×1070，空闲/命中各一张）之后，一次就标完了。

**原因**：单窗口截图是各自裁的，窗口在图里的位置、有无边框、缩放比例都不一样，`rect` 是相对整块屏幕算的，图里没有"屏幕"这个参照物就只能猜。

**修法**：标定一律用**同一时刻、同一缩放、含全部目标窗口的全景截图**，每份 demo 记一张 `shots/target.json`（源图尺寸、窗口在图里的位置、目标框在图里/裁剪后的坐标），从 `rect × 屏幕分辨率` 推像素框，再用实测对齐校验。本机 `legacy-report` 的 `baseline.png` 在新全景图上做平移搜索，最佳对齐误差 **0.00**，说明这套口径是可信的。

---

## 6. 打包发布（M6）

### 6.1 sdist 里 `tests/conftest.py` 会被悄悄漏掉

**现象**：`python -m build` 出来的 sdist 里有 `tests/test_*.py`，唯独没有 `tests/conftest.py`。解包跑 `pytest` 直接 `fixture 'blank' not found`。

**原因**：setuptools 默认的 MANIFEST 模板只匹配 `tests/test*.py`，`conftest.py` 不匹配。本地有 `.git` 时它还会改走"git 已跟踪文件"的路子，看起来一切正常；一旦在没有 git 的目录里构建（比如这个项目一开始就不是 git 仓库），就露馅。

**修法**：写 `MANIFEST.in`，用 `recursive-include tests *.py` 显式收。自检：解开 sdist 看一眼文件清单，别只看构建成没成功。

### 6.2 `license = { text = "MIT" }` 会触发 deprecation，还缺 `License-File`

**现象**：构建刷出一大段 `WARNING License classifiers are deprecated`，wheel 的 METADATA 里只有 `License: MIT`，`LICENSE` 也没进 `dist-info/licenses/`。

**原因**：PEP 639 之后 license 要用 SPDX 表达式 + `license-files`，老写法（table 形式 + `License :: OSI Approved ::` classifier）都被判过时。

**修法**：`license = "MIT"` + `license-files = ["LICENSE"]`，删掉那条 classifier，并把 `build-system.requires` 提到 `setuptools>=77`（低于这个版本不认新写法）。自检：`twine check dist/*` 通过 + METADATA 里有 `License-Expression: MIT`、`License-File: LICENSE`。

### 6.3 sdist 差点把 `shots/`（用户真实界面截图）和 `_mock/` 一起发出去

**现象**：只写了 `include` 规则时，`recursive-include demos *` 会把 `demos/*/shots/hit.png`——也就是调用方自己业务界面上的截图——打进发行包。

**原因**：`.gitignore` 只对 git 生效，**对 sdist 没有任何约束**；sdist 只认 `MANIFEST.in`（以及 setuptools 的默认模板）。项目里那些"本地素材、不进仓库"的目录，如果没有在 MANIFEST 里显式排除，照样会被打包。

**修法**：`prune demos/_mock` + `recursive-exclude demos/*/shots *`，并且把这两条放在所有 `include` **之后**（MANIFEST 后写的规则覆盖前面的）。自检：`tar -tzf dist/*.tar.gz | grep shots` 必须是空。

### 6.4 每次构建前必须清 `build/`、`dist/`、`*.egg-info`

**现象**：改完 `MANIFEST.in` 重建，sdist 里还是带着上一版的文件（包括刚被排除掉的 `shots/`）。

**原因**：`*.egg-info/SOURCES.txt` 是上一轮的清单，setuptools 会复用它；`build/` 里也可能有旧拷贝。

**修法**：构建脚本里固定 `rm -rf build dist *.egg-info` 再 `python -m build`。别信"增量构建"，打包这件事慢一点无所谓，错了才要命。

### 6.5 `pip install --no-deps` 装出来的轮子，`pixelbus --help` 会 import 失败

**现象**：为了省时间用 `--no-deps` 装轮子做冒烟，`pixelbus --help` 直接 `ModuleNotFoundError: numpy`。

**原因**：CLI 一进来就 import `rules`/`predicates` 链路，numpy 是硬依赖；`--no-deps` 只验证了"文件在不在"，没验证"装了能不能用"。

**修法**：冒烟测试必须**装全依赖**（本机 60 秒左右，CI 上有缓存更快）。冒烟要跑三件事：`--help`、`validate` 一份真规则、`import` 全部模块并确认 `py.typed` 在包里。

### 6.6 PowerShell 里 `| Select-Object -First N` 会把被管道化的构建进程提前杀掉

**现象**：`python -m build 2>&1 | Select-String 'Successfully' | Select-Object -First 5` 之后 `dist/` 是空的，`twine check dist/*` 报 `Cannot find file (or expand pattern): 'dist/*'`。单独跑 `python -m build` 又一切正常。

**原因**：`Select-Object -First` 拿到够数就**关闭整个上游管道**，被管道化的 `python` 进程收到断管直接被终止——构建中断在半路，所以你既没看到报错，也没拿到产物。

**修法**：构建/打包这类"必须跑完"的命令，要么不接管管道（直接让它把输出刷完），要么 `| Out-String` / `Tee-Object` 落文件后再过滤。判成功只看 `dist/` 里有没有产物 + 退出码，别只看日志里有没有 "Successfully"。

---

## 通用经验（跨里程碑）

- **像素判定的所有阈值都必须能被"看见"**。`probe` 存在的唯一意义就是让用户看到每条判据的实时分数，凭感觉调阈值 100% 会调到"偶尔命中"。所以 `probe` 里除了判据分数，还要打**原始指标**（墨量、簇数、最大缝、宽高比、质心偏移）和**主色**——用户照着抄进 YAML 就行。
- **合成图单测比真截图单测可靠得多**。真截图会随主题、字体、缩放、系统版本变；合成图能精确构造"两个字""一个箭头""一条 1px 光标"，才能把 0.2 那种坑钉死。
- **凡是"看起来没成本"的操作（`astype(float32)`、全图 `abs().max()`、每次新建一张全尺寸布尔图）都要在小图上量一遍**。中心区域 576×324 上，把 `astype(np.float32) + 三次乘加` 换成 uint16 定点运算，单帧省 0.78ms；把 `weak` 遮罩改成懒算，再省 0.3ms。加起来就是 1% 的 CPU。
- **不要把"能被优化掉的中间量"放在默认路径上**。`weak` 遮罩、浮点灰度、`int16` 全图，都是只有部分判据用得到的东西。

---

## 还没解决 / 已知限制

- `CAPTUREBLT` 自检在**持续播放视频**的桌面上永远"测不准"，只能一直用快路径，并依赖 5s~60s 的退避复检。如果某个分层浮层恰好只在视频播放期间出现，可能漏拍。
- 非 Windows 平台没有 `CAPTUREBLT` 问题，但 `mss` 在 macOS 上要"屏幕录制"权限，Linux 上 Wayland 基本抓不了。目前只在 Windows 上实测过。
- 引擎的"整帧未变就复用"快路径会让 `diff` 判据在静止画面上永远读到 0 —— 这符合语义（"画面没动"），但如果用户把 `diff` 当成"心跳"用，会觉得它不工作。
