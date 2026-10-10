"""
长文本渲染成图片（精髓移植自 LangBot longtext/image 策略，样式加强）
====================================================================
长回复降级链的中间档：合并转发失败 → 渲染文字图 → 渲染失败 → 逐条气泡。

样式：暖纸底 + 主题色标题条 + 深灰正文 + 舒适行距 + 按字宽实测折行
（数字/单词不拆断到两行）。资源硬上限（借鉴 LangBot）：超限抛
TextImageCapacityError，由调用方降级，绝不撑爆内存。
"""
import asyncio
import logging
import os
import time as _time
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("qqbot")

from .config import config

# 资源硬上限（超限抛 TextImageCapacityError → 调用方降级气泡）
MAX_CHARS = 20000
MAX_LINES = 300
MAX_PIXELS = 6_000_000

CANVAS_WIDTH = 820
PADDING_X = 48
PADDING_Y = 36
FONT_SIZE = 26
TITLE_FONT_SIZE = 24
LINE_HEIGHT = 40

BG_COLOR = (250, 247, 242)       # 暖纸白
TEXT_COLOR = (43, 43, 43)        # 深灰
TITLE_BAR_COLOR = (86, 122, 176)  # 蓝灰标题条
TITLE_TEXT_COLOR = (255, 255, 255)
META_COLOR = (150, 150, 150)

# 字体查找顺序：data/fonts/ → 系统常见中文路径；找不到 → None（文字图禁用）
_FONT_CANDIDATES = [
    os.path.join(config.DATA_DIR, "fonts", "NotoSansSC-Regular.otf"),
    os.path.join(config.DATA_DIR, "fonts", "NotoSansSC-Regular.ttf"),
    os.path.join(config.DATA_DIR, "fonts", "SourceHanSansSC-Regular.otf"),
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
_font_cache: dict[int, ImageFont.FreeTypeFont | None] = {}


class TextImageCapacityError(Exception):
    """文本超出渲染资源上限（调用方应降级到下一条路径）。"""


def _load_font(size: int) -> ImageFont.FreeTypeFont | None:
    """加载中文字体，lru 风格缓存；找不到返回 None。"""
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
            # 验证真的能渲染中文（避免豆腐块字体：两字必须渲染不同像素）
            im = Image.new("L", (64, 64), 255)
            ImageDraw.Draw(im).text((4, 4), "中", font=font, fill=0)
            a = im.tobytes()
            im2 = Image.new("L", (64, 64), 255)
            ImageDraw.Draw(im2).text((4, 4), "文", font=font, fill=0)
            if a != im2.tobytes():
                break
            font = None
        except Exception:
            font = None
    _font_cache[size] = font
    return font


def _has_font() -> bool:
    return _load_font(FONT_SIZE) is not None


def _split_text_lines(text: str, font: ImageFont.FreeTypeFont,
                      max_width: int) -> list[str]:
    """按字宽实测折行；数字/字母串不拆断到两行（LangBot 细节）。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        raw = raw.rstrip()
        if not raw:
            lines.append("")
            continue
        buf = ""
        i = 0
        token = ""
        while i < len(raw):
            ch = raw[i]
            # 连续的 ASCII 词（数字/单词）先收集成 token 整体放
            if ch.isascii() and (ch.isalnum() or ch in ".,%+-"):
                token += ch
                i += 1
                continue
            if token:
                cand = buf + token
                if font.getlength(cand) <= max_width:
                    buf = cand
                else:
                    if buf:
                        lines.append(buf)
                    buf = token if font.getlength(token) <= max_width else token[0]
                token = ""
            cand = buf + ch
            if font.getlength(cand) <= max_width:
                buf = cand
            else:
                if buf:
                    lines.append(buf)
                buf = ch
            i += 1
        if token:
            if font.getlength(buf + token) <= max_width:
                buf += token
            else:
                lines.append(buf)
                buf = token
        lines.append(buf)
    return lines


def render_text_image(text: str, title: str = "详细内容") -> str | None:
    """把长文本渲染成图片，返回本地 file:/// 路径；无法渲染返回 None。

    CPU 密集，调用方应放 asyncio.to_thread 里跑（不阻塞事件循环）。
    """
    if not _has_font():
        logger.debug("文字图跳过: 服务器无可用中文字体")
        return None
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        raise TextImageCapacityError(f"文本 {len(text)} 字符超上限 {MAX_CHARS}")

    font = _load_font(FONT_SIZE)
    title_font = _load_font(TITLE_FONT_SIZE)
    text_width = CANVAS_WIDTH - PADDING_X * 2
    lines = _split_text_lines(text, font, text_width)
    if len(lines) > MAX_LINES:
        raise TextImageCapacityError(f"行数 {len(lines)} 超上限 {MAX_LINES}")

    title_bar_h = 64
    height = title_bar_h + PADDING_Y + len(lines) * LINE_HEIGHT + PADDING_Y
    if height * CANVAS_WIDTH > MAX_PIXELS:
        raise TextImageCapacityError("画布像素超上限")

    img = Image.new("RGB", (CANVAS_WIDTH, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 标题条
    draw.rectangle([0, 0, CANVAS_WIDTH, title_bar_h], fill=TITLE_BAR_COLOR)
    tw = title_font.getlength(f"📖 {title}")
    draw.text(((CANVAS_WIDTH - tw) / 2, (title_bar_h - TITLE_FONT_SIZE) / 2 - 2),
              f"📖 {title}", font=title_font, fill=TITLE_TEXT_COLOR)

    # 正文
    y = title_bar_h + PADDING_Y
    for line in lines:
        if line:
            draw.text((PADDING_X, y), line, font=font, fill=TEXT_COLOR)
        y += LINE_HEIGHT

    out_dir = os.path.join(config.DATA_DIR, "text_images")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"long_{int(_time.time() * 1000) % 10**10}.png")

    # 压缩到合理体积（PNG→按需降质量重存）
    img.save(out_path, format="PNG", optimize=True)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    if size_mb > 8:
        img.save(out_path, format="JPEG", quality=85)
    return "file:///" + out_path.replace(os.sep, "/")


def cleanup_old_images(keep_minutes: int = 30):
    """清理过期渲染图（发送后图片已在 QQ 服务器，本地只留兜底）。"""
    out_dir = os.path.join(config.DATA_DIR, "text_images")
    try:
        import time as _t
        now = _t.time()
        for name in os.listdir(out_dir):
            p = os.path.join(out_dir, name)
            if now - os.path.getmtime(p) > keep_minutes * 60:
                os.remove(p)
    except Exception:
        pass
