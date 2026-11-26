"""独立的框选窗口（只依赖标准库 tkinter）。

为什么单独一个文件：**能不能开 Tk 窗口，取决于"哪个解释器在跑"**，而不是"有没有装
tkinter 模块"。有些被打包/裁剪过的 Python（比如各种 AI 运行时自带的 relocatable
Python）里 `import tkinter` 是成功的，但 `tk.Tk()` 会抛
``Can't find a usable init.tcl`` —— 因为 Tcl 的脚本数据文件被裁掉了。这种解释器上
`picker.pick_region` 会找另一个能开窗的解释器来跑本文件，所以本文件必须：

* 只用标准库（不能被 ``import pixelbus`` 连累）；
* 兼容老版本 Python（可能落到 3.8 的解释器上，别用 3.10+ 语法）；
* 只往 stdout 打**一行 JSON**，方便父进程解析。

调用方式::

    python _pick_gui.py '{"width":1920,"height":1080,"left":0,"top":0,...}'

输出::

    {"rect": [0.12, 0.34, 0.5, 0.2], "px": [960, 216]}     # 框到了（相对坐标 + 逻辑像素尺寸）
    {"rect": null}                                          # 按 Esc 取消
"""

import json
import sys

DEFAULT_TITLE = "拖一个矩形，Esc 取消"


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: _pick_gui.py <json-config>\n")
        return 2
    cfg = json.loads(argv[1])

    import tkinter as tk

    width = int(cfg.get("width") or 1920)
    height = int(cfg.get("height") or 1080)
    left = int(cfg.get("left") or 0)
    top = int(cfg.get("top") or 0)
    alpha = float(cfg.get("alpha") or 0.28)
    title = cfg.get("title") or DEFAULT_TITLE

    root = tk.Tk()
    state = {"rect": None}

    try:
        root.overrideredirect(True)
        root.geometry("%dx%d%+d%+d" % (width, height, left, top))
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        root.configure(bg="#000000")
        root.lift()

        # 这个解释器可能不是 DPI 感知的：Tk 的"屏幕宽度"是逻辑像素，
        # 而抓屏用的是物理像素。出现偏差就按比例换算。
        tk_w = root.winfo_screenwidth() or width
        scale = float(width) / float(tk_w) if tk_w else 1.0

        canvas = tk.Canvas(root, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        canvas.create_text(width // 2, 28, text=title, fill="#ffffff",
                           font=("Microsoft YaHei", 13, "normal"))

        sel = {"x0": 0, "y0": 0, "rect_id": None, "label_id": None, "dragging": False}

        def on_press(event):
            sel["dragging"] = True
            sel["x0"], sel["y0"] = event.x, event.y
            for key in ("rect_id", "label_id"):
                if sel[key] is not None:
                    canvas.delete(sel[key])
                sel[key] = None
            sel["rect_id"] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                     outline="#ff3860", width=2, fill="")

        def on_drag(event):
            if not sel["dragging"]:
                return
            canvas.coords(sel["rect_id"], sel["x0"], sel["y0"], event.x, event.y)
            w, h = abs(event.x - sel["x0"]), abs(event.y - sel["y0"])
            if sel["label_id"] is not None:
                canvas.delete(sel["label_id"])
            sel["label_id"] = canvas.create_text(
                min(event.x, sel["x0"]) + 8, max(event.y, sel["y0"]) + 14,
                text="%d x %d px" % (w, h), fill="#ffd166", anchor="nw",
                font=("Consolas", 11, "bold"))

        def on_release(event):
            if not sel["dragging"]:
                return
            sel["dragging"] = False
            x0, y0 = sel["x0"], sel["y0"]
            x1, y1 = event.x, event.y
            if abs(x1 - x0) < 4 or abs(y1 - y0) < 4:
                return  # 手滑点一下，不算
            lx, ty = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            l = round(max(0.0, min(1.0, (lx * scale) / width)), 4)
            t = round(max(0.0, min(1.0, (ty * scale) / height)), 4)
            rw = round(min((w * scale) / width, 1.0 - l), 4)
            rh = round(min((h * scale) / height, 1.0 - t), 4)
            state["rect"] = [l, t, rw, rh]
            state["px"] = [w, h]
            root.destroy()

        def on_escape(_event):
            state["rect"] = None
            root.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        root.bind("<Escape>", on_escape)
        root.focus_force()
        root.mainloop()
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass

    if state["rect"] is None:
        sys.stdout.write(json.dumps({"rect": None}) + "\n")
    else:
        sys.stdout.write(json.dumps({"rect": state["rect"], "px": state.get("px")}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
