"""
index_ai_logs.py —— 为导出的 AI 对话快照建立索引，并标记与本项目的相关度

赛事要求提交「AI 对话历史 Prompt 快照」。但 DSH 导出的 31 份会话里，相当一部分
与本项目无关（例如早期其他练习）。逐份人工翻阅很费时间，所以先做机器初筛：
按关键词命中数给出相关度，再由人确认。

输出的 ai-logs/README.md 就是提交时的索引页。

用法：
    python tools/index_ai_logs.py
    python tools/index_ai_logs.py --write     # 同时写入 ai-logs/README.md
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 原始导出目录：包含全部会话（含与项目无关的私人内容），已加入 .gitignore，绝不提交
RAW_DIR = PROJECT_ROOT / ".ai-logs-raw"
# 精选目录：只放与本项目相关的会话，随仓库提交
CURATED_DIR = PROJECT_ROOT / "ai-logs"
# 兼容旧名
LOGS_DIR = RAW_DIR

# 强相关关键词：出现任一个基本可判定属于本项目
STRONG_KEYWORDS = ["传智杯", "HaluLens", "AIC", "数智创新与AI应用"]

# 弱相关关键词：只在没有强关键词时作为参考，且需多个同时命中
WEAK_KEYWORDS = [
    "幻觉", "遮蔽", "归因", "申报书", "评测", "POPE",
    "证据区域", "覆盖保证", "视觉误读", "开发类",
]

HEAD_CHARS = None  # None = 读全文；提问分散在长会话各处，截断会漏


def count_strong(text: str) -> int:
    """统计强关键词命中数。AIC 需独立出现——路径 chuanzhi-aic 里的 aic 不算。"""
    n = 0
    for k in STRONG_KEYWORDS:
        if k == "AIC":
            if re.search(r"(?<![-\w])AIC(?![-\w])", text):
                n += 1
        elif k in text:
            n += 1
    return n


def parse_session(path: Path) -> dict:
    full = path.read_text(encoding="utf-8", errors="replace")
    text = full if HEAD_CHARS is None else full[:HEAD_CHARS]

    def grab(pattern: str) -> str:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""

    session_id = grab(r"会话 ID：`([^`]+)`")
    created = grab(r"记录时间：(.+)")
    stats = grab(r"内容统计：(.+)")
    rounds = grab(r"提问轮次：(\d+) 轮")
    span = grab(r"提问轮次：.*?（(第 1 轮 .+?)）")
    tools = grab(r"工具调用与结果 (\d+) 条")

    # 正文里每轮提问的标题形如：### 用户 · 第 12 轮 · 2026-10-25 12:08
    raw_prompts = re.findall(
        r"^### 用户[^\n]*\n\n(.*?)(?=\n### |\n---|\Z)", text, re.S | re.M
    )
    prompts = [re.sub(r"\s+", " ", p).strip()[:70] for p in raw_prompts[:3]]

    # 只在**用户提问**里找关键词，不在全文里找。实测教训：有的会话只是列了一下
    # 项目目录下的文件，全文检索就命中了 HaluLens；而 `chuanzhi-aic` 这个路径本身
    # 就含 "aic"，把 AIC 也命中了。用提问判断"这个会话在干什么"才可靠。
    prompt_text = "\n".join(raw_prompts)
    strong_hits = count_strong(prompt_text)
    weak_hits = sum(1 for k in WEAK_KEYWORDS if k in prompt_text)
    # 判定为相关：命中任一强关键词；或弱关键词命中 >= 4 个
    related = strong_hits > 0 or weak_hits >= 4
    return {
        "file": path.name,
        "session_id": session_id,
        "created": created,
        "stats": stats,
        "rounds": int(rounds) if rounds else len(raw_prompts),
        "span": span,
        "prompts": prompts,
        "strong": strong_hits,
        "weak": weak_hits,
        "related": related,
        "size_kb": round(path.stat().st_size / 1024, 1),
        # 内容完整度：轮次是主干，工具调用与结果决定「过程证据」是否完整。
        # 只比轮次会选错——有的导出轮次多但工具结果全被截断成空，过程反而核验不了。
        "richness": (int(rounds) if rounds else len(raw_prompts)) * 10
        + (int(tools) if tools else 0),
    }


def dedupe_sessions(sessions: list[dict]) -> tuple[list[dict], list[str]]:
    """同一场对话可能被导出成多份快照。

    会话文件会在续写、格式升级时产生新的文件名（mtime 变了），同一个会话 ID
    于是出现好几个 md。原样收进 ai-logs/ 会让评审看到同一场对话的多个副本，
    像是凑数。这里按会话 ID 去重，只留内容最全的一份。
    """
    best: dict[str, dict] = {}
    dropped: list[str] = []
    for s in sessions:
        key = s["session_id"] or s["file"]
        cur = best.get(key)
        if cur is None:
            best[key] = s
        elif s["richness"] > cur["richness"]:
            dropped.append(cur["file"])
            best[key] = s
        else:
            dropped.append(s["file"])
    kept = sorted(best.values(), key=lambda s: s["created"])
    return kept, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description="为 AI 对话快照建立索引")
    ap.add_argument("--write", action="store_true", help="写入 ai-logs/README.md")
    ap.add_argument("--copy-to", default=None,
                    help="把相关会话复制到指定目录（如 ai-logs），用于生成可提交的精简集合")
    args = ap.parse_args()

    if not LOGS_DIR.exists():
        raise SystemExit(f"找不到 {LOGS_DIR}，请先运行 node tools/export-ai-log.mjs")

    files = sorted(LOGS_DIR.glob("prompt-snapshot-*.md"))
    if not files:
        raise SystemExit(f"{LOGS_DIR} 下没有快照文件")

    sessions = [parse_session(f) for f in files]
    sessions.sort(key=lambda s: s["created"])
    sessions, dropped = dedupe_sessions(sessions)
    if dropped:
        print(f"同一会话有多份导出，只保留内容最全的一份，跳过 {len(dropped)} 份：")
        for name in dropped:
            print(f"  - {name}")
        print()

    related = [s for s in sessions if s["related"]]
    others = [s for s in sessions if not s["related"]]

    print("=" * 92)
    print(f"共 {len(sessions)} 份会话；判定与本项目相关 {len(related)} 份，其余 {len(others)} 份不公开")
    print("=" * 92)
    print(f"{'':2} {'强':>3} {'弱':>3}  {'大小KB':>7}  {'时间':<20} 首条提问")
    print("-" * 92)
    for s in sessions:
        flag = "★" if s["related"] else " "
        p = s["prompts"][0] if s["prompts"] else "（无用户提问）"
        print(f"{flag:2} {s['strong']:>3} {s['weak']:>3}  {s['size_kb']:>7}  "
              f"{s['created'][:19]:<20} {p}")

    print()
    print("判定规则：命中任一强关键词（传智杯 / HaluLens / AIC / 数智创新与AI应用）即视为相关；")
    print("          或弱关键词命中 >= 4 个。强关键词能可靠区分，弱关键词仅作参考——")
    print("          实测单靠弱关键词会把『做个倒计时』这类无关会话误判为相关。")

    # 复制相关会话到指定目录（用于生成可提交的精简集合）
    if args.copy_to:
        target = Path(args.copy_to)
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("prompt-snapshot-*.md"):
            old.unlink()
        for s in related:
            (target / s["file"]).write_bytes((LOGS_DIR / s["file"]).read_bytes())
        print(f"\n已复制 {len(related)} 份相关会话 -> {target}")

    if args.write:
        out = CURATED_DIR / "README.md"
        CURATED_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            "# AI 对话历史（Prompt 快照）",
            "",
            "本目录收录本项目开发过程中与 AI 编程助手的协作对话，用于佐证 AI 协同开发过程，",
            "满足赛事「提交 AI 对话历史 Prompt 快照」的要求。",
            "",
            "## 关于收录范围",
            "",
            f"导出工具会从本机取到全部 {len(sessions)} 份会话记录，但其中只有 {len(related)} 份与本项目相关，",
            "其余为与作品无关的私人用途记录。**本目录只公开与本项目相关的部分**，",
            "其余已刻意排除——它们既不构成本作品的开发过程证据，也没有理由公开。",
            "",
            "## 收录的会话",
            "",
            "| 记录时间 | 轮次 | 覆盖时间 | 大小 | 首条提问 | 文件 |",
            "|---|---|---|---|---|---|",
        ]
        for s in related:
            p = (s["prompts"][0] if s["prompts"] else "（无）").replace("|", "\\|")
            span = (s["span"] or "—").replace("|", "\\|")
            lines.append(
                f"| {s['created'][:19]} | {s['rounds']} | {span} | {s['size_kb']} KB | {p} | "
                f"[{s['file']}](./{s['file']}) |"
            )
        lines += [
            "",
            "## 怎么看这份快照",
            "",
            "每个快照文件开头都有一张「提问轮次索引」表：轮次 / 时间 / 提问首行 / 定位。",
            "正文里每轮提问前都有锚点（`#r01`…），点索引里的「跳转」直接落到该轮原文和它带来的产出。",
            "",
            "要核验「哪一轮提问催生了哪个功能」，看 [`docs/提交说明.md`](../docs/提交说明.md)",
            "里的「演进链路」表——它把每一轮的提问、当时产出的功能、以及对应的 git 提交对在了一起。",
            "",
            "## 复现方式",
            "",
            "```bash",
            "node tools/export-ai-log.mjs                       # 导出全部会话到 .ai-logs-raw/",
            "python tools/index_ai_logs.py --copy-to ai-logs    # 筛出相关会话到 ai-logs/",
            "```",
            "",
            "## 说明",
            "",
            "- 内容由脚本从 DSH 会话记录直接导出，未经人工删改；",
            "- 每条记录都带本地时间（UTC+8），可以直接和 git 提交时间对照；",
            "- AI 的思考过程（reasoning）默认不导出，可加 `--full` 保留；",
            "- 已剔除非用户输入的记录：客户端自动注入的运行时上下文、后台任务完成回执；",
            "- 同一会话的多份导出只保留内容最全的一份；",
            "- 会话按时间累积，提交前应重新导出以覆盖最新进展。",
        ]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n索引已写入：{out}")


if __name__ == "__main__":
    main()
