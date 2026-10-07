"""
make_mini_set.py —— 生成迷你评测集（合成图片 + 标注）

用途：在拿到真实照片或下载公开基准之前，先用合成数据把**评测流程**跑通。
      合成图片不适合衡量模型的真实水平，但完全够用来验证数据加载、批量推理、
      指标计算这条链路是通的。

生成物：
    images/01.png ... images/10.png
    mini.jsonl

运行：
    python tools/make_mini_set.py
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"
OUT_JSONL = PROJECT_ROOT / "mini.jsonl"

# 调色板
COLORS = {
    "red": (200, 30, 30),
    "blue": (30, 60, 200),
    "green": (30, 160, 60),
    "yellow": (230, 200, 40),
    "orange": (240, 140, 30),
    "purple": (130, 60, 180),
    "black": (20, 20, 20),
    "brown": (140, 90, 50),
    "cyan": (40, 180, 190),
}

# 每张图的内容：(形状, 颜色, 位置框)
# 位置框是归一化坐标 (x0, y0, x1, y1)，避免写死像素
SPEC: list[tuple[str, list[tuple[str, str, tuple[float, float, float, float]]]]] = [
    ("01", [("circle", "red", (0.08, 0.15, 0.42, 0.62)),
            ("square", "blue", (0.55, 0.30, 0.90, 0.80))]),
    ("02", [("triangle", "green", (0.15, 0.20, 0.55, 0.80))]),
    ("03", [("rect", "purple", (0.20, 0.30, 0.80, 0.65)),
            ("circle", "yellow", (0.60, 0.15, 0.90, 0.45))]),
    ("04", [("circle", "blue", (0.10, 0.20, 0.40, 0.60)),
            ("triangle", "orange", (0.50, 0.25, 0.90, 0.85))]),
    ("05", [("square", "brown", (0.25, 0.25, 0.75, 0.70))]),
    ("06", [("circle", "cyan", (0.20, 0.20, 0.60, 0.60)),
            ("rect", "black", (0.55, 0.50, 0.92, 0.90))]),
    ("07", [("square", "red", (0.10, 0.10, 0.45, 0.45)),
            ("square", "green", (0.50, 0.50, 0.88, 0.88))]),
    ("08", [("triangle", "purple", (0.15, 0.30, 0.50, 0.80)),
            ("circle", "orange", (0.58, 0.20, 0.88, 0.50))]),
    ("09", [("ellipse", "yellow", (0.12, 0.30, 0.60, 0.70))]),
    ("10", [("rect", "cyan", (0.15, 0.15, 0.55, 0.55)),
            ("circle", "brown", (0.55, 0.45, 0.90, 0.85))]),
]

# 用于构造"图中不存在"的负样本题目
COMBOS = [(k, c) for k in ("circle", "square", "triangle", "rect", "ellipse") for c in COLORS]

W, H = 480, 360


def draw_shape(d: ImageDraw.ImageDraw, kind: str, color: str, box) -> None:
    x0, y0, x1, y1 = box
    px = (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
    rgb = COLORS[color]

    if kind == "circle":
        # 必须强制成正方形外框再画椭圆，否则归一化框一旦不是正方形，
        # 画出来的就是椭圆。实测后果很严重：模型被问"这是圆吗"时会反复纠结，
        # 直到把 max_tokens 耗尽、答案被截断成空字符串。
        side = min(px[2] - px[0], px[3] - px[1])
        cx = (px[0] + px[2]) // 2
        cy = (px[1] + px[3]) // 2
        d.ellipse((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2), fill=rgb)
    elif kind == "ellipse":
        # 椭圆故意保持非正方外框，与 circle 形成对比
        d.ellipse(px, fill=rgb)
    elif kind == "square":
        # 同理：正方形必须强制等边长，否则"矩形"会被当成 square
        side = min(px[2] - px[0], px[3] - px[1])
        d.rectangle((px[0], px[1], px[0] + side, px[1] + side), fill=rgb)
    elif kind == "rect":
        d.rectangle(px, fill=rgb)
    elif kind == "triangle":
        d.polygon([((px[0] + px[2]) // 2, px[1]), (px[0], px[3]), (px[2], px[3])], fill=rgb)


def main() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    rows = []

    for name, shapes in SPEC:
        img = Image.new("RGB", (W, H), (242, 242, 242))
        d = ImageDraw.Draw(img)
        for kind, color, box in shapes:
            draw_shape(d, kind, color, box)
        img.save(IMAGES_DIR / f"{name}.png")

        present = {(k, c) for k, c, _ in shapes}
        # 每张图 2 道"存在"的题
        for kind, color, _ in shapes[:2]:
            rows.append({
                "image": f"images/{name}.png",
                "question": f"Is there a {color} {kind} in the image?",
                "label": 1,
            })
        # 每张图 1 道"不存在"的题，挑第一个不在图中的组合
        for combo in COMBOS:
            if combo not in present:
                rows.append({
                    "image": f"images/{name}.png",
                    "question": f"Is there a {combo[1]} {combo[0]} in the image?",
                    "label": 0,
                })
                break

    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    pos = sum(1 for r in rows if r["label"] == 1)
    neg = len(rows) - pos
    print(f"已生成图片 {len(SPEC)} 张 -> {IMAGES_DIR}")
    print(f"已生成题目 {len(rows)} 条 -> {OUT_JSONL}")
    print(f"  正样本（答 yes 才对）: {pos}")
    print(f"  负样本（答 no 才对） : {neg}")
    print()
    print("运行评测：")
    print("  python code\\halulens.py --task pope_b0 --data mini.jsonl "
          "--model deepseek-v4-flash-vision-exp")


if __name__ == "__main__":
    main()
