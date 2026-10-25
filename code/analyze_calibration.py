"""
analyze_calibration.py —— 用采集到的一致性分数标定级联阈值

背景：级联判定里，L1（提问改写一致性）和 L2（图像扰动一致性）是便宜的近似，
只有它们"拿不准"时才调用昂贵的 L3（遮蔽归因）。阈值就是那条分界线。

做法：把 L1/L2 分数当作对"该主张是否可靠"的预测，与**生成器给出的标准答案**
比较，扫描阈值算出精确率、召回率与省下的 L3 调用比例。

    预测"可靠"（跳过 L3）  当且仅当  分数 >= 阈值
    TP = 主张正确 且 跳过 L3      → 省了钱，且判断没错
    FP = 主张错误 且 跳过 L3      → 省了钱，但把错误主张放过去了（最危险）
    FN = 主张正确 但 没跳过        → 白花了钱
    TN = 主张错误 且 没跳过        → 正确地送去深查

    precision = TP/(TP+FP)  —— 跳过的那些里有多少是真没问题的
    recall    = TP/(TP+FN)  —— 正确主张里有多少被成功跳过
    省钱比例  = (TP+FP)/N

用法：
    python code/analyze_calibration.py
"""

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def histogram(values: list[float], width: int = 10) -> list[str]:
    buckets = [0] * (width + 1)
    for v in values:
        i = min(width, max(0, int(v * width)))
        buckets[i] += 1
    peak = max(buckets) or 1
    lines = []
    for i, n in enumerate(buckets):
        lo = i / width
        bar = "█" * int(round(n / peak * 40))
        lines.append(f"    [{lo:.1f}-{lo + 1 / width:.1f})  {n:4d}  {bar}")
    return lines


def sweep(rows: list[dict], key: str, thresholds: list[float]) -> list[dict]:
    n = len(rows)
    out = []
    for t in thresholds:
        tp = fp = fn = tn = 0
        for r in rows:
            score = r.get(key)
            if score is None:
                continue
            correct = r["label"] == 1
            skipped = score >= t
            if skipped and correct:
                tp += 1
            elif skipped and not correct:
                fp += 1
            elif not skipped and correct:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if (tp + fp) else float("nan")
        recall = tp / (tp + fn) if (tp + fn) else float("nan")
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision == precision and recall == recall and (precision + recall)
            else float("nan")
        )
        out.append({
            "t": t, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1,
            "saving": (tp + fp) / n if n else float("nan"),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="标定级联阈值")
    ap.add_argument("--scores", type=Path, default=PROJECT_ROOT / "calib_scores.jsonl")
    args = ap.parse_args()

    if not args.scores.exists():
        raise SystemExit(
            f"找不到 {args.scores}\n"
            f"请先采集分数：python code\\halulens.py --task calibrate "
            f"--data calib.jsonl --model deepseek-v4-flash-vision-exp"
        )

    rows = [json.loads(l) for l in args.scores.read_text(encoding="utf-8").splitlines() if l.strip()]
    scored = [r for r in rows if r.get("l1_score") is not None]

    if not scored:
        raise SystemExit("没有任何带一致性分数的记录——标定任务可能还没跑，或全部是否定式回答。")

    correct = [r for r in scored if r["label"] == 1]
    wrong = [r for r in scored if r["label"] == 0]

    print("=" * 74)
    print("标定样本概况")
    print("=" * 74)
    print(f"  总题数            ：{len(rows)}")
    print(f"  肯定式回答（可标定）：{len(scored)}")
    print(f"    主张正确（物体确实存在）：{len(correct)}")
    print(f"    主张错误（物体并不存在）：{len(wrong)}")
    print()

    if len(wrong) == 0:
        print("  ✗ 没有一条错误主张 —— 无法标定。")
        print("    阈值的作用是区分'可靠'与'不可靠'，只有一个类别时任何阈值都同样好。")
        print("    需要能真正诱发模型错误的样本（真实照片或公开基准）。")
        return

    if len(wrong) < 10 or len(correct) < 10:
        print(f"  ⚠ 样本量偏小（正确 {len(correct)} / 错误 {len(wrong)}）。")
        print("    下面的阈值只能作为**初步参考**，不足以作为最终结论。")
        print("    尤其错误类别样本太少时，精确率会被单个样本大幅拉扯。")
        print()

    for key, name in (("l1_score", "L1 提问改写一致性"), ("l2_score", "L2 图像扰动一致性")):
        print("=" * 74)
        print(f"{name} —— 分数分布")
        print("=" * 74)
        print(f"  主张正确（n={len(correct)}）:")
        for ln in histogram([r[key] for r in correct if r.get(key) is not None]):
            print(ln)
        print(f"  主张错误（n={len(wrong)}）:")
        for ln in histogram([r[key] for r in wrong if r.get(key) is not None]):
            print(ln)
        c_mean = sum(r[key] for r in correct if r.get(key) is not None) / max(1, len(correct))
        w_mean = sum(r[key] for r in wrong if r.get(key) is not None) / max(1, len(wrong))
        print(f"  均值：正确 {c_mean:.3f}   错误 {w_mean:.3f}   差 {c_mean - w_mean:+.3f}")
        print()

    thresholds = [round(0.30 + 0.05 * i, 2) for i in range(15)]
    for key, name in (("l1_score", "L1"), ("l2_score", "L2")):
        print("=" * 74)
        print(f"{name} 阈值扫描（预测\"可靠\"= 分数 >= 阈值，即跳过 L3）")
        print("=" * 74)
        print(f"  {'阈值':>5} {'跳过比例':>9} {'精确率':>8} {'召回率':>8} {'F1':>7} "
              f"{'TP':>4} {'FP':>4} {'FN':>4} {'TN':>4}")
        for s in sweep(scored, key, thresholds):
            p = "  n/a" if s["precision"] != s["precision"] else f"{s['precision']:.3f}"
            r = "  n/a" if s["recall"] != s["recall"] else f"{s['recall']:.3f}"
            f = "  n/a" if s["f1"] != s["f1"] else f"{s['f1']:.3f}"
            print(f"  {s['t']:>5.2f} {s['saving']:>8.1%} {p:>8} {r:>8} {f:>7} "
                  f"{s['tp']:>4} {s['fp']:>4} {s['fn']:>4} {s['tn']:>4}")
        print()

    # 推荐：在"不漏放过任何错误主张"的前提下省钱最多
    print("=" * 74)
    print("推荐阈值")
    print("=" * 74)
    for key, name in (("l1_score", "L1"), ("l2_score", "L2")):
        safe = [s for s in sweep(scored, key, thresholds) if s["fp"] == 0 and s["tp"] > 0]
        if safe:
            best = max(safe, key=lambda s: s["saving"])
            print(f"  {name}: 阈值 {best['t']:.2f} —— 零漏放过，"
                  f"省下 {best['saving']:.1%} 的 L3 调用，精确率 {best['precision']:.3f}")
        else:
            print(f"  {name}: 没有任何阈值能做到零漏放过 —— 该分数与主张正确性没有区分度")
    print()
    print("  注意：以上阈值来自合成数据，且错误类别样本有限。")
    print("  在真实照片或公开基准上重跑，才能得到可对外引用的阈值。")


if __name__ == "__main__":
    main()
