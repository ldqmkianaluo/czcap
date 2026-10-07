"""
HaluLens —— 多模态幻觉黑盒检测：评测与探针骨架 v0.1

设计目标：W1 结束时能跑出 B0 基线（原始 VLM 直接回答）在 POPE 与
MMHal-Bench 上的数字，并统计 API 调用成本。

依赖：
    pip install openai pillow

环境变量：
    VLM_API_KEY      被检测模型的 API Key
    VLM_BASE_URL     OpenAI 兼容端点（默认官方）
    JUDGE_API_KEY    评判模型的 Key（可选，MMHal-Bench 需要）

说明：本文件是骨架。标注 TODO 的位置需要按各 benchmark 官方仓库的
真实字段调整。不要直接当成成品使用。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from PIL import Image

# 探针参数的 prompt 版本号。任何 prompt 改动都必须提升版本号，
# 否则不同日期的实验结果无法区分是方法变了还是 prompt 变了。
PROMPT_VERSION = "v0.1.0"

# 项目根目录（code/ 的上一级）。默认的输出与缓存路径都以它为基准，而不是
# 以当前工作目录为基准——否则从不同目录运行会把结果散落到各处。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 被 max_tokens 截断时的扩容上限。
#
# 推理模型偶尔会把整个 token 预算烧在思维链上，一个字答案都不输出。
# 实测：27 次问答里出现 2 次，特征是 finish_reason=length 且内容为空字符串。
# 遇到这种情况就放大预算重试一次，而不是直接加大默认值——默认值加大会让
# 每一次调用都变贵，而这类失控只是少数。
TRUNCATION_MAX_TOKENS = 4096


# --------------------------------------------------------------------------
# 运行记录：每次实验必须留下可复现的元信息
# --------------------------------------------------------------------------

@dataclass
class RunMeta:
    """随结果一起存档的运行元信息。缺少这些字段的实验，三个月后无法复现。"""

    run_id: str
    started_at: str
    model: str
    model_reported_version: str          # 从 API 响应里读到的实际版本，可能被静默更新
    prompt_version: str
    gen_params: dict[str, Any]
    probe_params: dict[str, Any]
    code_commit: str = "unknown"
    note: str = ""

    @staticmethod
    def new(model: str, gen_params: dict, probe_params: dict, note: str = "") -> "RunMeta":
        ts = datetime.now(timezone.utc)
        return RunMeta(
            run_id=ts.strftime("%Y%m%dT%H%M%SZ"),
            started_at=ts.isoformat(),
            model=model,
            model_reported_version="",
            prompt_version=PROMPT_VERSION,
            gen_params=gen_params,
            probe_params=probe_params,
            code_commit=os.environ.get("GIT_COMMIT", "unknown"),
            note=note,
        )


class ResultLogger:
    """JSONL 结果日志 + 成本累计。"""

    def __init__(self, out_dir: Path, meta: RunMeta):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.meta = meta
        self.path = out_dir / f"{meta.run_id}_{meta.model.replace('/', '_')}.jsonl"
        self.fh = self.path.open("w", encoding="utf-8")
        self.fh.write(json.dumps({"__meta__": asdict(meta)}, ensure_ascii=False) + "\n")
        self.n_calls = 0
        self.cached_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def log(self, record: dict) -> None:
        self.fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.fh.flush()

    def close(self) -> None:
        summary = {
            "__summary__": {
                "api_calls": self.n_calls,
                "cached_calls": self.cached_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_hit_rate": (
                    self.cached_calls / (self.n_calls + self.cached_calls)
                    if (self.n_calls + self.cached_calls) else 0.0
                ),
                # 服务商可能静默更新模型，实际版本号以响应为准并写入汇总
                "model_reported_version": self.meta.model_reported_version,
            }
        }
        self.fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
        self.fh.close()
        print(f"\n[结果] {self.path}")
        print(
            f"[成本] 实际调用 {self.n_calls} 次，缓存命中 {self.cached_calls} 次，"
            f"输入 {self.input_tokens} tok，输出 {self.output_tokens} tok"
        )


# --------------------------------------------------------------------------
# yes/no 解析：黑盒方法的地基
#
# 这一层解析错了，后面所有指标都是错的。所以它必须既能容错，又能如实
# 报告"解析不了"，而不是硬猜一个答案。
# --------------------------------------------------------------------------

_POSITIVE_PATTERNS = (r"\byes\b", r"\btrue\b", r"\bcorrect\b", "是的", "是", "存在", "有")
_NEGATIVE_PATTERNS = (r"\bno\b", r"\bfalse\b", r"\bincorrect\b", "不是", "没有", "不存在", "否", "无")

# 明确要求只回答 Yes 或 No。
#
# 实测发现模型会自由发挥成整句描述（例如"Looking at the image, I can identify
# only two geometric shapes: ..."），从源头约束输出格式，比事后猜它什么意思
# 可靠得多。解析器仍然保留作兜底。
YESNO_SUFFIX = " Answer with exactly one word: Yes or No."


def _earliest_decision(low: str) -> bool | None:
    """
    取"最早出现的判定词"决定结果。

    这样"没有猫"里的"没有"（位置 0）会先于"有"（位置 1）被匹配到，
    得到正确的否定判定；而用简单的 in 判断就会出错。
    """
    best: tuple[int, bool] | None = None
    for patterns, verdict in ((_POSITIVE_PATTERNS, True), (_NEGATIVE_PATTERNS, False)):
        for pat in patterns:
            m = re.search(pat, low)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), verdict)
    return None if best is None else best[1]


def _parse_yesno(text: str) -> bool | None:
    """
    把模型输出解析成 True / False / None。

    优先只看最后一行——推理模型会把思维链写在前面，结论放在末尾，
    让思维链里的"是"参与判定会产生系统性错判。最后一行解析不出时
    才回退到全文。

    返回 None 表示解析不了。不要静默当成 False：解析失败率本身就是
    需要报告的指标。
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = ([lines[-1]] if lines else []) + [text]
    for cand in candidates:
        verdict = _earliest_decision(cand.lower())
        if verdict is not None:
            return verdict
    return None


