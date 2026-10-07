"""
viz.py —— 把遮蔽归因的结果画成一张可核验的图

设计原则：这张图不是装饰，而是**证据**。看的人应该能一眼看出：
    1. 被测的是什么问题、模型原话是什么；
    2. 哪些区域被遮住后答案会翻转（这就是判定所依据的证据）；
    3. 精化之后的证据区域具体落在哪里。

三联图：原图（含被测窗口）→ 遮蔽影响图 → 精化后的证据区域。

排版按文字行数动态计算高度：模型回答长短不一，任何写死的高度都会在多行时
发生重叠（这是实测踩过的坑）。
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 中文字体候选。找不到就退回默认字体（中文会显示成方块，但不至于崩溃）。
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    r"C:\Windows\Fonts\Deng.ttf",      # 等线
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

BG = (250, 250, 250)
INK = (26, 26, 26)
MUTED = (120, 120, 120)
LINE = (205, 205, 205)
HIT = (214, 45, 45)         # 影响区域：红
MISS = (165, 165, 165)      # 测过但无影响：灰
EVIDENCE = (230, 60, 60)    # 精化后的证据区域

ANSWER_PREVIEW_CHARS = 200

Box = tuple[float, float, float, float]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_width(img: Image.Image, width: int) -> Image.Image:
    ratio = width / img.width
    return img.resize((width, max(1, int(img.height * ratio))), Image.LANCZOS)


def _px_box(box: Box, w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))


def _wrap(text: str, max_chars: int) -> list[str]:
    """按字数硬换行。返回行列表，便于先算高度再落笔。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_lines(
    draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[str], font, fill
) -> int:
    lh = font.size + 8
    for i, ln in enumerate(lines):
        draw.text((x, y + i * lh), ln, font=font, fill=fill)
    return len(lines) * lh


def make_attribution_figure(
    image: Image.Image,
    *,
    question: str,
    baseline_text: str,
    verdict: str,
    tested_boxes: list[Box],
    flip_boxes: list[Box],
    refined_boxes_norm: list[Box],
    calls: int,
    out_path: Path,
    panel_w: int = 440,
) -> Path:
    """
    生成三联图并保存。

    tested_boxes       所有被测过的遮蔽窗口（归一化坐标，带重叠）
    flip_boxes         其中使答案翻转的窗口——它们就是判定的依据
    refined_boxes_norm 边缘收缩后得到的证据区域
    """
    base0 = _fit_width(image.convert("RGB"), panel_w)
    ph = base0.height

    margin, gap = 26, 20
    f_title = load_font(26)
    f_sub = load_font(18)
    f_label = load_font(19)
    f_small = load_font(16)

    W = margin * 2 + gap * 2 + panel_w * 3
    text_w = W - margin * 2
    chars_sub = max(20, int(text_w / 9.8))
    chars_small = max(20, int(text_w / 8.6))

    preview = baseline_text.strip().replace("\n", " ")
    if len(preview) > ANSWER_PREVIEW_CHARS:
        preview = preview[:ANSWER_PREVIEW_CHARS] + "…（完整原文见运行日志）"

    # ---- 先算高度 ----
    q_lines = _wrap(f"问题：{question}", chars_sub)
    a_lines = _wrap(f"模型回答：{preview}", chars_sub)

    # 图例只陈述数字，解释交给下方 verdict 行——因为"翻转 0 个"在肯定式与
    # 否定式回答下含义完全不同，混在图例里会产生误导。
    legend = (
        f"遮蔽影响：被测窗口 {len(tested_boxes)} 个，答案翻转 {len(flip_boxes)} 个"
        + (f"；收缩后证据区域 {len(refined_boxes_norm)} 块" if flip_boxes else "；无可高亮区域")
    )
    v_lines = _wrap(f"判定：{verdict}", chars_small)

    y_text = margin + f_title.size + 14
    panel_label_y = y_text + (len(q_lines) + len(a_lines)) * (f_sub.size + 8) + 12
    panel_y = panel_label_y + f_label.size + 8

    ly = panel_y + ph + 22
    cost_y = ly + 14 + (len(_wrap(legend, chars_small)) + len(v_lines)) * (f_small.size + 8) + 10
    H = cost_y + f_small.size + margin

    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)

    # ---- 顶部文字 ----
    draw.text((margin, margin), "HaluLens · 遮蔽敏感性归因", font=f_title, fill=INK)
    yy = y_text
    yy += _draw_lines(draw, margin, yy, q_lines, f_sub, INK)
    _draw_lines(draw, margin, yy, a_lines, f_sub, MUTED)

    # ---- 三联面板 ----
    labels = ["① 原图（浅线为被测窗口）", "② 遮蔽影响图", "③ 收缩后的证据区域"]

    for i in range(3):
        px = margin + i * (panel_w + gap)
        base = base0.copy()

        if i == 0:
            d = ImageDraw.Draw(base)
            for b in tested_boxes:
                d.rectangle(_px_box(b, panel_w, ph), outline=(226, 226, 226))

        elif i == 1:
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for b in tested_boxes:
                od.rectangle(_px_box(b, panel_w, ph), fill=MISS + (34,))
            for b in flip_boxes:
                # 多个翻转窗口重叠处会更深，恰好体现该区域被多个窗口共同证实
                od.rectangle(
                    _px_box(b, panel_w, ph), fill=HIT + (92,), outline=HIT + (255,), width=2
                )
            base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

        else:
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for b in refined_boxes_norm:
                od.rectangle(
                    _px_box(b, panel_w, ph),
                    fill=EVIDENCE + (112,),
                    outline=EVIDENCE + (255,),
                    width=3,
                )
            base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

        canvas.paste(base, (px, panel_y))
        draw.rectangle((px, panel_y, px + panel_w, panel_y + ph), outline=(214, 214, 214))
        draw.text((px, panel_label_y), labels[i], font=f_label, fill=INK)

    # ---- 底部结论 ----
    draw.line([(margin, ly - 10), (W - margin, ly - 10)], fill=LINE, width=1)
    yy = ly + 4
    yy += _draw_lines(draw, margin, yy, _wrap(legend, chars_small), f_small, INK)
    yy += _draw_lines(draw, margin, yy, v_lines, f_small, MUTED)
    draw.text(
        (margin, cost_y),
        f"共 {calls} 次模型调用    |    红色的语义是：把它遮住，模型的答案就会改变",
        font=f_small,
        fill=MUTED,
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path
