# CHANGELOG

版本号按"里程碑即版本"走：一个里程碑做完打一个 tag。语义化版本，0.x 阶段破坏性变更会写在
"不兼容变更"里。

## 0.1.0 · 2026-11-26

第一个能用的版本：**框几个小区域 → 出事件 → 可以被订阅 / 被动作消费 / 被 Agent 调用**。
五个里程碑全在里面（M0~M5），M6 是打包发布本身（CI、MANIFEST、本文件）。

含 201 个全合成图单测（其中 4 条专盯打包元数据）+ 三份真机标定的 demo 规则。Windows 上实测 1080p / 3 区域 / 15fps
占单核 3.3%~5.4%，内存峰值约 50MB。

### 引擎与抓屏（M1）

- `pixelbus run rules.yaml`：终端事件流 + 状态行，`--duration` / `--no-status` / `--fps` 可调。
- `mss` 抓屏，只抓区域、不抓全屏；`CAPTUREBLT` 兼容路径与快路径自动切换（持续播放视频的桌面上会自动退回兼容路径）。
- 单实例锁（占本地端口）：重复启动直接 `exit 1`，并打印"当前所有者是哪个规则、已经跑了多久"。
- 落后丢帧不追帧（宁可少画几帧，也不让事件延迟堆积），整帧未变时直接复用上一帧的结果。
- `pixelbus doctor`：显示器 / DPI / 抓屏耗时 / 快慢路径 / 可选依赖 / 框选用哪个解释器。

### 判据（M0）

- `color_ratio`、`glyph_runs`、`aspect`、`centroid_dx`、`ink_ratio`、`diff`、`ocr_keyword`（`ocr` 可选）。
- 每条判据都有 `ink`（dark/bright/auto）、`threshold`（显式 / Otsu 自动）等显式参数，默认值可覆盖。
- `pixelbus probe`：全屏半透明遮罩拖拽框选（tkinter），实时打印每条判据的分数 **和原始指标**（墨量、簇数、最大缝、宽高比、质心偏移、主色），阈值照着抄。
- `--pick <region> --apply` 把框选结果写回 YAML（先备份 `.bak`）；`--save-baseline` 存 `diff` 的固定底图。
- 框选会自动挑一个**真能开 Tk 窗口**的解释器（`PIXELBUS_TK_PYTHON` → `sys.executable` → `py -3.14/-3.13/-3.12/-3` → `python3/python`）。

### HTTP / SSE（M2）

- `GET /health`（`app=="pixelbus"`）、`GET /state`（完整快照：区域、信号、状态、最近事件）、`GET /events`（SSE）。
- SSE 事件带自增 `id`，断线重连用 `Last-Event-ID` 补发漏掉的；慢客户端丢最旧的，不拖垮引擎。
- `POST /reload` 热重载规则；YAML 有错返回 400 + 带行号的 issues，旧规则继续跑。
- 端口被占 / 单实例锁冲突都直接 `exit 1`，不静默假装起来了。

### 动作（M3）

- 六种动作：`log` / `beep` / `press` / `webhook` / `exec` / `mqtt`，都走独立工作线程 + 队列，引擎不被动作卡住。
- 每个动作可配 `cooldown`（同一动作两次触发的最小间隔）和 `repeat`（连续触发上限）。
- `press` 走 `pynput`（`pixelbus[keys]`），`mqtt` 走 `paho-mqtt`（`pixelbus[mqtt]`），`ocr` 走 `rapidocr`（`pixelbus[ocr]`）——都是可选 extra，缺了会告诉你该装哪个。
- `do.log` 的字符串是**日志正文**，不是动作类型；`do.text` / `do.payload` 是给动作用的载荷。

### MCP server（M4）

- `pixelbus mcp rules.yaml [--transport stdio|sse|streamable-http]`，四个工具：
  - `get_state` 完整快照
  - `list_targets` 只列名字 + 当前命中态（给模型省 token）
  - `wait_for(name, kind, timeout)` 等 appear / disappear / change / value / any（先查当前状态再订阅，已经命中的不会白等一个周期）
  - `press(keys)` 复用 `press` 动作的按键解析
- 兼容 MCP SDK 2.x（`MCPServer`）与 1.x（`FastMCP`）；业务壳不 import SDK，没装 `pixelbus[mcp]` 也能正常跑引擎。

### Demo（M5）

- 三份真机标定的规则：`demos/alarm-popup`（HIS 危急值红色弹窗，颜色占比）、`demos/canvas-board`（白板箭头，单字形 + 宽高比 + 墨迹占比）、`demos/legacy-report`（老报表多一行，固定底图差分）。
- 三段 GIF：由真机截图的 hit/idle 两帧合成（不是实时录屏），`python tools/make_demo_gif.py` 可重出；要录连续过程用 `tools/record_gif.py`。
- `tools/check_demo.py` 在真截图上验每条规则：命中要过、空闲要不过、余量 ≥ 0.2。

### 打包发布（M6）

- `python -m build` 产出 sdist + wheel，`twine check` 通过；wheel 内 `py.typed` 齐全。
- `MANIFEST.in` 明确控制 sdist：带 `tests/`（含 `conftest.py`）、`tools/`、`demos/`（规则/GIF/底图），**不带** `demos/_mock/` 与任何 `shots/`（那里面有调用方自己界面的截图）。
- GitHub Actions：三平台 × py3.11/3.13 跑测试，外加一个"构建 + twine check + 在干净 venv 里装轮子冒烟"的 job；打 `v*` tag 自动把产物挂到 Release。
- 许可证从 `license = { text = "MIT" }` 换成 PEP 639 的 SPDX 写法（`license = "MIT"` + `license-files`），消掉 setuptools 的 deprecation 告警。

### 不兼容变更

无（首版）。

### 已知限制

- 只在 Windows 上实测过。macOS 要"屏幕录制"权限，Linux Wayland 基本抓不了。
- `ocr_keyword` 需要额外装 `rapidocr-onnxruntime`，包体积大、CPU 也贵，默认不用。
- 判据阈值对主题 / 字体 / 浏览器缩放 / 窗口位置敏感，换机器必须用 `probe` 重标，抄数字会翻车。
- 区域里不能留会自己动的东西（时钟、滚动字幕、桌面宠物浮层），会把固定底图和判据一起毁掉 —— 见 `PITFALLS.md` 5.7。