# --------------------------------------------------------------------------
# VLM 客户端：缓存 + 重试 + 并发 + 成本统计
# --------------------------------------------------------------------------

class VLMClient:
    """
    所有探针共用同一个客户端，保证被检测模型、版本、采样参数完全一致。

    缓存键包含模型名、prompt 版本、图像内容哈希、问题文本与采样参数——
    任何一项变化都会产生新的缓存条目，避免复用过期结果。
    """

    def __init__(
        self,
        model: str,
        logger: ResultLogger,
        cache_dir: Path,
        concurrency: int = 4,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_tokens: int = 128,
    ):
        self.model = model
        self.logger = logger
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=os.environ.get("VLM_API_KEY"),
                base_url=os.environ.get("VLM_BASE_URL") or None,
            )
        return self._client

    # ---------------- 缓存 ----------------

    def _cache_key(
        self, image_bytes: bytes, question: str, temperature: float, max_tokens: int
    ) -> str:
        h = hashlib.sha256()
        for part in (
            self.model,
            PROMPT_VERSION,
            hashlib.sha256(image_bytes).hexdigest(),
            question,
            f"T={temperature}",
            f"max_tokens={max_tokens}",
        ):
            h.update(part.encode("utf-8"))
        return h.hexdigest()

    def _cache_read(self, key: str) -> dict | None:
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def _cache_write(self, key: str, payload: dict) -> None:
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    # ---------------- 调用 ----------------

    async def ask(
        self,
        image: Image.Image,
        question: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        _allow_grow: bool = True,
    ) -> dict:
        """返回 {'text', 'input_tokens', 'output_tokens', 'cached', 'finish_reason'}"""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens
        image_bytes = image_to_bytes(image)
        key = self._cache_key(image_bytes, question, temperature, max_tokens)

        async def _finalize(payload: dict, cached: bool) -> dict:
            """
            收尾：发现被截断就扩容重试一次。

            缓存命中也要走这条路径，否则重跑时会直接复用那份被截断的结果，
            重试机制就等于失效了。
            """
            if (
                payload.get("finish_reason") == "length"
                and _allow_grow
                and max_tokens < TRUNCATION_MAX_TOKENS
            ):
                grown = min(max_tokens * 4, TRUNCATION_MAX_TOKENS)
                self.logger.log(
                    {
                        "task": "vlm_truncation_grow",
                        "question": question,
                        "from_max_tokens": max_tokens,
                        "to_max_tokens": grown,
                    }
                )
                return await self.ask(
                    image, question, temperature, max_tokens=grown, _allow_grow=False
                )
            return {**payload, "cached": cached}

        hit = self._cache_read(key)
        if hit is not None:
            self.logger.cached_calls += 1
            return await _finalize(hit, True)

        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": question},
                ],
            }
        ]

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                async with self.sem:
                    resp = await self._get_client().chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                usage = getattr(resp, "usage", None)
                choice = resp.choices[0]
                payload = {
                    "text": (choice.message.content or "").strip(),
                    # finish_reason 必须记录：空回复到底是"模型什么都没说"还是
                    # "被 max_tokens 截断了"，只有这个字段能区分。
                    # 'length' = 被截断，'stop' = 正常结束。
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "model_reported": getattr(resp, "model", self.model),
                }
                self.logger.n_calls += 1
                self.logger.input_tokens += payload["input_tokens"]
                self.logger.output_tokens += payload["output_tokens"]
                self.logger.meta.model_reported_version = payload["model_reported"]
                self._cache_write(key, payload)
                return await _finalize(payload, False)
            except Exception as exc:  # noqa: BLE001
                # 4xx 多为配置问题（密钥错、模型名错、参数不合法），重试没有意义，
                # 应立即报错，否则白白等上几轮退避。429 是限流，仍然重试。
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and 400 <= status < 500 and status != 429:
                    raise RuntimeError(
                        f"请求被拒绝（HTTP {status}），不重试。"
                        f"请检查 VLM_API_KEY、VLM_BASE_URL 与 --model 是否正确。"
                        f"原始错误：{exc}"
                    ) from exc
                last_err = exc
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"调用失败（已重试 {self.max_retries} 次）：{last_err}")

    async def ask_yesno(
        self,
        image: Image.Image,
        question: str,
        temperature: float | None = None,
        force_format: bool = True,
    ) -> bool | None:
        """
        强制二值输出，并记录原始输出。

        两件事同时做，互为保险：
        1. 在问题里明确要求只回答 Yes 或 No —— 让模型输出干净，从源头减少
           解析失败；
        2. 用容错解析器兜底 —— 即使模型没听指令，也不至于直接判失败。

        原始输出必须落盘：出现解析失败时，要能回看模型究竟说了什么，
        否则 unparseable 只是一个无法诊断的数字。
        """
        asked = question if (not force_format or YESNO_SUFFIX in question) else question + YESNO_SUFFIX
        resp = await self.ask(image, asked, temperature)
        text = resp["text"]
        verdict = _parse_yesno(text)
        self.logger.log(
            {
                "task": "vlm_yesno",
                "cached": resp.get("cached", False),
                "question": question,
                "asked": asked,
                "verdict": verdict,
                "finish_reason": resp.get("finish_reason"),
                "raw": text,
            }
        )
        return verdict


