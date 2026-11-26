"""合成图工厂。

所有判据用例都用 PIL 现画，不依赖真实截图 —— 这样 CI 上也能跑，
而且每个坑（多字文本、抗锯齿缝、1px 光标）都能精确复现。
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from pixelbus.predicates import Frame

W, H = 320, 120

_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


@pytest.fixture(scope="session")
def cjk_font_path() -> str:
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    pytest.skip("这台机器没有中文字体，字形相关的用例跳过")


@pytest.fixture(scope="session")
def font60(cjk_font_path):
    return ImageFont.truetype(cjk_font_path, 60)


def blank(size=(W, H), bg="white") -> Image.Image:
    return Image.new("RGB", size, bg)


def frame_of(im: Image.Image, region: str = "test", seq: int = 0) -> Frame:
    return Frame.from_pil(im, region=region, seq=seq)


def bgra_of(im: Image.Image) -> np.ndarray:
    """PIL 的 RGB → mss 的 BGRA，用来验证通道顺序没被搞反。"""
    rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
    alpha = np.full(rgb.shape[:2] + (1,), 255, dtype=np.uint8)
    bgr = rgb[..., ::-1]
    return np.concatenate([bgr, alpha], axis=2)


def fill_rect(im: Image.Image, box, color="black") -> Image.Image:
    ImageDraw.Draw(im).rectangle(box, fill=color)
    return im


def draw_arrow(im: Image.Image, direction: str = "left", ink="black", origin=(40, 25),
               head=(60, 70), tail=90, shaft=6) -> Image.Image:
    """画一个"箭头形"：实心三角头 + 细尾巴。

    三角头在左边 → 墨量压在左侧 → 质心偏左（对应 ←）。
    """
    d = ImageDraw.Draw(im)
    x, y = origin
    hw, hh = head
    cy = y + hh // 2
    if direction == "left":
        d.polygon([(x, cy), (x + hw, y), (x + hw, y + hh)], fill=ink)
        d.rectangle([x + hw, cy - shaft // 2, x + hw + tail, cy + shaft // 2], fill=ink)
    elif direction == "right":
        x2 = x + hw + tail
        d.polygon([(x2, cy), (x2 - hw, y), (x2 - hw, y + hh)], fill=ink)
        d.rectangle([x, cy - shaft // 2, x + tail, cy + shaft // 2], fill=ink)
    else:
        raise ValueError(direction)
    return im


def draw_text(im: Image.Image, text: str, font, at=(10, 10), fill="black") -> Image.Image:
    ImageDraw.Draw(im).text(at, text, font=font, fill=fill)
    return im


def draw_plus(im: Image.Image, center=(W // 2, H // 2), arm=25, thick=9, ink="black") -> Image.Image:
    cx, cy = center
    d = ImageDraw.Draw(im)
    d.rectangle([cx - arm, cy - thick // 2, cx + arm, cy + thick // 2], fill=ink)
    d.rectangle([cx - thick // 2, cy - arm, cx + thick // 2, cy + arm], fill=ink)
    return im


def draw_caret(im: Image.Image, x: int, height: int = H - 10, ink="black") -> Image.Image:
    """1px 竖线：真实界面里的文本光标，会把列簇数凭空 +1。"""
    fill_rect(im, [x, 5, x + 1, 5 + height], ink)
    return im
