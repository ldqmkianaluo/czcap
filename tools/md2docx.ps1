<#
  md2docx.ps1 —— 把 Markdown 转成 .docx

  原理：Markdown -> 带 CSS 的 HTML -> 用 Word COM 打开并另存为 .docx
        （Word 对 HTML 的样式解析保真度远高于手工拼 WordprocessingML）

  用法：
    pwsh -File md2docx.ps1 -InPath "输入.md" -OutPath "输出.docx"

  支持的 Markdown 子集：
    # / ## / ### / ####  标题
    段落、- 无序列表、1. 有序列表、> 引用、--- 分隔线
    | 表格 |
    **粗体**、`行内代码`
#>

param(
    [Parameter(Mandatory = $true)][string]$InPath,
    [Parameter(Mandatory = $true)][string]$OutPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InPath)) { throw "输入文件不存在: $InPath" }
$InPath  = (Resolve-Path -LiteralPath $InPath).Path
$OutPath = [System.IO.Path]::GetFullPath($OutPath)
$outDir  = [System.IO.Path]::GetDirectoryName($OutPath)
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

function ConvertTo-HtmlEscaped([string]$s) {
    if ($null -eq $s) { return '' }
    $s = $s -replace '&', '&amp;'
    $s = $s -replace '<', '&lt;'
    $s = $s -replace '>', '&gt;'
    return $s
}

function ConvertTo-InlineHtml([string]$s) {
    $s = ConvertTo-HtmlEscaped $s
    # 粗体
    $s = [regex]::Replace($s, '\*\*(.+?)\*\*', '<b>$1</b>')
    # 行内代码
    $s = [regex]::Replace($s, '`([^`]+)`', '<span class="code">$1</span>')
    return $s
}

$lines = Get-Content -LiteralPath $InPath -Encoding UTF8
$body  = [System.Collections.Generic.List[string]]::new()

$i = 0
$n = $lines.Count
while ($i -lt $n) {
    $line = $lines[$i]
    $trim = $line.Trim()

    # ---- 空行 ----
    if ($trim -eq '') { $i++; continue }

    # ---- 分隔线 ----
    if ($trim -match '^-{3,}$' -or $trim -match '^\*{3,}$') {
        $body.Add('<hr/>'); $i++; continue
    }

    # ---- 标题 ----
    if ($trim -match '^(#{1,6})\s+(.*)$') {
        $level = $Matches[1].Length
        $text  = ConvertTo-InlineHtml $Matches[2]
        $body.Add("<h$level>$text</h$level>")
        $i++; continue
    }

    # ---- 表格 ----
    if ($trim.StartsWith('|')) {
        $rows = [System.Collections.Generic.List[string]]::new()
        while ($i -lt $n -and $lines[$i].Trim().StartsWith('|')) {
            $rows.Add($lines[$i].Trim()); $i++
        }
        $body.Add('<table>')
        $isFirst = $true
        foreach ($row in $rows) {
            # 跳过分隔行 |---|---|
            if ($row -match '^\|[\s:\-\|]+\|$') { continue }
            $cells = $row.Trim('|') -split '\|'
            $tag = if ($isFirst) { 'th' } else { 'td' }
            $html = '<tr>'
            foreach ($c in $cells) {
                $html += "<$tag>" + (ConvertTo-InlineHtml $c.Trim()) + "</$tag>"
            }
            $html += '</tr>'
            $body.Add($html)
            $isFirst = $false
        }
        $body.Add('</table>')
        continue
    }

    # ---- 引用块（连续 > 行合并为一个块）----
    if ($trim.StartsWith('>')) {
        $quote = [System.Collections.Generic.List[string]]::new()
        while ($i -lt $n -and $lines[$i].Trim().StartsWith('>')) {
            $quote.Add((ConvertTo-InlineHtml ($lines[$i].Trim().TrimStart('>').Trim())))
            $i++
        }
        $body.Add('<div class="quote">' + ($quote -join '<br/>') + '</div>')
        continue
    }

    # ---- 列表 ----
    if ($trim -match '^[-*]\s+(.*)$') {
        $items = [System.Collections.Generic.List[string]]::new()
        while ($i -lt $n -and $lines[$i].Trim() -match '^[-*]\s+(.*)$') {
            $items.Add((ConvertTo-InlineHtml $Matches[1]))
            $i++
        }
        $body.Add('<ul>' + (($items | ForEach-Object { "<li>$_</li>" }) -join '') + '</ul>')
        continue
    }

    if ($trim -match '^\d+\.\s+(.*)$') {
        $items = [System.Collections.Generic.List[string]]::new()
        while ($i -lt $n -and $lines[$i].Trim() -match '^\d+\.\s+(.*)$') {
            $items.Add((ConvertTo-InlineHtml $Matches[1]))
            $i++
        }
        $body.Add('<ol>' + (($items | ForEach-Object { "<li>$_</li>" }) -join '') + '</ol>')
        continue
    }

    # ---- 普通段落 ----
    $body.Add('<p>' + (ConvertTo-InlineHtml $trim) + '</p>')
    $i++
}

