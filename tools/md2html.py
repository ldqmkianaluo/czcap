"""
md2html.py —— 把 Markdown 转成打印就绪的 HTML（用于导出 PDF）

为什么是 HTML 而不是直接出 PDF：本机没有 pandoc / LibreOffice / weasyprint，
而浏览器自带的「打印 → 另存为 PDF」是唯一稳定、无依赖、样式保真的路径。
生成的 HTML 已内置 A4 分页与打印样式，打开后直接 Ctrl+P 即可。

支持的 Markdown 子集：
    标题 # ~ ####、段落、无序/有序列表、表格、代码块、引用块、分隔线、
    **粗体**、`行内代码`

用法：
    python tools/md2html.py docs/技术文档.md
    python tools/md2html.py docs/技术文档.md -o docs/技术文档.html
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

CSS = """
@page { size: A4; margin: 20mm 18mm; }
body {
  font-family: "宋体", SimSun, serif; font-size: 10.5pt; line-height: 1.65;
  color: #111; max-width: 100%; margin: 0;
}
h1 { font-family: "黑体", SimHei, sans-serif; font-size: 21pt; text-align: center;
     margin: 0 0 20pt 0; page-break-after: avoid; }
h2 { font-family: "黑体", SimHei, sans-serif; font-size: 14.5pt; margin: 20pt 0 9pt 0;
     padding-bottom: 3pt; border-bottom: 0.6pt solid #bbb; page-break-after: avoid; }
h3 { font-family: "黑体", SimHei, sans-serif; font-size: 12pt; margin: 14pt 0 6pt 0;
     page-break-after: avoid; }
h4 { font-family: "黑体", SimHei, sans-serif; font-size: 11pt; margin: 11pt 0 5pt 0;
     page-break-after: avoid; }
p { margin: 0 0 7pt 0; text-align: justify; }
ul, ol { margin: 0 0 9pt 0; padding-left: 22pt; }
li { margin: 0 0 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 7pt 0 11pt 0; page-break-inside: avoid; }
th, td { border: 0.6pt solid #666; padding: 3.5pt 6pt; font-size: 9.5pt; vertical-align: top; }
th { background: #e9e9e9; font-family: "黑体", SimHei, sans-serif; }
code { font-family: Consolas, "Courier New", monospace; background: #f2f2f2; font-size: 9.5pt;
       padding: 0 2pt; }
pre { font-family: Consolas, "Courier New", monospace; background: #f7f7f7;
      border-left: 2.5pt solid #999; padding: 7pt 10pt; margin: 7pt 0 11pt 0;
      font-size: 9pt; line-height: 1.45; white-space: pre-wrap; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 9pt; }
blockquote { background: #f7f7f7; border-left: 2.5pt solid #999; padding: 6pt 10pt;
             margin: 7pt 0 11pt 0; font-size: 10pt; }
hr { border: none; border-top: 0.6pt solid #bbb; margin: 12pt 0; }
@media print { body { margin: 0; } }
"""


def inline(text: str) -> str:
    """行内语法：先转义，再处理粗体与行内代码。"""
    s = html.escape(text)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def convert(lines: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        s = raw.strip()

        if not s:
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}", s):
            out.append("<hr/>")
            i += 1
            continue

        # 代码块
        if s.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1  # 跳过结束的 ```
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 表格
        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append("<table>")
            first = True
            for row in rows:
                if re.fullmatch(r"\|[\s:\-|]+\|", row):
                    continue
                cells = [c.strip() for c in row.strip("|").split("|")]
                tag = "th" if first else "td"
                out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
                first = False
            out.append("</table>")
            continue

        # 引用
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br/>".join(buf) + "</blockquote>")
            continue

        # 无序列表
        if re.match(r"^[-*]\s+", s):
            buf = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                buf.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ul>")
            continue

        # 有序列表
        if re.match(r"^\d+\.\s+", s):
            buf = []
            while i < n and re.match(r"^\d+\.\s+", lines[i].strip()):
                buf.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ol>")
            continue

        out.append(f"<p>{inline(s)}</p>")
        i += 1

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Markdown -> 打印就绪的 HTML")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()

    src = args.input
    if not src.exists():
        raise SystemExit(f"找不到输入文件：{src}")

    dst = args.output or src.with_suffix(".html")
    text = src.read_text(encoding="utf-8")
    body = convert(text.splitlines())

    doc = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/>\n"
        f"<title>{html.escape(src.stem)}</title>\n<style>{CSS}</style></head>\n"
        f"<body>\n{body}\n</body></html>\n"
    )
    dst.write_text(doc, encoding="utf-8")

    print(f"已生成：{dst}（{dst.stat().st_size / 1024:.1f} KB）")
    print()
    print("导出 PDF：")
    print(f"  1. 用浏览器打开 {dst.name}")
    print("  2. Ctrl+P -> 目标选「另存为 PDF」-> 边距选「默认」-> 保存")
    print("  页面样式已按 A4 调好，不需要在打印对话框里额外调整。")


if __name__ == "__main__":
    main()
