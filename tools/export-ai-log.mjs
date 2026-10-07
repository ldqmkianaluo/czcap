/**
 * export-ai-log.mjs —— 把 DSH 的 AI 对话会话导出为可读的 Prompt 快照
 *
 * 用途：赛事要求提交「AI 对话历史 Prompt 快照」。本机 DSH 的会话记录是
 *      zstd 压缩的 jsonl，这个脚本负责解压并转成人能读的 Markdown。
 *
 * 为什么用 Node 而不是 Python：本机 Node v24 内置 zlib 的 zstd 解压，
 * 不需要额外安装任何依赖。
 *
 * 用法：
 *   node tools/export-ai-log.mjs --list              列出所有会话
 *   node tools/export-ai-log.mjs --inspect           只看会话结构，不导出
 *   node tools/export-ai-log.mjs                     导出全部会话
 *   node tools/export-ai-log.mjs --full              额外保留 AI 的思考过程
 *   node tools/export-ai-log.mjs --session <关键字>   只导出匹配的会话
 *
 * 输出目录：<项目根>/ai-logs/
 */

import { readFileSync, writeFileSync, readdirSync, mkdirSync, statSync } from 'node:fs';
import { zstdDecompressSync } from 'node:zlib';
import { join, basename, dirname, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const has = (flag) => args.includes(flag);
const valueOf = (flag) => {
  const i = args.indexOf(flag);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : null;
};

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh');
const SESSIONS_DIR = join(DSH_HOME, 'sessions');

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]); // 帧魔数 0xFD2FB528 的小端序

/**
 * 解压多帧拼接的 zstd 数据。
 *
 * 必须自己切帧：Node 的 zstdDecompressSync 与 createZstdDecompress 都只解
 * 第一帧就停下。DSH 的会话文件是每次写入追加一帧拼成的（实测一个 2 MB 的
 * 文件里有 4400 帧），直接用现成 API 只能拿到开头 171 个字符。
 *
 * 切帧依据是帧魔数，但魔数也可能偶然出现在压缩数据内部，所以每段都试着
 * 解压：失败就把下一段并进来重试，直到成功。
 */
function decompressMultiFrame(buf) {
  const starts = [];
  for (let i = 0; i + 4 <= buf.length; i++) {
    if (buf.compare(ZSTD_MAGIC, 0, 4, i, i + 4) === 0) starts.push(i);
  }
  if (!starts.length) return zstdDecompressSync(buf);
  if (starts[0] !== 0) starts.unshift(0);

  const pieces = [];
  let i = 0;
  while (i < starts.length) {
    let j = i + 1;
    let ok = false;
    while (j <= starts.length) {
      const end = j < starts.length ? starts[j] : buf.length;
      try {
        pieces.push(zstdDecompressSync(buf.subarray(starts[i], end)));
        ok = true;
        break;
      } catch {
        j++; // 这个边界是误判，把下一段合并进来重试
      }
    }
    if (!ok) break; // 末尾可能是正在写入的残帧，放弃剩余部分
    i = j;
  }
  return Buffer.concat(pieces);
}

function findSessions(dir) {
  const out = [];
  const walk = (d) => {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.jsonl.zstd')) {
        const st = statSync(p);
        out.push({ path: p, size: st.size, mtime: st.mtime });
      }
    }
  };
  walk(dir);
  return out;
}

function loadSession(path) {
  const buf = readFileSync(path);
  let text;
  try {
    text = decompressMultiFrame(buf).toString('utf8');
  } catch (err) {
    return { records: [], error: err.message, rawLength: 0, parseFailures: 0 };
  }
  const records = [];
  let parseFailures = 0;
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try {
      records.push(JSON.parse(t));
    } catch {
      parseFailures++;
    }
  }
  return { records, error: null, rawLength: text.length, parseFailures };
}

/** 从 content 块数组里挑出指定类型的文本 */
function blocksToText(content, kinds) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((b) => b && kinds.includes(b.type))
    .map((b) => b.text || '')
    .filter(Boolean)
    .join('\n\n');
}

function truncate(s, n) {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n) + ` …（已截断，原文 ${s.length} 字符）` : s;
}

/**
 * 客户端自动注入的上下文片段。
 *
 * DSH 会在每轮对话前注入一段运行时上下文（文件策略、审批策略等），它们在
 * 会话记录里同样是 user/message，但并不是用户真正输入的内容。不滤掉的话，
 * 快照里会夹进大量与项目无关的噪音，评审读起来会以为是学生写的。
 */
const INJECTED_MARKERS = [
  'Current runtime context.',
  'This snapshot supersedes earlier runtime-context snapshots.',
];

function isInjectedContext(text) {
  const t = text.trim();
  return INJECTED_MARKERS.some((m) => t.startsWith(m) || t.includes(m));
}