$css = @'
<style>
body { font-family: "宋体", SimSun, serif; font-size: 10.5pt; line-height: 1.55; }
h1 { font-family: "黑体", SimHei, sans-serif; font-size: 18pt; text-align: center; margin: 0 0 14pt 0; }
h2 { font-family: "黑体", SimHei, sans-serif; font-size: 14pt; margin: 16pt 0 8pt 0; }
h3 { font-family: "黑体", SimHei, sans-serif; font-size: 12pt; margin: 12pt 0 6pt 0; }
h4 { font-family: "黑体", SimHei, sans-serif; font-size: 11pt; margin: 10pt 0 4pt 0; }
p  { margin: 0 0 6pt 0; text-align: justify; }
ul, ol { margin: 0 0 8pt 0; }
li { margin: 0 0 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt 0; }
th, td { border: 1px solid #000000; padding: 3pt 5pt; font-size: 9.5pt; vertical-align: top; }
th { background-color: #E8E8E8; font-family: "黑体", SimHei, sans-serif; font-weight: bold; }
.code { font-family: Consolas, "Courier New", monospace; background-color: #F2F2F2; }
.quote { background-color: #F7F7F7; border-left: 3pt solid #999999; padding: 6pt 9pt; margin: 6pt 0 10pt 0; font-size: 10pt; }
hr { border: none; border-top: 1pt solid #BBBBBB; margin: 10pt 0; }
</style>
'@

$html = @"
<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>申报书</title>$css</head>
<body>
$($body -join "`r`n")
</body></html>
"@

$htmlPath = [System.IO.Path]::ChangeExtension($OutPath, '.html')
[System.IO.File]::WriteAllText($htmlPath, $html, (New-Object System.Text.UTF8Encoding($true)))
Write-Output "HTML 已生成: $htmlPath ($((Get-Item $htmlPath).Length) bytes)"

# ---- Word COM 转换 ----
$word = $null
$doc  = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    try { $word.Options.ConfirmConversions = $false } catch { }

    # Open(FileName, ConfirmConversions, ReadOnly)
    $doc = $word.Documents.Open($htmlPath, $false, $false)

    # 16 = wdFormatDocumentDefault (.docx)
    try {
        $doc.SaveAs2($OutPath, 16)
    } catch {
        Write-Output "SaveAs2 失败，回退 SaveAs: $($_.Exception.Message)"
        $doc.SaveAs($OutPath, 16)
    }
    Write-Output "DOCX 已生成: $OutPath ($((Get-Item $OutPath).Length) bytes)"
}
catch {
    Write-Output "WORD_CONVERSION_FAILED: $($_.Exception.Message)"
    exit 2
}
finally {
    if ($doc)  { try { $doc.Close(0) } catch { } }
    if ($word) { try { $word.Quit() } catch { } }
    if ($doc)  { try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($doc) } catch { } }
    if ($word) { try { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) } catch { } }
}
