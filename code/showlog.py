"""
日志查看工具 —— 回看模型每次到底说了什么

用途：出现解析失败（unparseable）或结果反常时，用它回看原始输出。
     不要靠猜，日志里有原话。

运行：
    python code/showlog.py                # 看最新一次运行
    python code/showlog.py --file xxx.jsonl
    python code/showlog.py --only-null    # 只看解析失败的调用
"""

import argparse
import glob
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser(description="查看 halulens 运行日志")
parser.add_argument("--file", type=str, default=None, help="指定日志文件；省略则用最新的")
parser.add_argument("--only-null", action="store_true", help="只显示解析失败的调用")
parser.add_argument("--raw-width", type=int, default=140, help="原始输出截断宽度")
args = parser.parse_args()

runs_dir = PROJECT_ROOT / "runs"
if args.file:
    path = Path(args.file)
else:
    files = sorted(glob.glob(str(runs_dir / "*.jsonl")), key=os.path.getmtime)
    if not files:
        raise SystemExit(f"在 {runs_dir} 下没找到任何日志文件")
    path = Path(files[-1])

print(f"日志文件：{path.name}")
print(f"路径：{path}\n")

rows = []
with path.open(encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))

meta = next((r["__meta__"] for r in rows if "__meta__" in r), None)
summary = next((r["__summary__"] for r in rows if "__summary__" in r), None)

if meta:
    print("=" * 78)
    print("运行元信息（复现实验必须靠这些字段）")
    print("=" * 78)
    print(f"  运行时间     : {meta.get('started_at')}")
    print(f"  请求模型     : {meta.get('model')}")
    print(f"  服务端返回版本: {meta.get('model_reported_version') or '(未记录)'}")
    print(f"  prompt 版本  : {meta.get('prompt_version')}")
    print(f"  生成参数     : {meta.get('gen_params')}")
    print(f"  测试参数     : {meta.get('probe_params')}")
    print()

if summary:
    print("=" * 78)
    print("成本统计")
    print("=" * 78)
    print(f"  实际调用 {summary.get('api_calls')} 次 | 缓存命中 {summary.get('cached_calls')} 次 "
          f"| 缓存命中率 {summary.get('cache_hit_rate', 0):.1%}")
    print(f"  输入 {summary.get('input_tokens')} tok | 输出 {summary.get('output_tokens')} tok")
    if summary.get("model_reported_version"):
        print(f"  服务端实际版本: {summary['model_reported_version']}")
    print()

calls = [r for r in rows if r.get("task") == "vlm_yesno"]
failed = [r for r in calls if r.get("verdict") is None]

print("=" * 78)
print(f"yes/no 调用明细（共 {len(calls)} 次，其中解析失败 {len(failed)} 次）")
print("=" * 78)

shown = failed if args.only_null else calls
if not shown:
    print("  （没有符合条件的调用）")

for i, r in enumerate(shown, 1):
    tag = "解析失败" if r.get("verdict") is None else ("是" if r["verdict"] else "否")
    cache = "[缓存]" if r.get("cached") else "[实时]"
    raw = (r.get("raw") or "").replace("\n", " ⏎ ")
    if len(raw) > args.raw_width:
        raw = raw[: args.raw_width] + " …"
    print(f"\n{i:2d}. {cache} 判定={tag}")
    print(f"    问题: {r.get('question')}")
    print(f"    原话: {raw}")

print()
if failed:
    print(f"⚠ 有 {len(failed)} 次解析失败。原话已在上方列出——根据原话调整 prompt 或解析规则，")
    print("  不要把失败静默当成 False，它本身就是一个需要报告的指标。")
else:
    print("✓ 本次运行没有解析失败的调用。")