# --------------------------------------------------------------------------
# 图像工具
# --------------------------------------------------------------------------

def image_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format=fmt, quality=92)
    return buf.getvalue()


def occlude_grid(img: Image.Image, row: int, col: int, grid: int, fill: int = 128) -> Image.Image:
    """
    遮蔽第 (row, col) 格。用中性灰填充而非黑色，避免引入"大面积黑色物体"
    这一额外语义干扰——这是遮蔽类方法常见的实现陷阱。
    """
    out = img.convert("RGB").copy()
    w, h = out.size
    cw, ch = w / grid, h / grid
    box = (int(col * cw), int(row * ch), int((col + 1) * cw), int((row + 1) * ch))
    out.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), (fill, fill, fill)), box[:2])
    return out


# 与问题语义正交的图像变换。注意：若问题涉及颜色，亮度变换不满足正交性，
# 必须按问题类型筛选可用变换——这是本方法已知的设计难点。
PERTURBATIONS: dict[str, Callable[[Image.Image], Image.Image]] = {
    "jpeg_recompress": lambda im: _recompress(im, quality=55),
    "crop_5pct": lambda im: _crop_margin(im, 0.05),
    "scale_90": lambda im: im.resize((max(1, int(im.width * 0.9)), max(1, int(im.height * 0.9)))),
    "flip_none": lambda im: im.copy(),  # 占位：用于测试变换管线本身
}

MISLEADING_FOR_COLOR_QUESTIONS = {"brightness", "contrast"}


