"""
extract_subs.py —— 从视频/音频里提取字幕文本（本地运行，完全离线）

为什么不用在线服务：演示视频里有代码、项目结构、配置画面，上传到第三方不合适。
为什么用 faster-whisper 而不是 openai-whisper：前者基于 CTranslate2，CPU 上快数倍，
而且自带 PyAV 音频解码，**不需要单独安装 ffmpeg**。

安装（在你自己的终端里执行，本项目环境没装）：
    pip install faster-whisper

国内下载模型可能连不上 HuggingFace，先设镜像：
    PowerShell:  $env:HF_ENDPOINT = "https://hf-mirror.com"
    CMD:         set HF_ENDPOINT=https://hf-mirror.com

用法：
    python tools/extract_subs.py 视频.mp4
    python tools/extract_subs.py 视频.mp4 --model medium      # 更好的中文效果
    python tools/extract_subs.py 视频.mp4 --model large-v3    # 最好，但慢
    python tools/extract_subs.py 视频.mp4 --txt-only          # 只要纯文本，不要时间轴

模型大小与取舍（中文）：
    small     ~500MB   快，准确率一般，适合先跑通
    medium    ~1.5GB   推荐，中文准确率明显更好
    large-v3  ~3GB     最好，CPU 上会慢很多

输出：
    视频.srt   带时间轴的字幕，可直接拖进剪映
    视频.txt   纯文本，便于编辑或喂给 TTS
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def fmt_ts(seconds: float) -> str:
    """秒 -> SRT 时间戳 00:00:00,000"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    ap = argparse.ArgumentParser(description="本地提取视频字幕（faster-whisper）")
    ap.add_argument("input", type=Path, help="视频或音频文件")
    ap.add_argument("--model", default="medium",
                    help="模型：tiny / base / small / medium / large-v3（默认 medium）")
    ap.add_argument("--device", default="cpu", help="cpu 或 cuda")
    ap.add_argument("--compute-type", default="int8",
                    help="cpu 建议 int8；显卡可用 float16")
    ap.add_argument("--lang", default="zh", help="语言，默认 zh")
    ap.add_argument("--txt-only", action="store_true", help="只输出纯文本，不生成 SRT")
    ap.add_argument("-o", "--out-dir", type=Path, default=None)
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"找不到文件：{args.input}")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit(
            "缺少 faster-whisper。请先在你自己的终端里安装：\n"
            "    pip install faster-whisper\n"
            "若下载模型失败，先设镜像：\n"
            '    $env:HF_ENDPOINT = "https://hf-mirror.com"'
        )

    out_dir = args.out_dir or args.input.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.input.stem

    print(f"输入：{args.input}")
    print(f"模型：{args.model}（{args.device} / {args.compute_type}）")
    print("首次运行会下载模型，请耐心等待……\n")

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    segments, info = model.transcribe(
        str(args.input),
        language=args.lang,
        vad_filter=True,              # 跳过静音，避免产生空字幕
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
    )

    print(f"检测到语言：{info.language}（置信度 {info.language_probability:.2f}）")
    print(f"音频时长：{info.duration:.1f} 秒\n")

    srt_lines: list[str] = []
    text_parts: list[str] = []
    count = 0

    for seg in segments:
        count += 1
        text = seg.text.strip()
        if not text:
            continue
        text_parts.append(text)
        srt_lines += [
            str(count),
            f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}",
            text,
            "",
        ]
        print(f"  [{fmt_ts(seg.start)}] {text}")

    if not text_parts:
        raise SystemExit("没有识别到任何语音内容，请确认文件里有人声。")

    txt_path = out_dir / f"{stem}.txt"
    txt_path.write_text("\n".join(text_parts) + "\n", encoding="utf-8")
    print(f"\n纯文本：{txt_path}（{len(text_parts)} 句，{sum(len(t) for t in text_parts)} 字）")

    if not args.txt_only:
        srt_path = out_dir / f"{stem}.srt"
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        print(f"字幕文件：{srt_path}（可直接拖进剪映）")


if __name__ == "__main__":
    main()
