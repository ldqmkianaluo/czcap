"""
halulens 自检脚本 —— 只测试不需要联网的部分

用途：在花 API 额度之前，先确认纯逻辑是对的。
运行：python code/selftest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import halulens as H  # noqa: E402

failures: list[str] = []


def check(name: str, actual, expected, tol: float = 1e-4) -> None:
    ok = (
        abs(actual - expected) <= tol
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float))
        else actual == expected
    )
    if ok:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}: 实际={actual!r} 期望={expected!r}")
        failures.append(name)


print("=" * 60)
print("1. 一致性度量 normalized_consistency")
print("=" * 60)

r = H.normalized_consistency([True, True, True])
check("全部相同 -> 一致性 1.0", round(r.score, 4), 1.0)
check("全部相同 -> yes 计数", r.distribution["yes"], 3)
check("全部相同 -> 无解析失败", r.unparseable, 0)

r = H.normalized_consistency([True, True, False])
check("2:1 分歧 -> 一致性低（约 0.08）", round(r.score, 4), 0.0817, tol=1e-3)

r = H.normalized_consistency([True, False])
check("1:1 分歧 -> 一致性 0.0", round(r.score, 4), 0.0)

r = H.normalized_consistency([True, False, None])
check("含未解析项 -> 只统计可解析的", r.unparseable, 1)
check("1:1 且含未解析 -> 一致性 0.0", round(r.score, 4), 0.0)

r = H.normalized_consistency([None, None])
check("全部未解析 -> 一致性 0.0", round(r.score, 4), 0.0)
check("全部未解析 -> unparseable 计数", r.unparseable, 2)

print()
print("=" * 60)
print("2. 图像遮蔽 occlude_grid")
print("=" * 60)

img = Image.new("RGB", (90, 90), (255, 0, 0))
masked = H.occlude_grid(img, 0, 0, grid=3)

check("输出尺寸不变", masked.size, (90, 90))
check("被遮蔽格中心变灰", masked.getpixel((15, 15)), (128, 128, 128))
check("未被遮蔽区域保持原色", masked.getpixel((60, 60)), (255, 0, 0))
check("原图未被就地修改（重要）", img.getpixel((15, 15)), (255, 0, 0))

masked_br = H.occlude_grid(img, 2, 2, grid=3)
check("右下格被遮蔽", masked_br.getpixel((75, 75)), (128, 128, 128))
check("左上格未受影响", masked_br.getpixel((15, 15)), (255, 0, 0))

print()
print("=" * 60)
print("3. 指标计算 classification_metrics")
print("=" * 60)

m = H.classification_metrics([1, 1, 0, 0], [1, 0, 1, 0])
check("完全错误分类 -> accuracy 0.5", m["accuracy"], 0.5)
check("完全错误分类 -> precision 0.5", m["precision"], 0.5)
check("完全错误分类 -> recall 0.5", m["recall"], 0.5)
check("完全错误分类 -> f1 0.5", m["f1"], 0.5)
check("完全错误分类 -> mcc 0.0", m["mcc"], 0.0)

m = H.classification_metrics([1, 1, 1, 0], [1, 1, 0, 0])
check("部分正确 -> accuracy 0.75", m["accuracy"], 0.75)
check("部分正确 -> precision 1.0", m["precision"], 1.0)
check("部分正确 -> recall 0.6667", m["recall"], 0.6667, tol=1e-3)
check("部分正确 -> f1 0.8", m["f1"], 0.8, tol=1e-3)
# MCC = (tp*tn - fp*fn) / sqrt((tp+fp)(tp+fn)(tn+fp)(tn+fn))
#     = (2*1 - 0*1) / sqrt(2*3*1*2) = 2/sqrt(12) = 0.5774
check("部分正确 -> mcc 0.5774", m["mcc"], 0.5774, tol=1e-3)
check("漏检率 0.3333", m["miss_rate"], 0.3333, tol=1e-3)
check("误报率 0.0", m["false_alarm_rate"], 0.0)

m = H.classification_metrics([1, 0], [1, 0])
check("全对 -> accuracy 1.0", m["accuracy"], 1.0)
check("全对 -> f1 1.0", m["f1"], 1.0)

print()
print("=" * 60)
print("4. 图像扰动变换 PERTURBATIONS")
print("=" * 60)

base = Image.new("RGB", (100, 80), (10, 200, 30))
for name in ("jpeg_recompress", "crop_5pct", "scale_90", "flip_none"):
    out = H.PERTURBATIONS[name](base)
    check(f"{name} 返回 RGB 图像", out.mode, "RGB")
    check(f"{name} 未修改原图", base.size, (100, 80))

check("crop_5pct 缩小了尺寸", H.PERTURBATIONS["crop_5pct"](base).size, (90, 72))
check("scale_90 按 0.9 缩放", H.PERTURBATIONS["scale_90"](base).size, (90, 72))

print()
print("=" * 60)
print("5. yes/no 解析 _parse_yesno（整个方法的地基）")
print("=" * 60)

check("英文否定句", H._parse_yesno("No, there is no cat in the image."), False)
check("英文肯定句", H._parse_yesno("Yes, there is a cat."), True)
check("只回答 No.", H._parse_yesno("No."), False)
check("中文否定（没有）", H._parse_yesno("图中没有猫。"), False)
check("中文肯定（有）", H._parse_yesno("图中有一只猫。"), True)
check("中文：没有 优先于 有", H._parse_yesno("这不是猫。"), False)
check("思维链在前、结论在后", H._parse_yesno("Let me look at the shapes.\nNo"), False)
check("无法判定返回 None", H._parse_yesno("I cannot determine."), None)
check("空字符串返回 None", H._parse_yesno(""), None)
check("已知局限：do not 不被识别", H._parse_yesno("I do not see a cat."), None)
check("_earliest_decision 内部函数", H._earliest_decision("没有猫"), False)
check("强制格式后缀已定义", isinstance(H.YESNO_SUFFIX, str) and len(H.YESNO_SUFFIX) > 0, True)

print()
print("=" * 60)
print("6. openai SDK 接口形状检查")
print("=" * 60)

try:
    from openai import AsyncOpenAI

    check("AsyncOpenAI 可导入", True, True)
    client = AsyncOpenAI(api_key="sk-not-a-real-key")
    check("client.chat.completions.create 存在", callable(client.chat.completions.create), True)
except Exception as exc:  # noqa: BLE001
    print(f"  [FAIL] openai SDK 检查异常: {exc!r}")
    failures.append("openai SDK")

print()
print("=" * 60)
if failures:
    print(f"结果：{len(failures)} 项失败")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("结果：全部通过")
