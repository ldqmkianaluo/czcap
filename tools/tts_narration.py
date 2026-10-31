"""
tts_narration.py —— 用微软 Edge 的在线语音合成生成旁白音频（免费，无额度限制）

为什么不用剪映的 AI 配音：剪映的配音要会员。Edge 的语音合成走微软 Azure 的
神经语音模型，中文自然度相当好，而且 edge-tts 这个库调用它不需要密钥、无次数
限制，完全免费。

本工具会读取旁白稿，按章节切成若干段，**每段单独生成一个 MP3**——这样正好
对应视频的分段录制，某段要重录只换那一个文件。

依赖：
    pip install edge-tts
    （若装到了自定义目录，运行时需要把该目录加入 PYTHONPATH）

用法：
    python tools/tts_narration.py --list              # 列出可用中文语音
    python tools/tts_narration.py                     # 按旁白稿生成全部分段
    python tools/tts_narration.py --voice zh-CN-XiaoxiaoNeural --rate +8%
    python tools/tts_narration.py --text "试一句"      # 只合成一句话
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = PROJECT_ROOT / "docs" / "演示视频旁白.md"
OUT_DIR = PROJECT_ROOT / "voice"

# 推荐的中文语音（按适合技术演示的程度排序）
RECOMMENDED = [
    ("zh-CN-YunxiNeural", "云希 · 男声，年轻自然，适合讲解"),
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声，自然亲切"),
    ("zh-CN-YunyangNeural", "云扬 · 男声，新闻播报感，偏正式"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声，偏活泼"),
    ("zh-CN-YunjianNeural", "云健 · 男声，偏解说"),
]


def clean(text: str) -> str:
    """去掉标记符号，只留可以朗读的内容。"""
    t = text
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)      # 粗体
    t = re.sub(r"`([^`]+)`", r"\1", t)          # 行内代码
    t = re.sub(r"【[^】]*】", "", t)              # 画面提示
    t = t.replace("……", "。")                    # 省略号读不出来
    t = re.sub(r"\s+", "", t)
    return t.strip()


def parse_sections(md_path: Path) -> list[tuple[str, str]]:
    """从旁白稿里切出 (章节标题, 旁白文本) 列表。"""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_buf: list[str] = []

    for raw in lines:
        line = raw.strip()
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_title and current_buf:
                sections.append((current_title, current_buf))
            current_title, current_buf = m.group(1).strip(), []
            continue
        if line.startswith(">") and not line.startswith("> **") and not line.startswith("> -"):
            body = clean(line.lstrip(">"))
            if len(body) >= 6:                 # 过滤掉零碎的短句
                current_buf.append(body)

    if current_title and current_buf:
        sections.append((current_title, current_buf))

    return [(t, "".join(b)) for t, b in sections]


async def synth(text: str, voice: str, out: Path, rate: str, volume: str) -> None:
    import edge_tts

    out.parent.mkdir(parents=True, exist_ok=True)
    comm = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    await comm.save(str(out))


async def list_voices() -> None:
    import edge_tts

    voices = await edge_tts.list_voices()
    zh = [v for v in voices if v["Locale"].startswith("zh-CN")]
    print(f"中文（zh-CN）语音共 {len(zh)} 个：\n")
    for v in zh:
        print(f"  {v['ShortName']:<34} {v['Gender']:<7} {v.get('FriendlyName', '')}")
    print("\n推荐用于技术演示的：")
    for name, desc in RECOMMENDED:
        print(f"  {name:<34} {desc}")


def main() -> None:
    ap = argparse.ArgumentParser(description="用 Edge 在线语音合成生成旁白音频")
    ap.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    ap.add_argument("--voice", default="zh-CN-YunxiNeural")
    ap.add_argument("--rate", default="+0%", help="语速，如 +8%% 或 -5%%")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--list", action="store_true", help="列出可用中文语音")
    ap.add_argument("--text", default=None, help="只合成这一句，忽略旁白稿")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    try:
        import edge_tts  # noqa: F401
    except ImportError:
        raise SystemExit(
            "缺少 edge-tts，请先安装：\n"
            "    python -m pip install edge-tts\n"
            "若装到了自定义目录，运行时加 PYTHONPATH 指向该目录。"
        )

    if args.list:
        asyncio.run(list_voices())
        return

    if args.text:
        out = args.out_dir / "sample.mp3"
        asyncio.run(synth(args.text, args.voice, out, args.rate, args.volume))
        print(f"已生成：{out}（{out.stat().st_size / 1024:.1f} KB）")
        return

    if not args.script.exists():
        raise SystemExit(f"找不到旁白稿：{args.script}")

    sections = parse_sections(args.script)
    if not sections:
        raise SystemExit("旁白稿里没有解析到内容，请检查格式（每段旁白以 > 开头）")

    total_chars = sum(len(t) for _, t in sections)
    print(f"语音：{args.voice}　语速：{args.rate}")
    print(f"共 {len(sections)} 段，合计 {total_chars} 字，"
          f"按每分钟 220 字约 {total_chars / 220:.1f} 分钟\n")

    for i, (title, text) in enumerate(sections, 1):
        out = args.out_dir / f"{i:02d}.mp3"
        asyncio.run(synth(text, args.voice, out, args.rate, args.volume))
        print(f"  [{i:02d}] {out.name}  {out.stat().st_size / 1024:6.1f} KB  "
              f"{len(text):4d} 字  {title}")

    print(f"\n全部完成，输出目录：{args.out_dir}")
    print("在剪映里按顺序拖入时间轴，与画面对齐即可。")


if __name__ == "__main__":
    main()