/** 把一条记录渲染成 {who, body}；返回 null 表示这条不导出 */
function renderRecord(r, opts) {
  const d = r.data || {};
  switch (r.type) {
    case 'user/message': {
      const body = blocksToText(d.content, ['text']);
      if (!body.trim()) return null;
      if (isInjectedContext(body)) return null;
      return { who: '用户', body: body.trim() };
    }
    case 'assistant/message': {
      const content = d.message?.content;
      const parts = [];
      const text = blocksToText(content, ['text']);
      if (text.trim()) parts.push(text.trim());
      if (opts.full) {
        const reasoning = blocksToText(content, ['reasoning']);
        if (reasoning.trim()) {
          parts.push('<!-- 以下是 AI 的思考过程，加 --full 才会导出 -->\n\n' + reasoning.trim());
        }
      }
      if (!parts.length) return null;
      return { who: 'AI', body: parts.join('\n\n') };
    }
    case 'tool/call': {
      let a = d.arguments || '';
      try {
        a = JSON.stringify(JSON.parse(a), null, 0);
      } catch {
        /* 保持原样 */
      }
      return { who: '工具调用', body: `\`${d.name}\` ${truncate(a, opts.toolArgs)}` };
    }
    case 'tool/result': {
      const content = d.message?.content;
      let text = '';
      if (Array.isArray(content)) {
        for (const c of content) {
          if (c && c.type === 'tool-result') text += blocksToText(c.content, ['text']) + '\n';
        }
      }
      text = text.trim();
      if (!text) return null;
      return { who: '工具结果', body: '```\n' + truncate(text, opts.toolResult) + '\n```' };
    }
    default:
      return null;
  }
}

// --------------------------------------------------------------------------

const sessions = findSessions(SESSIONS_DIR).sort((a, b) => b.mtime - a.mtime);

if (!sessions.length) {
  console.log(`没在 ${SESSIONS_DIR} 找到任何会话文件`);
  process.exit(0);
}

if (has('--list')) {
  console.log(`会话目录：${SESSIONS_DIR}\n`);
  console.log('序号  大小(KB)   修改时间              会话目录');
  sessions.forEach((s, i) => {
    console.log(
      `${String(i + 1).padStart(3)}  ${String(Math.round(s.size / 1024)).padStart(8)}   ` +
        `${s.mtime.toISOString().replace('T', ' ').slice(0, 19)}   ${basename(dirname(s.path))}`
    );
  });
  process.exit(0);
}

if (has('--inspect')) {
  const target = sessions[0];
  console.log(`检查会话：${target.path}`);
  const { records, error, rawLength, parseFailures } = loadSession(target.path);
  if (error) {
    console.log(`解压失败（可能正在写入）：${error}`);
    process.exit(0);
  }
  console.log(`压缩后：${(target.size / 1024).toFixed(1)} KB`);
  console.log(`解压后：${(rawLength / 1024).toFixed(1)} KB`);
  console.log(`记录数：${records.length}，解析失败：${parseFailures}\n`);
  const kinds = new Map();
  for (const r of records) kinds.set(r.type, (kinds.get(r.type) || 0) + 1);
  console.log('记录类型分布：');
  for (const [k, n] of [...kinds].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(5)}  ${k}`);
  }
  process.exit(0);
}

// ---------------- 正式导出 ----------------

const outDir = resolve(PROJECT_ROOT, valueOf('--out') || 'ai-logs');
const only = valueOf('--session');
mkdirSync(outDir, { recursive: true });

const opts = {
  full: has('--full'),
  toolArgs: 400,
  toolResult: 600,
};

let exported = 0;
let skipped = 0;

for (const s of sessions) {
  if (only && !s.path.includes(only)) continue;
  const { records, error } = loadSession(s.path);
  if (error || !records.length) {
    skipped++;
    continue;
  }

  const lines = [];
  let userCount = 0;
  let aiCount = 0;
  let toolCount = 0;

  for (const r of records) {
    const rendered = renderRecord(r, opts);
    if (!rendered) continue;
    if (rendered.who === '用户') userCount++;
    else if (rendered.who === 'AI') aiCount++;
    else toolCount++;

    lines.push(`### ${rendered.who}`);
    lines.push('');
    lines.push(rendered.body);
    lines.push('');
  }

  if (!userCount) {
    skipped++; // 没有用户提问的会话（例如纯内部调用）不导出
    continue;
  }

  const sessionId = basename(dirname(s.path));
  // 只用时间戳取名会撞车：同秒结束的会话会互相覆盖，所以补上会话 ID 前缀
  const shortId = sessionId.replace(/^session-/, '').slice(0, 8);
  const stamp = s.mtime.toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const header = [
    `# AI 协同对话记录（Prompt 快照）`,
    '',
    `- 会话 ID：\`${sessionId}\``,
    `- 记录时间：${s.mtime.toISOString().replace('T', ' ').slice(0, 19)}`,
    `- 工作目录：\`D:\\code\``,
    `- 内容统计：用户提问 ${userCount} 条，AI 回答 ${aiCount} 条，工具调用与结果 ${toolCount} 条`,
    `- 导出方式：\`node tools/export-ai-log.mjs${opts.full ? ' --full' : ''}\``,
    '',
    '> 本文件由脚本自动导出，内容未经人工删改。',
    opts.full ? '' : '> AI 的思考过程（reasoning）默认不导出，加 `--full` 可保留。',
    '',
    '---',
    '',
  ];

  const outPath = join(outDir, `prompt-snapshot-${stamp}-${shortId}.md`);
  writeFileSync(outPath, header.concat(lines).join('\n'), 'utf8');
  exported++;
  console.log(`已导出 ${basename(outPath)}  （用户 ${userCount} / AI ${aiCount} / 工具 ${toolCount}）`);
}

console.log(`\n完成：导出 ${exported} 个，跳过 ${skipped} 个（正在写入、为空或无用户提问）`);
console.log(`输出目录：${outDir}`);

if (exported) {
  console.log('\n⚠ 提交前必须人工通读一遍：');
  console.log('  1. 仓库是公开的 —— 确认没有 API Key、密码、个人邮箱、真实姓名等敏感信息');
  console.log('  2. 确认没有不应公开的内部路径或第三方机密内容');
  console.log('  3. 确认对话确实体现了 AI 协同开发过程（这正是赛事要看的）');
}
