"""
make_calib_set.py —— 生成阈值标定用的题目集

为什么需要专门的标定集：级联判定的阈值（"一致性分数低于多少就送去昂贵的
遮蔽归因"）必须有已知答案的样本来标定。这里不用归因结果当标签（那是循环
论证），而是用**生成器自带的标准答案**——图形的位置、颜色、数量都是程序
自己放的，标准答案绝对可靠。

    问图中确实存在的物体  → 答 Yes 即为有视觉依据
    问图中不存在的物体    → 答 Yes 即为无视觉依据（幻觉）

关键是**必须让模型真的出错**，否则没有幻觉样本，阈值无从标定。实测教训：

    每图 2 个图形的稀疏排布 → 50 道负题只错 3 道（6%）
    加位置题、计数题        → 仍然不够

所以这里改为**每图 6 个图形密集排布**：图形多了之后，计数和空间判断才真正
有难度，模型才会凭印象作答。

每图 7 道题（2 正 5 负）：
    正题                 物体确实存在
    easy_negative        形状和颜色都不在图中
    same_color_other_shape  颜色在、形状在，但这个组合不在
    same_shape_other_color  形状在、颜色在，但这个组合不在
    wrong_position       问它是否在相反一侧
    wrong_count          问比实际多一个的数量

运行：
    python tools/make_calib_set.py
    python tools/make_calib_set.py --images 30 --seed 7
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "images" / "calib"
OUT_JSONL = PROJECT_ROOT / "calib.jsonl"

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
SHAPES = ("circle", "square", "triangle", "rect", "ellipse")
ALL = [(s, c) for s in SHAPES for c in COLORS]
NUM_WORD = {2: "two", 3: "three", 4: "four", 5: "five"}

W, H = 480, 360
COLS, ROWS = 3, 2  # 每图 6 个槽位


def draw_shape(d: ImageDraw.ImageDraw, kind: str, color: str, box) -> None:
    x0, y0, x1, y1 = box
    rgb = COLORS[color]
    if kind == "circle":
        # 必须用正方形外框，否则画出来是椭圆——模型被问"是不是圆"时会纠结到截断
        side = min(x1 - x0, y1 - y0)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        d.ellipse((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2), fill=rgb)
    elif kind == "ellipse":
        d.ellipse((x0, y0, x1, y1), fill=rgb)
    elif kind == "square":
        side = min(x1 - x0, y1 - y0)
        d.rectangle((x0, y0, x0 + side, y0 + side), fill=rgb)
    elif kind == "rect":
        d.rectangle((x0, y0, x1, y1), fill=rgb)
    elif kind == "triangle":
        d.polygon([((x0 + x1) // 2, y0), (x0, y1), (x1, y1)], fill=rgb)


def build_scene(rng: random.Random):
    """在 3×2 槽位里各放一个图形，位置和尺寸带抖动。返回 [(kind, color, box_px, norm_box)]"""
    shapes = []
    cw, ch = W / COLS, H / ROWS
    used = set()
    for r in range(ROWS):
        for c in range(COLS):
            # 优先用没出现过的组合，让每张图的形状颜色尽量分散
            for _ in range(50):
                kind, color = rng.choice(SHAPES), rng.choice(list(COLORS))
                if (kind, color) not in used or len(used) >= len(ALL) - 2:
                    break
            used.add((kind, color))

            # 尺寸必须让形状在**视觉上可区分**。原先宽高都取同一个 size，结果
            # 所有 rect / ellipse 都画成了完美正方形和正圆——于是"图中有 red rect"
            # 时去问"Is there a red square?"，模型答 yes 完全正确，而标准答案却把它
            # 标成"不存在"。这个 bug 让整批标定标签失效过一次，代价很大。
            # 现在 rect / ellipse 强制明显的非方形长宽比。
            if kind in ("circle", "square", "triangle"):
                bw = bh = rng.randint(55, 85)
            else:
                if rng.random() < 0.5:
                    bw, bh = rng.randint(95, 130), rng.randint(45, 60)
                else:
                    bw, bh = rng.randint(45, 60), rng.randint(95, 130)

            x0 = int(c * cw + rng.randint(4, max(4, int(cw - bw - 4))))
            y0 = int(r * ch + rng.randint(4, max(4, int(ch - bh - 4))))
            x0 = max(2, min(x0, W - bw - 2))
            y0 = max(2, min(y0, H - bh - 2))
            x1, y1 = x0 + bw, y0 + bh
            norm = (x0 / W, y0 / H, x1 / W, y1 / H)
            shapes.append((kind, color, (x0, y0, x1, y1), norm))

    # 守卫：标准答案必须与"视觉上能看到的"一致。曾经因为宽高恒等，rect 画成了
    # 正方形、ellipse 画成了圆，导致"图中有 red rect"时问"Is there a red square?"
    # 模型答 yes 完全正确，而标签却写成"不存在"——整批标定数据因此失效。
    for kind, _color, b, _norm in shapes:
        bw, bh = b[2] - b[0], b[3] - b[1]
        ratio = max(bw, bh) / max(1, min(bw, bh))
        if kind in ("rect", "ellipse"):
            assert ratio >= 1.5, (
                f"{kind} 的长宽比只有 {ratio:.2f}，视觉上与 square/circle 无法区分，"
                f"会导致标准答案失真"
            )
        else:
            assert ratio <= 1.05, f"{kind} 的长宽比为 {ratio:.2f}，应当接近 1"

    return shapes


def make_negatives(shapes) -> list[tuple[str, str]]:
    """构造负题。返回 (类型, 问题)，全部的标准答案都是 No。"""
    present = {(k, c) for k, c, _, _ in shapes}
    shapes_in = {k for k, _ in present}
    colors_in = {c for _, c in present}
    negs: list[tuple[str, str]] = []

    easy = next(((s, c) for s, c in ALL if s not in shapes_in and c not in colors_in), None)
    if easy:
        negs.append(("easy_negative", f"Is there a {easy[1]} {easy[0]} in the image?"))

    hard_sc = next(((s, c) for s, c in ALL if c in colors_in and s not in shapes_in), None)
    if hard_sc:
        negs.append(("same_color_other_shape",
                     f"Is there a {hard_sc[1]} {hard_sc[0]} in the image?"))

    hard_ss = next(((s, c) for s, c in ALL if s in shapes_in and c not in colors_in), None)
    if hard_ss:
        negs.append(("same_shape_other_color",
                     f"Is there a {hard_ss[1]} {hard_ss[0]} in the image?"))

    k0, c0, _, norm0 = shapes[0]
    cx = (norm0[0] + norm0[2]) / 2
    wrong_side = "right" if cx < 0.5 else "left"
    negs.append(("wrong_position",
                 f"Is the {c0} {k0} on the {wrong_side} side of the image?"))

    n = sum(1 for k, c, _, _ in shapes if (k, c) == (k0, c0))
    n_ask = n + 1
    word = NUM_WORD.get(n_ask, str(n_ask))
    negs.append(("wrong_count",
                 f"Are there {word} {c0} {k0}{'' if n_ask == 1 else 's'} in the image?"))

    return negs


def main() -> None:
    ap = argparse.ArgumentParser(description="生成阈值标定集")
    ap.add_argument("--images", type=int, default=20, help="生成多少张图")
    ap.add_argument("--seed", type=int, default=20261007)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    rows = []

    for i in range(args.images):
        name = f"{i + 1:03d}"
        shapes = build_scene(rng)

        img = Image.new("RGB", (W, H), (242, 242, 242))
        d = ImageDraw.Draw(img)
        for kind, color, box, _ in shapes:
            draw_shape(d, kind, color, box)
        img.save(IMAGES_DIR / f"{name}.png")

        # 正题：随机挑两个确实存在的物体
        for kind, color, _, _ in rng.sample(shapes, 2):
            rows.append({
                "image": f"images/calib/{name}.png",
                "question": f"Is there a {color} {kind} in the image?",
                "label": 1,
                "neg_type": None,
            })

        for neg_type, question in make_negatives(shapes):
            rows.append({
                "image": f"images/calib/{name}.png",
                "question": question,
                "label": 0,
                "neg_type": neg_type,
            })

    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    pos = sum(1 for r in rows if r["label"] == 1)
    print(f"已生成 {args.images} 张图 -> {IMAGES_DIR}")
    print(f"已生成标定题 {len(rows)} 条 -> {OUT_JSONL}")
    print(f"  正题（答 yes 才对）: {pos}")
    print(f"  负题（答 no 才对） : {len(rows) - pos}")
    for k, v in Counter(r["neg_type"] for r in rows if r["neg_type"]).items():
        print(f"      {k}: {v}")
    print()
    print("下一步——摸底，看模型在密集排布下会不会出错：")
    print("  python code\\halulens.py --task pope_b0 --data calib.jsonl "
          "--model deepseek-v4-flash-vision-exp")


if __name__ == "__main__":
    main()
