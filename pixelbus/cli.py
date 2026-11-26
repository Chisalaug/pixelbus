"""命令行：run / probe / doctor / validate / mcp。

设计取向：终端里能一眼看懂"现在到底是什么状态、差多少"。
所以 probe 会把每条判据的实测值、阈值余量、进度条都打出来 —— 调阈值不该靠猜。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Iterable, Mapping

import numpy as np

from . import __version__

# --------------------------------------------------------------------------
# 终端小工具
# --------------------------------------------------------------------------

_COLOR = {
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "white": "\x1b[37m",
    "bold": "\x1b[1m",
    "off": "\x1b[0m",
}

_USE_COLOR = True


def _enable_ansi() -> bool:
    """Windows 10+ 的 conhost 默认不解析颜色转义，要显式打开 VT 模式。"""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
                return True  # 不是真控制台（重定向到文件），直接当支持
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


def c(text: str, color: str) -> str:
    if not _USE_COLOR or color not in _COLOR:
        return text
    return f"{_COLOR[color]}{text}{_COLOR['off']}"


def bar(level: float, width: int = 10) -> str:
    n = max(0, min(width, int(round(level * width))))
    return "█" * n + "░" * (width - n)


def human_ms(sec: float) -> str:
    return f"{sec * 1000:.1f}ms"


# --------------------------------------------------------------------------
# 输出格式化
# --------------------------------------------------------------------------


def _print_rules_error(err: Exception) -> None:
    print(c(str(err), "red"), file=sys.stderr)


def _status_suffix(snap: Any) -> str:
    hits = [n for n, s in snap.signals.items() if s["hit"]]
    act = [n for n, s in snap.states.items() if s["active"]]
    h = c(",".join(hits), "yellow") if hits else c("—", "dim")
    a = c(",".join(act), "red") if act else c("—", "dim")
    return f"signal:{h}  state:{a}  frames={snap.frames} dropped={snap.dropped}"


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    from .engine import Engine
    from .lock import PortLock, probe_owner
    from .rules import RulesError, load_rules

    try:
        rules = load_rules(args.rules)
    except RulesError as exc:
        _print_rules_error(exc)
        return 1
    except FileNotFoundError:
        print(c(f"找不到规则文件：{args.rules}", "red"), file=sys.stderr)
        return 1

    for warning in rules.warnings:
        print(c(warning.render(os.path.basename(rules.path)), "yellow"), file=sys.stderr)

    fps = args.fps or rules.fps
    lock_port = args.lock_port or rules.server.lock_port
    host = args.host or rules.server.host
    port = args.port or rules.server.port
    lock = PortLock(lock_port, host)
    if not lock.acquire():
        print(c(f"已经有 pixelbus 在跑了（{host}:{lock_port} 被占用）。", "red"))
        owner = probe_owner(host, port)
        if owner:
            print(f"  正在运行的那份：规则 {owner.get('rules') or '?'}，已跑 {owner.get('uptime', '?')} 秒")
            print(f"  状态：http://{rules.server.host}:{rules.server.port}/state")
        else:
            print(f"  端口被别的程序占着，或者上一份还没退干净。换端口：pixelbus run {args.rules} --lock-port {lock_port + 1}")
        print("  这个窗口退出，不会重复抓屏、重复执行动作。")
        return 1

    counters = {"events": 0}
    started = time.time()

    def on_event(event: Any) -> None:
        counters["events"] += 1
        if args.quiet:
            return
        color = {"appear": "red", "disappear": "green", "change": "cyan", "value": "dim"}.get(event.kind, "white")
        extra = ""
        detail = event.detail or {}
        if detail.get("failed"):
            extra = c(f"  failed={','.join(detail['failed'])}", "dim")
        if detail.get("text"):
            extra = c(f"  text={detail['text']}", "dim")
        print(f"\r{c(event.clock(), 'dim')} {c(event.kind.ljust(9), color)} "
              f"{event.target}:{c(event.name, 'bold')}  lvl={event.level:.2f}{extra}\x1b[K")

    engine = Engine(rules, on_event=on_event)
    print(c(f"pixelbus {__version__} · {os.path.basename(rules.path)}", "bold")
          + f"  fps={fps:g}  区域 {len(rules.regions)} 个  signal {len(rules.signals)} 条  "
          + f"state {len(rules.states)} 个  dpi={engine.dpi or '?'}")

    srv = None
    if args.no_server:
        print(c("  Ctrl+C 退出。没起 HTTP（--no-server），事件只打到终端。", "dim"))
    else:
        from .server import ServerThread, create_app

        app = create_app(engine)
        srv = ServerThread(app, host=host, port=port)
        try:
            srv.start()
        except Exception as exc:
            print(c(f"  HTTP 起不来：{exc}", "red"))
            print(c("  换个端口：--port 8790；或者干脆 --no-server 只打终端。", "dim"))
            lock.release()
            return 1
        print(c(f"  Ctrl+C 退出。state: {srv.url}/state   events(SSE): {srv.url}/events", "dim"))
        print(c(f"  热重载：curl -X POST {srv.url}/reload", "dim"))

    engine.start()
    last_status = 0.0
    try:
        while True:
            time.sleep(0.2)
            now = time.time()
            if args.duration and now - started >= args.duration:
                break
            if args.quiet or args.no_status or not sys.stdout.isatty():
                continue
            if now - last_status >= 1.0:
                last_status = now
                sys.stdout.write("\r" + c(_status_suffix(engine.snapshot(now)), "dim") + "\x1b[K")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()
        if srv is not None:
            srv.stop()
        lock.release()

    snap = engine.snapshot()
    elapsed = max(0.001, time.time() - started)
    print()
    print(c(f"停止：{snap.frames} 帧 / {elapsed:.1f}s ≈ {snap.frames / elapsed:.1f}fps，"
             f"事件 {counters['events']} 条，丢帧 {snap.dropped}，抓屏失败 {engine.grab_errors}", "dim"))
    return 0


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------


def _raw_metrics(frame: Any) -> dict[str, Any]:
    """不依赖任何 signal 的"体检数据"，用来现场找合适的判据。"""
    from .predicates import bbox_of, centroid_offset, count_runs

    ink = frame.ink()
    box = bbox_of(ink.strong, min_area=40)
    info = count_runs(ink, min_area=40)
    out: dict[str, Any] = {
        "ink": float(ink.strong.mean()),
        "threshold": ink.threshold,
        "ink_px": ink.count,
        "runs": info.runs,
        "runs_raw": info.runs_raw,
        "gap_max": info.gap_max,
        "size": frame.shape,
    }
    if box is not None:
        out.update(
            {
                "box": box.as_list(),
                "aspect": round(box.aspect, 3),
                "dx": round(centroid_offset(ink.strong, box), 4),
                "h_ratio": round(box.h / max(1, frame.shape[0]), 3),
            }
        )
    return out


def _dominant_colors(frame: Any, top: int = 3) -> list[tuple[str, float]]:
    rgb = frame.rgb()
    q = (rgb // 24) * 24
    flat = q.reshape(-1, 3)
    # 采样，别在 15fps 的循环里做 5 万行的 unique
    if flat.shape[0] > 20000:
        flat = flat[:: max(1, flat.shape[0] // 20000)]
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1][:top]
    total = float(counts.sum())
    return [(f"#{r:02x}{g:02x}{b:02x}", round(float(counts[i]) / total, 3)) for i in order for r, g, b in [colors[i]]]


def _parse_rect(text: str) -> tuple[float, float, float, float]:
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) != 4:
        raise ValueError('--rect 要 4 个数，例如 "0.72,0.08,0.26,0.10"')
    vals = tuple(float(p) for p in parts)
    if any(v < 0 or v > 1 for v in vals):
        raise ValueError("--rect 是相对坐标，四个值都要在 0~1 之间")
    return vals  # type: ignore[return-value]


def _write_rect(path: str, name: str, rect: tuple[float, float, float, float]) -> str:
    """把框选结果写回 YAML。只动 ``<name>`` 下面的 rect，其余一个字节都不碰。

    行内写法（``alarm: {monitor: primary, rect: [...]}``）和块写法都支持，
    改之前先存一份 ``.bak``。
    """
    with open(path, "r", encoding="utf-8") as fp:
        original = fp.read()
    lines = original.splitlines(keepends=True)

    new_rect = "[" + ", ".join(f"{v:.3f}" for v in rect) + "]"
    header = re.compile(rf"^(\s*){re.escape(name)}\s*:\s*(.*)$")
    wrote = 0
    replaced = ""

    for i, line in enumerate(lines):
        m = header.match(line)
        if not m:
            continue
        wrote = 1
        if "rect:" in line:  # 行内写法
            lines[i] = re.sub(r"rect:\s*\[[^\]]*\]", f"rect: {new_rect}", line)
            replaced = lines[i].strip()
            break
        # 块写法：往下找最近的 rect
        for j in range(i + 1, min(i + 8, len(lines))):
            if "rect:" in lines[j]:
                lines[j] = re.sub(r"(\S\s*rect:\s*)\[[^\]]*\]", rf"\g<1>{new_rect}", lines[j])
                replaced = lines[j].strip()
                break
        break

    if not wrote:
        raise RuntimeError(f"YAML 里找不到 regions.{name}，先把它写好再 --apply")

    with open(path + ".bak", "w", encoding="utf-8") as fp:
        fp.write(original)  # 备份的是原文，不是改完的
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("".join(lines))
    return replaced or new_rect


def _cmd_probe(args: argparse.Namespace) -> int:
    from .capture import Grabber, RegionSpec, rect_to_bbox
    from .predicates import Frame
    from .rules import RulesError, load_rules

    try:
        rules = load_rules(args.rules)
    except RulesError as exc:
        _print_rules_error(exc)
        return 1

    base_dir = os.path.dirname(os.path.abspath(rules.path)) or "."
    grabber = Grabber()

    # 1) 决定要看哪个区域
    name = args.region
    rect_override = None
    monitor_override = args.monitor

    if args.pick:
        from .picker import pick_region

        name = name or args.pick
        if not name:
            if len(rules.regions) == 1:
                name = next(iter(rules.regions))
            else:
                print(c("框选要给个区域名：--pick alarm", "red"))
                return 1
        existing = rules.regions.get(name)
        mon_spec = monitor_override or (existing.spec.monitor if existing else "primary")
        print(c(f"准备框选 {name}（显示器 {mon_spec}）：拖一个矩形，Esc 取消", "cyan"))
        try:
            picked = pick_region(mon_spec, grabber=grabber)
        except RuntimeError as exc:
            print(c(f"框选窗口起不来：{exc}", "red"))
            print('  手动传坐标：--rect "0.72,0.08,0.26,0.10"')
            return 1
        if picked is None:
            print("取消了，什么都没改。")
            return 1
        rect_override = picked.rect
        monitor_override = picked.monitor
        snippet = picked.yaml_snippet(name, mon_spec)
        print(c("框到了：", "green") + snippet.strip())
        print(c(f"  绝对像素 {picked.bbox}", "dim"))
        if args.apply:
            try:
                replaced = _write_rect(rules.path, name, picked.rect)
            except Exception as exc:
                print(c(f"写回失败：{exc}", "red"))
                return 1
            print(c(f"  已写回 {os.path.basename(rules.path)}：{replaced}", "green")
                  + c("（原件备份为 .bak）", "dim"))
            try:
                rules = load_rules(args.rules)
            except RulesError as exc:  # 写坏了要能立刻发现
                _print_rules_error(exc)
                return 1
        elif existing is None:
            print(c("  这个区域不在 YAML 里（--apply 才能写回去）", "yellow"))

    if args.rect:
        try:
            rect_override = _parse_rect(args.rect)
        except ValueError as exc:
            print(c(str(exc), "red"))
            return 1

    if name is None:
        if len(rules.regions) == 1:
            name = next(iter(rules.regions))
        else:
            print(c("有多个区域，用 --region 指定一个：", "red") + ", ".join(rules.regions))
            return 1

    existing = rules.regions.get(name)
    if existing is None and rect_override is None:
        print(c(f"region {name!r} 不在 YAML 里。用 --pick {name} 框一个，或 --rect 直接给坐标。", "red"))
        return 1

    if rect_override is not None:
        spec = RegionSpec(name=name, monitor=monitor_override or (existing.spec.monitor if existing else "primary"), rect=rect_override)
    else:
        assert existing is not None
        spec = RegionSpec(
            name=name,
            monitor=monitor_override or existing.spec.monitor,
            rect=existing.spec.rect,
            pixels=existing.spec.pixels,
        )
    bbox = rect_to_bbox(spec, grabber.monitor(spec.monitor))

    if args.save_baseline:
        # 把当前画面存成 diff 参考图：先摆成"没变化"的状态再跑，底图才干净
        try:
            arr = grabber.grab_bbox(bbox)
        except Exception as exc:
            print(c(f"抓屏失败：{exc}", "red"))
            return 1
        from PIL import Image

        path = args.save_baseline
        # mss 给的是 BGRA，Pillow 要 RGB
        rgb = np.ascontiguousarray(np.asarray(arr)[..., [2, 1, 0]])
        Image.fromarray(rgb).save(path)
        print(c(f"底图已存：{path}", "green") + c(f"  区域 {bbox}", "dim"))
        return 0

    tests: dict[str, Any] = {
        sname: cfg.build(base_dir=base_dir) for sname, cfg in rules.signals.items() if cfg.region == name
    }

    fps = args.fps or min(rules.fps, 15.0)
    interval = 1.0 / fps
    started = time.time()
    frames = 0
    raw = {}
    colors: list[tuple[str, float]] = []
    try:
        while True:
            t0 = time.perf_counter()
            try:
                arr = grabber.grab_bbox(bbox)
            except Exception as exc:
                print(c(f"抓屏失败：{exc}", "red"))
                time.sleep(0.5)
                continue
            frame = Frame(arr, order="bgra", region=name, seq=frames, t=time.time())
            results = {sname: test.measure(frame) for sname, test in tests.items()}
            if frames % max(1, int(fps)) == 0:
                raw = _raw_metrics(frame)
                colors = _dominant_colors(frame)

            if not args.no_screen:
                sys.stdout.write("\x1b[H\x1b[2J")
            print(c(f"pixelbus probe · {os.path.basename(rules.path)} · region={name}", "bold")
                  + f"  monitor={spec.monitor}  bbox={bbox}  target={fps:g}fps")
            print(c(f"  已跑 {frames} 帧  {time.strftime('%H:%M:%S')}   Ctrl+C 退出", "dim"))
            print()
            if tests:
                for sname, result in results.items():
                    head = c("● HIT ", "red") if result.hit else c("○ miss", "dim")
                    print(f"  {head} {c(sname, 'bold')}  level={result.level:.2f} {bar(result.level)}")
                    for judge in result.judges:
                        mark = c("✓", "green") if judge.passed else c("✗", "red")
                        print(f"      {mark} {judge.predicate:<12} {judge.value:>9.3f}   {c(judge.head, 'dim')}")
                        detail = {k: v for k, v in judge.detail.items() if k not in ("total",)}
                        print(c(f"        {detail}", "dim"))
                    print()
            else:
                print(c("  这个区域还没有 signal，先看下面的原始指标挑判据", "yellow") + "\n")
            if raw:
                print(c("  原始指标（不依赖 YAML，照着挑判据）", "cyan"))
                print(f"    ink={raw.get('ink', 0):.3f} (阈值 {raw.get('threshold')})  "
                      f"runs={raw.get('runs')} (raw {raw.get('runs_raw')}, 最大缝 {raw.get('gap_max')}px)  "
                      f"aspect={raw.get('aspect', 0)}  dx={raw.get('dx', 0):+.3f}  h_ratio={raw.get('h_ratio', 0)}")
                print(f"    box={raw.get('box')}  ink_px={raw.get('ink_px')}  区域={raw.get('size')}")
                if colors:
                    print("    主色 " + "  ".join(f"{c(col, 'blue')} {pct:.0%}" for col, pct in colors)
                          + c("   ← 直接抄进 color_ratio 的 color", "dim"))
            print()
            print(c('  提示：阈值要卡在"命中时稳定高、空闲时稳定低"的中间，别贴着边界。', "dim"))

            frames += 1
            elapsed = time.perf_counter() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)
            if args.seconds and time.time() - started >= args.seconds:
                break
    except KeyboardInterrupt:
        print("\n退出 probe。")
    finally:
        grabber.close()
    return 0


# --------------------------------------------------------------------------
# doctor / validate
# --------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .capture import Grabber
    from .predicates import available_predicates

    print(c(f"pixelbus {__version__}", "bold"))
    print(f"  python {sys.version.split()[0]}  ({sys.executable})")
    try:
        from .capture import enable_dpi_awareness

        print(f"  dpi 感知：{enable_dpi_awareness()}")
    except Exception as exc:
        print(c(f"  dpi 探测失败：{exc}", "red"))

    try:
        with Grabber() as g:
            print("\n  显示器：")
            for mon in g.describe():
                star = c(" ★主屏", "yellow") if mon["primary"] else ""
                print(f"    [{mon['index']}] {mon['width']}x{mon['height']} @ ({mon['left']}, {mon['top']}){star}")
            print(f"\n  抓一帧耗时：", end="")
            t0 = time.perf_counter()
            for _ in range(3):
                g.grab_monitor("primary")
            print(f"{human_ms((time.perf_counter() - t0) / 3)}（全屏 3 次取平均）")
            print(f"  抓屏路径：{g.layer_note}")
            print(c(f"    快路径 {g.fast_grabs} 次 / 兼容 {g.compat_grabs} 次", "dim"))
    except Exception as exc:
        print(c(f"  抓屏不可用：{exc}", "red"))

    print("\n  可选能力：")
    for label, mod in (
        ("press (pynput)", "pynput"),
        ("ocr (rapidocr)", "rapidocr_onnxruntime"),
        ("mqtt (paho-mqtt)", "paho.mqtt.client"),
        ("mcp", "mcp"),
    ):
        try:
            __import__(mod)
            print(f"    {c('✓', 'green')} {label}")
        except Exception:
            print(f"    {c('✗', 'dim')} {label}  {c('未安装', 'dim')}")
    try:
        from . import picker

        cmd = picker._first_usable()
        if cmd is None:
            print(f"    {c('✗', 'red')} tkinter（探到的解释器都开不了 Tk 窗口，probe --pick 用不了）")
            print(c("      设置 PIXELBUS_TK_PYTHON=<能用的 python.exe> 可以指定一个", "dim"))
        else:
            shown = " ".join(cmd)
            by = "本体解释器" if cmd == [sys.executable] else "外部解释器"
            print(f"    {c('✓', 'green')} tkinter（框选用 {by}：{shown}）")
    except Exception as exc:
        print(f"    {c('✗', 'red')} tkinter（{exc}）")

    print("\n  可用判据：" + ", ".join(available_predicates()))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from .rules import RulesError, load_rules

    try:
        rules = load_rules(args.rules)
    except RulesError as exc:
        _print_rules_error(exc)
        return 1
    except FileNotFoundError:
        print(c(f"找不到规则文件：{args.rules}", "red"))
        return 1

    print(c("规则没问题。", "green"))
    print(f"  fps={rules.fps:g}  区域 {len(rules.regions)}   signal {len(rules.signals)}   "
          f"state {len(rules.states)}   action {len(rules.actions)}")
    for name, region in rules.regions.items():
        kind = "rect" if region.spec.rect else "pixels"
        print(f"    {name}: {kind} {region.spec.rect or region.spec.pixels} @ {region.spec.monitor}")
    for name, state in rules.states.items():
        print(f"    state {name}: when {state.when}  stable={state.stable_frames}")
    for warning in rules.warnings:
        print(c(warning.render(os.path.basename(rules.path)), "yellow"))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import run_mcp

    return run_mcp(args.rules, transport=args.transport)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pixelbus", description="把没有 API 的界面变成事件流")
    ap.add_argument("-V", "--version", action="version", version=f"pixelbus {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="按规则跑起来：终端事件流 + HTTP/SSE")
    run.add_argument("rules", help="rules.yaml 路径")
    run.add_argument("--fps", type=float, default=None, help="覆盖 YAML 里的 fps")
    run.add_argument("--lock-port", type=int, default=None, help="单实例锁端口（默认 rules 里的 lock_port）")
    run.add_argument("--host", default=None, help="HTTP 监听地址（默认 rules 里的 host）")
    run.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 rules 里的 port）")
    run.add_argument("--no-server", action="store_true", help="不起 HTTP，只打终端事件流")
    run.add_argument("--duration", type=float, default=0, help="跑多少秒后自动退出（0=一直跑）")
    run.add_argument("--quiet", action="store_true", help="只跑不打事件")
    run.add_argument("--no-status", action="store_true", help="不显示底部状态行")
    run.set_defaults(func=_cmd_run)

    probe = sub.add_parser("probe", help="实时打分，用来调阈值")
    probe.add_argument("rules", help="rules.yaml 路径")
    probe.add_argument("--region", default=None, help="看哪个区域（只有一个区域时可省）")
    probe.add_argument("--pick", nargs="?", const="", default=None, metavar="NAME",
                       help="打开全屏遮罩框选；给名字就顺便写回 YAML（配 --apply）")
    probe.add_argument("--apply", action="store_true", help="把框选结果写回 YAML（会先备份 .bak）")
    probe.add_argument("--rect", default=None, help='不用框选，直接给相对坐标："0.72,0.08,0.26,0.10"')
    probe.add_argument("--monitor", default=None, help="显示器：primary / 1 / 2")
    probe.add_argument("--fps", type=float, default=None)
    probe.add_argument("--seconds", type=float, default=0, help="跑多少秒后退出（0=一直）")
    probe.add_argument("--no-screen", action="store_true", help="不刷屏（输出重定向时用）")
    probe.add_argument("--save-baseline", default=None, metavar="PATH",
                       help="把当前区域画面存成 PNG 当 diff 的参考底图（存完就退出）")
    probe.set_defaults(func=_cmd_probe)

    doc = sub.add_parser("doctor", help="看看环境：显示器、DPI、可选依赖")
    doc.set_defaults(func=_cmd_doctor)

    val = sub.add_parser("validate", help="只校验 YAML")
    val.add_argument("rules")
    val.set_defaults(func=_cmd_validate)

    mcp = sub.add_parser("mcp", help="MCP server：把状态和按键暴露给 Agent")
    mcp.add_argument("rules", help="rules.yaml 路径")
    mcp.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"],
                    help="MCP 传输方式（默认 stdio）")
    mcp.set_defaults(func=_cmd_mcp)
    return ap


def main(argv: list[str] | None = None) -> int:
    global _USE_COLOR
    _USE_COLOR = _enable_ansi()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