def _recompress(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _crop_margin(img: Image.Image, ratio: float) -> Image.Image:
    w, h = img.size
    dx, dy = int(w * ratio), int(h * ratio)
    return img.crop((dx, dy, w - dx, h - dy))


# --------------------------------------------------------------------------
# 三个黑盒探针
# --------------------------------------------------------------------------

@dataclass
class ConsistencyResult:
    """一致性类探针（A / B）的输出。"""

    score: float                     # 1.0 = 完全一致，0.0 = 完全发散
    n_variants: int
    distribution: dict[str, int] = field(default_factory=dict)
    unparseable: int = 0

    def as_record(self) -> dict:
        return {
            "score": round(self.score, 4),
            "n_variants": self.n_variants,
            "distribution": self.distribution,
            "unparseable": self.unparseable,
        }


def normalized_consistency(answers: Sequence[bool | None]) -> ConsistencyResult:
    """
    一致性 = 1 - 归一化熵。

    用熵而不是"多数票占比"，是为了反映"接近边界"的灰色情况：
    4:1 与 3:2 的多数票占比都算多数，但不确定性完全不同。
    """
    valid = [a for a in answers if a is not None]
    unparseable = len(answers) - len(valid)
    if not valid:
        return ConsistencyResult(score=0.0, n_variants=len(answers), unparseable=unparseable)

    counts = {True: valid.count(True), False: valid.count(False)}
    n = len(valid)
    entropy = 0.0
    for c in counts.values():
        if c:
            p = c / n
            entropy -= p * math.log2(p)
    max_entropy = 1.0 if n > 0 else 1.0  # 二值时最大熵为 log2(2) = 1
    score = 1.0 - (entropy / max_entropy if max_entropy else 0.0)
    return ConsistencyResult(
        score=score,
        n_variants=len(answers),
        distribution={"yes": counts[True], "no": counts[False]},
        unparseable=unparseable,
    )


async def probe_paraphrase(
    client: VLMClient,
    image: Image.Image,
    question: str,
    variants: Sequence[str],
) -> ConsistencyResult:
    """
    探针 A：提问改写一致性。

    variants 由外部提供（人工撰写或由 LLM 生成后人工抽检）。
    不要用 LLM 生成改写后直接使用——语义漂移会污染这一信号。
    """
    tasks = [client.ask_yesno(image, v, temperature=0.7) for v in variants]
    return normalized_consistency(await asyncio.gather(*tasks))


async def probe_perturbation(
    client: VLMClient,
    image: Image.Image,
    question: str,
    transforms: Sequence[str],
) -> ConsistencyResult:
    """探针 B：图像扰动一致性。"""
    tasks = []
    for name in transforms:
        fn = PERTURBATIONS.get(name)
        if fn is None:
            continue
        tasks.append(client.ask_yesno(fn(image), question, temperature=0.0))
    return normalized_consistency(await asyncio.gather(*tasks))


@dataclass
class AttributionResult:
    """
    探针 C：遮蔽敏感性归因的输出。

    influential_cells 为空 = 不存在任何遮蔽能让答案翻转 = 该陈述无视觉依据。
    influential_cells 非空 = 得到依赖区域，可据此判定"视觉误读"并高亮。
    """

    baseline: bool | None
    influential_cells: list[tuple[int, int]]
    cells_tested: int
    grid: int

    @property
    def has_visual_grounding(self) -> bool:
        return len(self.influential_cells) > 0

    def as_record(self) -> dict:
        return {
            "baseline": self.baseline,
            "influential_cells": [list(c) for c in self.influential_cells],
            "cells_tested": self.cells_tested,
            "grid": self.grid,
            "has_visual_grounding": self.has_visual_grounding,
        }


async def probe_occlusion(
    client: VLMClient,
    image: Image.Image,
    question: str,
    grid: int = 3,
) -> AttributionResult:
    """
    探针 C：遮蔽敏感性归因。

    注意：本探针的调用次数 = grid^2，是三个探针中最昂贵的，
    必须由级联剪枝控制其触发范围。
    """
    baseline = await client.ask_yesno(image, question, temperature=0.0)
    cells = [(r, c) for r in range(grid) for c in range(grid)]
    answers = await asyncio.gather(
        *[client.ask_yesno(occlude_grid(image, r, c, grid), question, temperature=0.0) for r, c in cells]
    )
    influential = [
        cell
        for cell, ans in zip(cells, answers)
        if ans is not None and baseline is not None and ans != baseline
    ]
    return AttributionResult(
        baseline=baseline,
        influential_cells=influential,
        cells_tested=len(cells),
        grid=grid,
    )


# --------------------------------------------------------------------------
# 级联调度：成本递增 + 早期剪枝
# --------------------------------------------------------------------------

@dataclass
class CascadeConfig:
    paraphrase_threshold: float = 0.85   # 低于此值进入 L2
    perturbation_threshold: float = 0.75  # 低于此值进入 L3
    paraphrase_variants: Sequence[str] = ()
    perturbation_transforms: Sequence[str] = ("jpeg_recompress", "crop_5pct", "scale_90")
    coarse_grid: int = 3
    use_l3: bool = True


@dataclass
class StatementVerdict:
    statement: str
    label: str                # "grounded" | "misperception" | "ungrounded" | "uncertain"
    paraphrase: ConsistencyResult | None = None
    perturbation: ConsistencyResult | None = None
    attribution: AttributionResult | None = None

    def as_record(self) -> dict:
        return {
            "statement": self.statement,
            "label": self.label,
            "paraphrase": self.paraphrase.as_record() if self.paraphrase else None,
            "perturbation": self.perturbation.as_record() if self.perturbation else None,
            "attribution": self.attribution.as_record() if self.attribution else None,
        }


async def cascade_detect(
    client: VLMClient,
    image: Image.Image,
    question: str,
    check_question: str,
    cfg: CascadeConfig,
) -> StatementVerdict:
    """
    对单条陈述执行三级级联判定。

    参数：
        question      原始问题（用于记录）
        check_question 该陈述转换成的可判定二值问题，例如
                       "图中是否有一只猫？" —— 转换质量直接决定检测上限。
    """
    # L1：改写一致性（最便宜）
    if cfg.paraphrase_variants:
        p = await probe_paraphrase(client, image, check_question, cfg.paraphrase_variants)
        if p.score >= cfg.paraphrase_threshold:
            return StatementVerdict(statement=question, label="grounded", paraphrase=p)
    else:
        p = None

    # L2：扰动一致性
    b = await probe_perturbation(client, image, check_question, cfg.perturbation_transforms)
    if b.score >= cfg.perturbation_threshold:
        return StatementVerdict(statement=question, label="grounded", paraphrase=p, perturbation=b)

    # L3：遮蔽归因（最昂贵，仅对仍存疑的陈述触发）
    if not cfg.use_l3:
        return StatementVerdict(statement=question, label="uncertain", paraphrase=p, perturbation=b)

    c = await probe_occlusion(client, image, check_question, grid=cfg.coarse_grid)
    if c.has_visual_grounding:
        # 依赖某区域但答案仍不稳定 —— 需人工或辅助模型核验该区域内容，
        # TODO: 接入判定模型，比较该区域内容是否支持陈述。
        label = "misperception"
    else:
        label = "ungrounded"
    return StatementVerdict(statement=question, label=label, paraphrase=p, perturbation=b, attribution=c)


# --------------------------------------------------------------------------
# 指标计算
# --------------------------------------------------------------------------

def classification_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict[str, float]:
    """Accuracy / Precision / Recall / F1 / MCC。正类 = 1 = 判为幻觉。"""
    assert len(y_true) == len(y_pred)
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    n = len(y_true) or 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = ((tp * tn - fp * fn) / denom) if denom else 0.0

    return {
        "n": float(len(y_true)),
        "accuracy": round((tp + tn) / n, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "mcc": round(mcc, 4),
        "miss_rate": round(fn / (tp + fn), 4) if (tp + fn) else 0.0,   # 漏检：幻觉被判为可信
        "false_alarm_rate": round(fp / (tn + fp), 4) if (tn + fp) else 0.0,  # 误报
    }


# --------------------------------------------------------------------------
# B0 基线：POPE 评测（W1 的第一个目标）
# --------------------------------------------------------------------------

def load_pope(jsonl_path: Path) -> list[dict]:
    """
    读取评测数据。TODO: 按官方仓库真实字段调整。

    期望每行：
        {"image": "path/to.jpg", "question": "Is there a cat?", "label": 1}
    其中 label: 1 = 该物体存在（回答 yes 为正确），0 = 不存在（回答 no 为正确）。

    图片路径若是相对路径，按 jsonl 文件所在目录解析，而不是按当前工作目录——
    这样从任何目录运行结果都一致，不会因为 cwd 不同而找不到图片。
    """
    jsonl_path = Path(jsonl_path)
    base_dir = jsonl_path.parent
    rows = []
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            img = Path(rec["image"])
            if not img.is_absolute():
                rec["image"] = str((base_dir / img).resolve())
            rows.append(rec)
    return rows


async def run_pope_baseline(
    client: VLMClient,
    data: Sequence[dict],
    logger: ResultLogger,
    limit: int | None = None,
) -> dict:
    """B0：原始 VLM 直接回答，无任何检测。这是所有对比的基准线。"""

    async def one(row: dict) -> int | None:
        img = Image.open(row["image"]).convert("RGB")
        pred = await client.ask_yesno(img, row["question"], temperature=0.0)
        logger.log(
            {
                "task": "pope_b0",
                "image": str(row["image"]),
                "question": row["question"],
                "gold": row["label"],
                "pred": None if pred is None else int(pred),
            }
        )
        return None if pred is None else int(pred)

    subset = data[:limit] if limit else data
    preds = await asyncio.gather(*[one(r) for r in subset])

    # 解析失败单独统计，不静默丢弃——解析失败率本身是指标
    parsed = [(r["label"], p) for r, p in zip(subset, preds) if p is not None]
    unparseable = len(subset) - len(parsed)
    if not parsed:
        raise RuntimeError("没有任何可解析的输出，先检查 prompt 与模型响应格式")

    metrics = classification_metrics([t for t, _ in parsed], [p for _, p in parsed])
    yes_ratio = sum(p for _, p in parsed) / len(parsed)
    metrics["yes_ratio"] = round(yes_ratio, 4)
    metrics["unparseable"] = unparseable
    return metrics


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

async def main() -> None:
    ap = argparse.ArgumentParser(description="HaluLens 评测骨架")
    ap.add_argument("--task", default="pope_b0", choices=["pope_b0", "probe_demo"])
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--data", type=Path, help="POPE jsonl 路径")
    ap.add_argument("--image", type=Path, help="probe_demo 用的单张图片")
    ap.add_argument("--question", default="Is there a cat in the image?")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "runs")
    ap.add_argument("--cache", type=Path, default=PROJECT_ROOT / ".cache" / "vlm")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--grid", type=int, default=3)
    args = ap.parse_args()

    # max_tokens 默认 1024：DeepSeek V4 属于推理模型，会先输出思维链。
    # 实测 512 仍会出现被截断导致的空回复（12 次里 1 次），所以留足余量。
    gen_params = {"temperature": 0.0, "max_tokens": 1024}
    probe_params = {
        "paraphrase_threshold": 0.85,
        "perturbation_threshold": 0.75,
        "coarse_grid": args.grid,
    }
    meta = RunMeta.new(args.model, gen_params, probe_params, note=f"task={args.task}")
    logger = ResultLogger(args.out, meta)
    client = VLMClient(
        model=args.model,
        logger=logger,
        cache_dir=args.cache,
        concurrency=args.concurrency,
        **gen_params,
    )

    t0 = time.time()
    try:
        if args.task == "pope_b0":
            if not args.data:
                raise SystemExit("--data 必填（POPE jsonl 路径）")
            metrics = await run_pope_baseline(client, load_pope(args.data), logger, limit=args.limit)
            print("\n=== B0 基线（POPE）===")
            for k, v in metrics.items():
                print(f"  {k:20s} {v}")
        else:
            if not args.image:
                raise SystemExit("--image 必填")
            img = Image.open(args.image).convert("RGB")
            p = await probe_paraphrase(
                client,
                img,
                args.question,
                [
                    args.question,
                    args.question.replace("Is there", "Are there any"),
                    f"Please answer yes or no: {args.question}",
                ],
            )
            b = await probe_perturbation(
                client, img, args.question, ["jpeg_recompress", "crop_5pct", "scale_90"]
            )
            c = await probe_occlusion(client, img, args.question, grid=args.grid)
            print("\n=== 探针输出 ===")
            print("  改写一致性：", json.dumps(p.as_record(), ensure_ascii=False))
            print("  扰动一致性：", json.dumps(b.as_record(), ensure_ascii=False))
            print("  遮蔽归因　：", json.dumps(c.as_record(), ensure_ascii=False))
            logger.log(
                {
                    "task": "probe_demo",
                    "question": args.question,
                    "paraphrase": p.as_record(),
                    "perturbation": b.as_record(),
                    "attribution": c.as_record(),
                }
            )
    finally:
        print(f"[耗时] {time.time() - t0:.1f}s")
        logger.close()


def _load_dotenv() -> None:
    """
    极简 .env 读取器，不引入额外依赖。

    只为省去每次开窗口都重设环境变量的麻烦。已存在的真实环境变量优先，
    不会被 .env 覆盖。
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",  # 项目根目录
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
        return


if __name__ == "__main__":
    _load_dotenv()
    asyncio.run(main())
