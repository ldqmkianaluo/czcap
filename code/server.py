"""
server.py —— HaluLens 的本地演示服务

刻意只用 Python 标准库（http.server），不引入 Flask/FastAPI/Node 依赖：
评审或你自己拉下仓库后，一条命令就能跑起来，不需要装任何东西。

设计要点：
    1. **长任务异步化**。一次完整遮蔽归因要 30–60 秒、几十次模型调用，
       如果同步阻塞在 HTTP 请求里，页面会像卡死。所以提交后立即返回
       job_id，前端轮询进度。
    2. **两种模式**。quick 用粗配置快速给个方向（几次调用），full 走完整
       流程并产出四联证据图。
    3. 每次任务独立 client 与 logger，互不干扰，结果仍按 runs/ 的格式落盘。

用法：
    python code\\server.py            # 默认 http://127.0.0.1:8000
    python code\\server.py --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

import halulens as H
from viz import make_attribution_figure

PROJECT_ROOT = H.PROJECT_ROOT
WEB_DIR = PROJECT_ROOT / "web"
REPORTS_DIR = PROJECT_ROOT / "reports"

# 任务表：job_id -> {status, stage, result, error, created}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

MODE_CONFIG = {
    # 粗配置：窗口大、步长大、不收缩。调用次数少，适合快速看方向。
    "quick": {"window": 0.8, "stride": 0.40, "shrink_step": 0.06, "max_refine_boxes": 0},
    # 完整配置：带边缘收缩，产出更紧的证据区域。
    "full": {"window": 0.8, "stride": 0.25, "shrink_step": 0.06, "max_refine_boxes": 2},
}


def _set(job_id: str, **kw) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------

async def _run_job(job_id: str, image_bytes: bytes, question: str, mode: str) -> None:
    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["quick"])
    logger = None
    try:
        _set(job_id, status="running", stage="初始化模型客户端…")

        H._load_dotenv()
        model = H.DEFAULT_MODEL
        meta = H.RunMeta.new(model, {"temperature": 0.0, "max_tokens": 1024}, cfg,
                             note=f"web/{mode}")
        logger = H.ResultLogger(PROJECT_ROOT / "runs", meta)
        client = H.VLMClient(
            model=model,
            logger=logger,
            cache_dir=PROJECT_ROOT / ".cache" / "vlm",
            concurrency=4,
            temperature=0.0,
            max_tokens=1024,
        )

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        _set(job_id, stage="向模型提问并取基线回答…")

        res = await H.probe_attribution_refined(
            client,
            img,
            question,
            window=cfg["window"],
            stride=cfg["stride"],
            shrink_step=cfg["shrink_step"],
            max_refine_boxes=cfg["max_refine_boxes"],
        )

        _set(job_id, stage="整理判定与证据…")
        raw = await client.ask(img, question + H.YESNO_SUFFIX, temperature=0.0)
        baseline_text = (raw.get("text") or "").strip() or "（空）"

        # 判定逻辑与命令行保持一致：必须区分肯定式与否定式回答。
        if res.baseline is None:
            verdict = "基线回答未能解析，本次归因不成立"
            level = "unknown"
        elif res.baseline is False:
            if res.has_visual_grounding:
                verdict = "否定式回答，但存在能改变它的区域——该否定判断依赖局部图像内容"
                level = "grounded"
            else:
                verdict = "否定式回答，且无任何区域能改变它。「不存在」不对应任何区域，无可高亮属正常，不据此判为幻觉"
                level = "ok"
        elif not res.has_visual_grounding:
            if res.whole_image_flips is True:
                verdict = ("有视觉依据（目标过大）：全遮整图会改变答案，说明答案依赖图像内容；"
                           "但没有局部窗口能遮全目标，证据区域约等于整张图")
                level = "grounded"
            elif res.whole_image_flips is False:
                verdict = "高疑似幻觉（无视觉依据型）：连整张图全遮住都不影响答案，模型根本没在看图像"
                level = "hallucination"
            else:
                verdict = "没有窗口能改变答案，且全图遮蔽对照未能解析，结论不确定"
                level = "unknown"
        elif res.supported is False:
            verdict = "高疑似幻觉（视觉误读型）：答案依赖高亮区域，但只看该区域时答案会改变"
            level = "hallucination"
        elif res.supported is True:
            verdict = "陈述被其证据区域支持：只看高亮区域也能得到同样答案"
            level = "ok"
        else:
            verdict = "有视觉依据，但证据区域核验未能解析，结论不确定"
            level = "unknown"

        _set(job_id, stage="生成证据图…")
        evidence_crop = H.crop_box(img, res.evidence_box) if res.evidence_box else None
        out_name = f"attribution-{meta.run_id}.png"
        make_attribution_figure(
            img,
            question=question,
            baseline_text=baseline_text,
            verdict=verdict,
            tested_boxes=res.tested_boxes,
            flip_boxes=res.flip_boxes,
            refined_boxes_norm=res.refined_boxes,
            evidence_crop=evidence_crop,
            calls=res.calls,
            out_path=REPORTS_DIR / out_name,
        )

        logger.log({"task": "web_attribution", "question": question, "mode": mode,
                    **res.as_record()})

        _set(
            job_id,
            status="done",
            stage="完成",
            result={
                "question": question,
                "mode": mode,
                "model": model,
                "baseline_yes": res.baseline,
                "baseline_text": baseline_text,
                "verdict": verdict,
                "level": level,
                "has_visual_grounding": res.has_visual_grounding,
                "supported": res.supported,
                "whole_image_flips": res.whole_image_flips,
                "coverage_px": round(
                    (res.window - res.stride) * min(img.width, img.height), 1
                ),
                "tested": len(res.tested_boxes),
                "flips": len(res.flip_boxes),
                "refined_boxes": [list(b) for b in res.refined_boxes],
                "evidence_box": list(res.evidence_box) if res.evidence_box else None,
                "calls": res.calls,
                "figure": f"/reports/{out_name}",
            },
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要回给前端，不能白屏
        _set(job_id, status="error", stage="失败", error=f"{exc}",
             traceback=traceback.format_exc()[-1500:])
    finally:
        if logger is not None:
            try:
                logger.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HaluLens"

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path: Path, ctype: str) -> None:
        if not path.exists() or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/web/"):
            # 只允许 web/ 下的文件名，挡掉路径穿越
            name = path[len("/web/"):]
            if "/" in name or "\\" in name or name.startswith("."):
                self._json({"error": "bad path"}, 400)
                return
            ctype = "text/css" if name.endswith(".css") else "application/javascript"
            self._file(WEB_DIR / name, f"{ctype}; charset=utf-8")
        elif path.startswith("/reports/"):
            name = path[len("/reports/"):]
            if "/" in name or "\\" in name or name.startswith("."):
                self._json({"error": "bad path"}, 400)
                return
            self._file(REPORTS_DIR / name, "image/png")
        elif path.startswith("/api/job/"):
            job_id = path[len("/api/job/"):]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                self._json({"error": "no such job"}, 404)
            else:
                self._json(job)
        elif path == "/api/health":
            self._json({"ok": True, "model": H.DEFAULT_MODEL})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/analyze":
            self._json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            b64 = payload["image"]
            if "," in b64[:100]:  # 兼容 dataURL 前缀
                b64 = b64.split(",", 1)[1]
            image_bytes = base64.b64decode(b64)
            question = (payload.get("question") or "").strip()
            mode = payload.get("mode") or "quick"
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"请求解析失败：{exc}"}, 400)
            return

        if not question:
            self._json({"error": "question 不能为空"}, 400)
            return
        if mode not in MODE_CONFIG:
            self._json({"error": f"未知模式：{mode}"}, 400)
            return

        try:
            Image.open(io.BytesIO(image_bytes)).verify()
        except Exception:  # noqa: BLE001
            self._json({"error": "上传的不是有效图片"}, 400)
            return

        job_id = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "queued", "stage": "已排队", "created": time.time()}

        threading.Thread(
            target=lambda: asyncio.run(_run_job(job_id, image_bytes, question, mode)),
            daemon=True,
        ).start()
        self._json({"job_id": job_id})


def main() -> None:
    ap = argparse.ArgumentParser(description="HaluLens 本地演示服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    H._load_dotenv()
    if not os.environ.get("VLM_API_KEY"):
        raise SystemExit("没有读到 VLM_API_KEY，请先配置项目根目录的 .env")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print("=" * 62)
    print("HaluLens 演示服务已启动")
    print(f"  地址：{url}")
    print(f"  模型：{H.DEFAULT_MODEL}")
    print("  按 Ctrl+C 停止")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
