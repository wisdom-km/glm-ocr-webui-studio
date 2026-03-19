import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import MethodType
from typing import Any

import gradio as gr
import gradio.blocks as gr_blocks
import requests
import pypdfium2 as pdfium

from glmocr import GlmOcr
from glmocr.maas_client import MissingApiKeyError


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = APP_ROOT / "glm_ocr_outputs_web"
LOGS_ROOT = APP_ROOT / "logs"
RUNTIME_LOG_DIR = LOGS_ROOT / "runtime"
DEBUG_LOG_DIR = LOGS_ROOT / "debug"
DOCS_ROOT = APP_ROOT / "docs"
MAINTENANCE_DOCS_DIR = DOCS_ROOT / "maintenance"
TROUBLESHOOTING_DOCS_DIR = DOCS_ROOT / "troubleshooting"
APP_LOG_FILE = RUNTIME_LOG_DIR / "glm_ocr_web_gui.log"
SELFHOSTED_SERVER_LOG_FILE = RUNTIME_LOG_DIR / "glm_ocr_local_server.log"
STAGING_ROOT = Path(tempfile.gettempdir()) / "glmocr_staging"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".pdf"}
SELFHOSTED_HOST = "127.0.0.1"
SELFHOSTED_PORT = 5002
BAR_TEMPLATE = """
<div class="status-card status-card-progress">
  <div class="status-head">
    <span class="status-title">{label}</span>
    <span class="status-percent">{percent:.1f}%</span>
  </div>
  <div class="status-stage">{stage}</div>
  <div class="status-meter">
    <div class="status-meter-fill" style="width:{percent:.1f}%"></div>
  </div>
  <div class="status-foot">{eta}</div>
</div>
"""


def normalize_file_path(file_obj: Any) -> str:
    if file_obj is None:
        return ""
    if isinstance(file_obj, str):
        return file_obj

    candidates: list[str] = []
    for attr in ("path", "name", "orig_name"):
        if hasattr(file_obj, attr):
            value = getattr(file_obj, attr)
            if value:
                candidates.append(str(value))

    if isinstance(file_obj, dict):
        for key in ("path", "name", "orig_name"):
            value = file_obj.get(key)
            if value:
                candidates.append(str(value))

    seen = set()
    normalized_candidates = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized_candidates.append(candidate)

    if not normalized_candidates:
        raise ValueError(f"Unsupported upload payload: {type(file_obj)!r}")

    for candidate in normalized_candidates:
        if Path(candidate).expanduser().exists():
            return candidate
    return normalized_candidates[0]


def normalize_optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_optional_int(value: str | None, label: str) -> int | None:
    value = normalize_optional_text(value)
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise gr.Error(f"{label} 必须是整数。") from exc
    if parsed <= 0:
        raise gr.Error(f"{label} 必须大于 0。")
    return parsed


def collect_paths(files: list[Any] | None) -> list[str]:
    if not files:
        raise gr.Error("请先上传至少一个图片或 PDF 文件。")

    normalized = []
    for file_obj in files:
        raw_path = normalize_file_path(file_obj)
        path = Path(raw_path).expanduser().resolve()
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if not path.exists():
            raise gr.Error(f"上传文件不存在或无法访问: {raw_path}")
        normalized.append(str(path))

    if not normalized:
        raise gr.Error("没有可处理的图片或 PDF 文件。")
    return normalized


def count_pdf_pages(path: Path) -> int:
    try:
        doc = pdfium.PdfDocument(str(path))
        return len(doc)
    except Exception:
        return 1


def resolve_pdf_page_range(
    path: Path,
    start_page_id: int | None,
    end_page_id: int | None,
) -> tuple[int, int, int, int]:
    total_pages = max(1, count_pdf_pages(path))
    start_page = start_page_id or 1
    end_page = end_page_id or total_pages
    start_page = min(max(1, start_page), total_pages)
    end_page = min(max(start_page, end_page), total_pages)
    selected_pages = max(1, end_page - start_page + 1)
    return total_pages, start_page, end_page, selected_pages


def render_pdf_range_to_images(
    file_path: str,
    staging_dir: Path,
    index: int,
    start_page_id: int | None,
    end_page_id: int | None,
    dpi: int = 200,
) -> tuple[list[str], int]:
    source = Path(file_path)
    _, start_page, end_page, selected_pages = resolve_pdf_page_range(
        source,
        start_page_id,
        end_page_id,
    )
    doc = pdfium.PdfDocument(str(source))
    scale = max(float(dpi) / 72.0, 1.0)
    rendered_paths: list[str] = []
    for page_no in range(start_page - 1, end_page):
        page = doc[page_no]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        page_path = staging_dir / f"input_{index}_page_{page_no + 1:04d}.png"
        pil_image.save(page_path)
        rendered_paths.append(str(page_path))
    return rendered_paths, selected_pages


def estimate_units(
    paths: list[str],
    start_page_id: int | None = None,
    end_page_id: int | None = None,
) -> list[tuple[str, int]]:
    units: list[tuple[str, int]] = []
    for file_path in paths:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".pdf":
            _, _, _, selected_pages = resolve_pdf_page_range(
                Path(file_path),
                start_page_id,
                end_page_id,
            )
            units.append((file_path, selected_pages))
        else:
            units.append((file_path, 1))
    return units


def render_progress(label: str, percent: float, eta: str, stage: str = "当前阶段：等待任务开始") -> str:
    return BAR_TEMPLATE.format(
        label=label,
        percent=max(0.0, min(100.0, percent)),
        eta=eta,
        stage=stage,
    )


def sanitize_output_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)
    value = value.rstrip(" .")
    return value or "result"


def expected_saved_dir(output_root: Path, file_path: str) -> Path:
    return output_root / sanitize_output_name(Path(file_path).stem)


def resolve_saved_dir(output_root: Path, file_path: str) -> Path:
    expected = expected_saved_dir(output_root, file_path)
    if expected.exists():
        return expected

    candidates = [p for p in output_root.iterdir() if p.is_dir()] if output_root.exists() else []
    matching = [p for p in candidates if p.name == expected.name]
    if matching:
        return matching[0]
    return expected


def collect_saved_artifacts(saved_dir: Path) -> list[Path]:
    if not saved_dir.exists():
        return []
    return [
        child
        for child in sorted(saved_dir.rglob("*"))
        if child.is_file() and child.suffix.lower() in {".json", ".md", ".png", ".jpg", ".jpeg"}
    ]


def compute_selfhosted_file_fraction(progress_state: dict[str, Any]) -> float:
    pages_total = max(1, int(progress_state.get("pages_total", 1)))
    pages_done = min(pages_total, int(progress_state.get("pages_done", 0)))
    pages_loaded = min(pages_total, int(progress_state.get("pages_loaded", 0)))
    layout_pages_done = min(pages_total, int(progress_state.get("layout_pages_done", 0)))
    parse_done = 1.0 if progress_state.get("parse_done") else 0.0
    save_done = 1.0 if progress_state.get("save_done") else 0.0

    page_done_ratio = pages_done / pages_total
    load_ratio = pages_loaded / pages_total
    layout_ratio = layout_pages_done / pages_total

    fraction = (
        0.70 * page_done_ratio
        + 0.10 * load_ratio
        + 0.10 * layout_ratio
        + 0.05 * parse_done
        + 0.05 * save_done
    )
    if save_done:
        return 1.0
    return min(max(fraction, 0.0), 0.99)


def snapshot_progress_state(progress_state: dict[str, Any]) -> dict[str, Any]:
    lock = progress_state.get("_lock")
    if lock is None:
        return {k: v for k, v in progress_state.items() if not k.startswith("_")}
    with lock:
        return {k: v for k, v in progress_state.items() if not k.startswith("_")}


def update_progress_state(progress_state: dict[str, Any], **updates: Any) -> dict[str, Any]:
    with progress_state["_lock"]:
        progress_state.update(updates)
        progress_state["last_event_at"] = time.time()
        return {k: v for k, v in progress_state.items() if not k.startswith("_")}


def increment_progress_state(
    progress_state: dict[str, Any], key: str, amount: int = 1
) -> dict[str, Any]:
    with progress_state["_lock"]:
        progress_state[key] = int(progress_state.get(key, 0)) + amount
        progress_state["last_event_at"] = time.time()
        return {k: v for k, v in progress_state.items() if not k.startswith("_")}


def _describe_selfhosted_progress_legacy(progress_state: dict[str, Any]) -> tuple[str, str, int, int]:
    snapshot = snapshot_progress_state(progress_state)
    pages_total = max(1, int(snapshot.get("pages_total", 1)))
    pages_loaded = min(pages_total, int(snapshot.get("pages_loaded", 0)))
    layout_pages_done = min(pages_total, int(snapshot.get("layout_pages_done", 0)))
    regions_total = max(0, int(snapshot.get("regions_total", 0)))
    regions_done = min(regions_total, int(snapshot.get("regions_done", 0)))
    parse_done = bool(snapshot.get("parse_done"))
    save_done = bool(snapshot.get("save_done"))

    if save_done:
        return f"当前进度：{pages_total} / {pages_total} 页", "finished", pages_total, pages_total
    if parse_done:
        return "当前进度：解析完成，正在保存", "saving", layout_pages_done or pages_loaded, pages_total
    if regions_total > 0:
        return f"当前进度：OCR 识别 {regions_done} / {regions_total}", "running", regions_done, regions_total
    if layout_pages_done > 0:
        return f"当前进度：版面分析 {layout_pages_done} / {pages_total} 页", "running", layout_pages_done, pages_total
    if pages_loaded > 0:
        return f"当前进度：页面加载 {pages_loaded} / {pages_total} 页", "counting", pages_loaded, pages_total
    return f"当前进度：0 / {pages_total} 页", "preparing", 0, pages_total


def progress_stage_text(progress_state: dict[str, Any], phase: str) -> str:
    snapshot = snapshot_progress_state(progress_state)
    if snapshot.get("save_done"):
        return "已完成"
    if snapshot.get("parse_done"):
        return "解析完成，正在保存"
    if phase == "running":
        if int(snapshot.get("regions_total", 0)) > 0:
            return "OCR 识别中"
        if int(snapshot.get("layout_pages_done", 0)) > 0:
            return "正在版面分析"
        if int(snapshot.get("pages_loaded", 0)) > 0:
            return "正在渲染页面"
    if phase == "counting":
        return "正在加载页面"
    if phase == "backend_wait":
        return "正在等待本地 OCR 服务响应"
    if phase == "retrying":
        return "正在重试当前页"
    if phase == "restarting_service":
        return "正在重启本地 OCR 服务"
    if phase == "page_failed":
        return "当前页失败，继续后续页面"
    return "正在打开 PDF"


def format_progress_foot(stage_text: str, remaining_seconds: float | None) -> str:
    return f"当前阶段：{stage_text} | {format_remaining_time(remaining_seconds)}"


def is_selfhosted_timeout_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return "Read timed out" in message or "ConnectionResetError" in message or (
        "HTTPConnectionPool" in message and "127.0.0.1" in message and "5002" in message
    )


def summarize_task_failure(exc: Exception, mode: str) -> str:
    raw_error = f"{type(exc).__name__}: {exc}"
    if mode == "selfhosted":
        timeout_match = re.search(r"read timeout=(\d+)", raw_error, re.IGNORECASE)
        if "Read timed out" in raw_error:
            timeout_text = f"{timeout_match.group(1)} 秒" if timeout_match else "300 秒"
            return f"本地 OCR 服务请求超时：127.0.0.1:5002，{timeout_text} 内未返回结果。"
        if "ConnectionResetError" in raw_error:
            return "本地 OCR 服务连接中断：127.0.0.1:5002。"
    return raw_error


def estimate_selfhosted_eta_seconds(
    current_progress: dict[str, Any] | None,
    current_file_units: float,
    total_units: float,
    completed_units: float,
    task_started_at: float,
) -> float | None:
    if current_progress is None or current_file_units <= 0 or total_units <= 0:
        return None

    snapshot = snapshot_progress_state(current_progress)
    fraction = compute_selfhosted_file_fraction(snapshot)
    if fraction <= 0.01:
        return None

    elapsed_task = max(0.0, time.time() - task_started_at)
    progress_units = completed_units + (current_file_units * fraction)
    if progress_units <= 0:
        return None

    progress_ratio = min(max(progress_units / float(total_units), 1e-6), 0.999999)
    estimated_total = elapsed_task / progress_ratio
    return max(0.0, estimated_total - elapsed_task)


def describe_selfhosted_page_counts(progress_state: dict[str, Any]) -> tuple[int, int]:
    snapshot = snapshot_progress_state(progress_state)
    pages_total = max(1, int(snapshot.get("pages_total", 1)))
    pages_done = min(pages_total, int(snapshot.get("pages_done", 0)))
    parse_done = bool(snapshot.get("parse_done"))
    save_done = bool(snapshot.get("save_done"))

    if save_done or parse_done:
        return pages_total, pages_total

    return min(pages_done, pages_total), pages_total


def describe_selfhosted_progress(
    progress_state: dict[str, Any],
) -> tuple[str, str, str, int, int]:
    snapshot = snapshot_progress_state(progress_state)
    current_pages, pages_total = describe_selfhosted_page_counts(snapshot)
    pages_loaded = min(pages_total, int(snapshot.get("pages_loaded", 0)))
    layout_pages_done = min(pages_total, int(snapshot.get("layout_pages_done", 0)))
    current_page_hint = min(pages_total, max(1, int(snapshot.get("current_page_hint", 1))))
    current_page_region_done = max(0, int(snapshot.get("current_page_region_done", 0)))
    current_page_region_total = max(0, int(snapshot.get("current_page_region_total", 0)))
    parse_done = bool(snapshot.get("parse_done"))
    save_done = bool(snapshot.get("save_done"))
    phase = str(snapshot.get("phase") or "preparing")

    if save_done:
        return (
            f"当前进度：{pages_total} / {pages_total} 页",
            "当前阶段：已完成",
            "finished",
            pages_total,
            pages_total,
        )
    if parse_done:
        return (
            f"当前进度：{pages_total} / {pages_total} 页",
            "当前阶段：正在保存结果",
            "saving",
            pages_total,
            pages_total,
        )
    if current_pages > 0:
        return (
            f"当前进度：{current_pages} / {pages_total} 页",
            (
                f"当前阶段：正在处理第 {min(current_pages + 1, pages_total)} 页"
                if current_pages < pages_total
                else "当前阶段：正在完成最后收尾"
            ),
            "running",
            current_pages,
            pages_total,
        )
    if layout_pages_done > 0:
        stage_text = "当前阶段：正在进行版面分析"
        if current_page_region_total > 0:
            stage_text = (
                f"当前阶段：当前页局部识别 第{current_page_hint}页 "
                f"{current_page_region_done}/{current_page_region_total} 区块"
            )
        return (
            f"当前进度：0 / {pages_total} 页",
            stage_text,
            "running",
            0,
            pages_total,
        )
    if pages_loaded > 0:
        stage_text = "当前阶段：正在加载页面"
        if current_page_region_total > 0:
            stage_text = (
                f"当前阶段：当前页局部识别 第{current_page_hint}页 "
                f"{current_page_region_done}/{current_page_region_total} 区块"
            )
        return (
            f"当前进度：0 / {pages_total} 页",
            stage_text,
            "counting",
            0,
            pages_total,
        )

    phase_to_stage = {
        "preparing": "当前阶段：准备解析任务",
        "parse_prepare_start": "当前阶段：准备解析任务",
        "pdf_opened": "当前阶段：正在打开 PDF",
        "page_render_start": "当前阶段：正在渲染第一页",
        "first_page_wait": "当前阶段：正在等待第一页进入 OCR",
        "backend_wait": (
            f"当前阶段：当前页局部识别 第{current_page_hint}页 "
            f"{current_page_region_done}/{current_page_region_total} 区块（等待本地 OCR 服务响应）"
            if current_page_region_total > 0
            else "当前阶段：正在等待本地 OCR 服务响应"
        ),
        "retrying": f"当前阶段：正在重试第 {current_page_hint} 页",
        "restarting_service": "当前阶段：正在重启本地 OCR 服务",
        "page_failed": f"当前阶段：第 {current_page_hint} 页失败，继续后续页面",
        "failed": "当前阶段：任务失败",
    }
    return (
        f"当前进度：0 / {pages_total} 页",
        phase_to_stage.get(phase, "当前阶段：准备解析任务"),
        phase,
        0,
        pages_total,
    )


def extract_backend_timeout_details(text: str) -> dict[str, Any] | None:
    if "Read timed out" not in text and "read timeout" not in text.lower():
        return None
    host_match = re.search(r"host='([^']+)'", text)
    port_match = re.search(r"port=(\d+)", text)
    timeout_match = re.search(r"read timeout[= ](\d+)", text, flags=re.IGNORECASE)
    host = host_match.group(1) if host_match else SELFHOSTED_HOST
    port = int(port_match.group(1)) if port_match else SELFHOSTED_PORT
    timeout_seconds = int(timeout_match.group(1)) if timeout_match else 300
    return {
        "service": f"{host}:{port}",
        "timeout_seconds": timeout_seconds,
    }


def extract_runtime_context_details(text: str) -> dict[str, str]:
    details: dict[str, str] = {}
    phase_match = re.search(r"phase=([A-Za-z0-9_:-]+)", text)
    current_match = re.search(r"current=([0-9]+)", text)
    total_match = re.search(r"total=([0-9]+)", text)
    item_match = re.search(r"item=([0-9]+)", text)
    wait_match = re.search(r"elapsed_wait=([0-9]+s)", text)
    request_match = re.search(r"request_id=([A-Za-z0-9_:-]+)", text)
    page_match = re.search(r"page=([0-9]+)", text)
    region_match = re.search(r"region=([0-9]+)", text)
    if phase_match:
        details["phase"] = phase_match.group(1)
    if current_match and total_match:
        details["progress"] = f"{current_match.group(1)}/{total_match.group(1)}"
    if item_match:
        details["item"] = item_match.group(1)
    if wait_match:
        details["elapsed_wait"] = wait_match.group(1)
    if request_match:
        details["request_id"] = request_match.group(1)
    if page_match:
        details["page"] = page_match.group(1)
    if region_match:
        details["region"] = region_match.group(1)
    return details


def summarize_error_for_ui(error_msg: str, traceback_text: str = "") -> dict[str, str]:
    combined = "\n".join(part for part in (error_msg, traceback_text) if part)
    timeout_info = extract_backend_timeout_details(combined)
    if timeout_info:
        timeout_seconds = timeout_info["timeout_seconds"]
        service = timeout_info["service"]
        context = extract_runtime_context_details(combined)
        context_lines = []
        if context.get("phase"):
            context_lines.append(f"当前阶段：{context['phase']}")
        if context.get("item"):
            context_lines.append(f"当前文件序号：{context['item']}")
        if context.get("progress"):
            context_lines.append(f"阶段进度：{context['progress']}")
        if context.get("elapsed_wait"):
            context_lines.append(f"已等待：{context['elapsed_wait']}")
        context_text = ("\n" + "\n".join(context_lines)) if context_lines else ""
        return {
            "summary": f"本地 OCR 服务请求超时（{service}，{timeout_seconds} 秒）",
            "stage": "当前阶段：等待本地 OCR 服务超时",
            "detail": (
                f"原因：本地 OCR 服务 {service} 在 {timeout_seconds} 秒内未返回结果。"
                f"{context_text}\n"
                "建议检查 selfhosted 服务是否卡住、推理过慢，或是否存在单页处理异常。"
            ),
        }
    return {
        "summary": f"识别失败：{error_msg}",
        "stage": "当前阶段：任务失败",
        "detail": f"原因：{error_msg}",
    }


def summarize_error_for_ui_v2(error_msg: str, traceback_text: str = "") -> dict[str, str]:
    combined = "\n".join(part for part in (error_msg, traceback_text) if part)
    timeout_info = extract_backend_timeout_details(combined)
    if timeout_info:
        timeout_seconds = timeout_info["timeout_seconds"]
        service = timeout_info["service"]
        context = extract_runtime_context_details(combined)
        context_lines: list[str] = []
        if context.get("phase"):
            context_lines.append(f"阶段：{context['phase']}")
        if context.get("item"):
            context_lines.append(f"任务项：{context['item']}")
        if context.get("progress"):
            context_lines.append(f"当前进度：{context['progress']}")
        location_parts: list[str] = []
        if context.get("page"):
            location_parts.append(f"page={context['page']}")
        if context.get("region"):
            location_parts.append(f"region={context['region']}")
        if location_parts:
            context_lines.append(f"卡住位置：{', '.join(location_parts)}")
        if context.get("request_id"):
            context_lines.append(f"请求 ID：{context['request_id']}")
        if context.get("elapsed_wait"):
            context_lines.append(f"已等待：{context['elapsed_wait']}")
        context_text = ("\n" + "\n".join(context_lines)) if context_lines else ""
        return {
            "summary": f"本地 OCR 服务请求超时：{service}（{timeout_seconds} 秒）",
            "stage": "当前阶段：等待本地 OCR 服务超时",
            "detail": (
                f"本地 OCR 服务 {service} 在 {timeout_seconds} 秒内未返回结果。"
                f"{context_text}\n"
                "建议：检查 selfhosted 本地服务是否卡在单页或单个局部请求的生成阶段。"
            ),
        }
    return {
        "summary": f"识别失败：{error_msg}",
        "stage": "当前阶段：任务失败",
        "detail": f"错误详情：{error_msg}",
    }


@contextmanager
def install_selfhosted_progress_hooks(
    parser: GlmOcr,
    event_queue: queue.Queue,
    task_id: str,
    file_index: int,
    file_path: str,
    pages_total: int,
    *,
    initial_pages_done: int = 0,
    initial_pages_loaded: int = 0,
    initial_layout_pages_done: int = 0,
    initial_page_hint: int = 1,
):
    pipeline = getattr(parser, "_pipeline", None)
    if pipeline is None:
        yield None
        return

    progress_state: dict[str, Any] = {
        "pages_total": max(1, int(pages_total)),
        "pages_done": max(0, int(initial_pages_done)),
        "pages_loaded": max(0, int(initial_pages_loaded)),
        "layout_pages_done": max(0, int(initial_layout_pages_done)),
        "regions_total": 0,
        "regions_done": 0,
        "parse_done": False,
        "save_done": False,
        "phase": "preparing",
        "backend_wait_started_at": None,
        "current_request_id": "",
        "current_page_hint": max(1, int(initial_page_hint)),
        "current_page_region_done": 0,
        "current_page_region_total": 0,
        "request_seq": 0,
        "per_page_regions": {},
        "last_backend_activity_at": time.time(),
        "failure_reason": "",
        "failure_stage": "",
        "started_at": time.time(),
        "last_event_at": time.time(),
        "_lock": threading.Lock(),
    }

    original_iter = pipeline.page_loader.iter_pages_with_unit_indices
    original_layout_batch = pipeline._stream_process_layout_batch
    original_ocr_process = pipeline.ocr_client.process
    original_process = pipeline.process

    def emit(event_type: str, **payload: Any) -> None:
        event_queue.put(
            {
                "type": event_type,
                "index": file_index,
                "file_path": file_path,
                "progress_state": snapshot_progress_state(progress_state),
                **payload,
            }
        )

    def patched_iter_pages_with_unit_indices(*args: Any, **kwargs: Any):
        for page, unit_idx in original_iter(*args, **kwargs):
            snapshot = increment_progress_state(progress_state, "pages_loaded", 1)
            snapshot = update_progress_state(
                progress_state,
                phase="counting",
                last_backend_activity_at=time.time(),
            )
            emit("page_loaded", pages_loaded=snapshot["pages_loaded"])
            yield page, unit_idx

    def patched_stream_process_layout_batch(self, batch_images, batch_indices, region_queue, images_dict, layout_results_dict, save_visualization, vis_output_dir, global_start_idx):
        original_put = region_queue.put

        def patched_put(item: Any, *args: Any, **kwargs: Any):
            if isinstance(item, tuple) and item and item[0] == "region":
                increment_progress_state(progress_state, "regions_total", 1)
            return original_put(item, *args, **kwargs)

        region_queue.put = patched_put
        try:
            original_layout_batch(
                batch_images,
                batch_indices,
                region_queue,
                images_dict,
                layout_results_dict,
                save_visualization,
                vis_output_dir,
                global_start_idx,
            )
        finally:
            region_queue.put = original_put
        batch_region_count = 0
        page_region_updates: list[tuple[int, int]] = []
        for img_idx in batch_indices:
            region_count = len(layout_results_dict.get(img_idx, []))
            batch_region_count += region_count
            page_no = int(img_idx) + 1
            page_region_updates.append((page_no, region_count))
        with progress_state["_lock"]:
            per_page_regions = dict(progress_state.get("per_page_regions", {}))
            for page_no, region_count in page_region_updates:
                per_page_regions[page_no] = int(region_count)
            progress_state["per_page_regions"] = per_page_regions
            current_page_hint = int(progress_state.get("current_page_hint", 1))
            if int(progress_state.get("current_page_region_total", 0)) <= 0 and current_page_hint in per_page_regions:
                progress_state["current_page_region_total"] = int(per_page_regions[current_page_hint])
            progress_state["last_event_at"] = time.time()
        snapshot = increment_progress_state(
            progress_state, "layout_pages_done", len(batch_indices)
        )
        if snapshot["layout_pages_done"] > snapshot["pages_total"]:
            snapshot = update_progress_state(
                progress_state,
                layout_pages_done=snapshot["pages_total"],
                phase="running",
                last_backend_activity_at=time.time(),
            )
        else:
            snapshot = update_progress_state(
                progress_state,
                phase="running",
                last_backend_activity_at=time.time(),
            )
        emit(
            "layout_batch_done",
            batch_pages=len(batch_indices),
            batch_regions=batch_region_count,
        )
        for page_no, region_count in page_region_updates:
            emit(
                "page_region_metrics",
                page=page_no,
                regions=region_count,
            )

    def patched_ocr_process(request_data: dict[str, Any]):
        with progress_state["_lock"]:
            progress_state["request_seq"] = int(progress_state.get("request_seq", 0)) + 1
            request_seq = int(progress_state["request_seq"])
            page_hint = min(
                int(progress_state.get("pages_total", 1)),
                int(progress_state.get("pages_done", 0)) + 1,
            )
            per_page_regions = dict(progress_state.get("per_page_regions", {}))
            request_id = f"{task_id}_req_{request_seq:05d}"
            progress_state["current_request_id"] = request_id
            progress_state["current_page_hint"] = page_hint
            if int(progress_state.get("current_page_region_total", 0)) <= 0 and page_hint in per_page_regions:
                progress_state["current_page_region_total"] = int(per_page_regions[page_hint])
            progress_state["last_event_at"] = time.time()

        request_data = dict(request_data)
        request_data["trace_task_id"] = task_id
        request_data["trace_request_id"] = request_id
        request_data["trace_page"] = page_hint
        request_data["trace_region"] = request_seq
        request_data["trace_stage"] = "ocr_request"
        wait_started_at = time.time()
        stop_wait = threading.Event()

        def backend_wait_watcher() -> None:
            thresholds = (10, 60, 120, 240)
            emitted_thresholds: set[int] = set()
            while not stop_wait.wait(1.0):
                elapsed_wait = int(time.time() - wait_started_at)
                for threshold in thresholds:
                    if elapsed_wait < threshold or threshold in emitted_thresholds:
                        continue
                    emitted_thresholds.add(threshold)
                    phase = "backend_wait"
                    snapshot = update_progress_state(
                        progress_state,
                        phase=phase,
                        backend_wait_started_at=wait_started_at,
                    )
                    emit(
                        "backend_wait",
                        service=f"{SELFHOSTED_HOST}:{SELFHOSTED_PORT}",
                        phase_name="ocr_request",
                        request_id=request_id,
                        page=page_hint,
                        region=request_seq,
                        elapsed_wait=elapsed_wait,
                        progress_state=snapshot,
                    )

        watcher = threading.Thread(target=backend_wait_watcher, daemon=True)
        watcher.start()
        try:
            response, status_code = original_ocr_process(request_data)
        finally:
            stop_wait.set()
            watcher.join(timeout=0.2)

        current_time = time.time()
        if status_code == 200:
            snapshot = increment_progress_state(progress_state, "regions_done", 1)
            if int(snapshot.get("regions_total", 0)) < int(snapshot.get("regions_done", 0)):
                snapshot = update_progress_state(
                    progress_state,
                    regions_total=int(snapshot.get("regions_done", 0)),
                )
            with progress_state["_lock"]:
                progress_state["current_page_region_done"] = int(progress_state.get("current_page_region_done", 0)) + 1
                progress_state["last_event_at"] = time.time()
            snapshot = update_progress_state(
                progress_state,
                phase="running",
                backend_wait_started_at=None,
                last_backend_activity_at=current_time,
            )
            emit("region_done", regions_done=snapshot["regions_done"])
            return response, status_code

        error_text = str(response.get("error") or response)
        timeout_info = extract_backend_timeout_details(error_text)
        if timeout_info:
            snapshot = update_progress_state(
                progress_state,
                phase="failed",
                backend_wait_started_at=wait_started_at,
                failure_reason="本地 OCR 服务请求超时",
                failure_stage="等待本地 OCR 服务超时",
                last_backend_activity_at=current_time,
            )
            emit(
                "backend_timeout",
                service=timeout_info["service"],
                timeout_seconds=timeout_info["timeout_seconds"],
                phase_name="ocr_request",
                request_id=request_id,
                page=page_hint,
                region=request_seq,
                error=error_text,
                progress_state=snapshot,
            )
        else:
            snapshot = update_progress_state(
                progress_state,
                phase="failed",
                failure_reason=error_text,
                failure_stage="OCR 请求失败",
                last_backend_activity_at=current_time,
            )
            emit(
                "backend_failure",
                service=f"{SELFHOSTED_HOST}:{SELFHOSTED_PORT}",
                phase_name="ocr_request",
                request_id=request_id,
                page=page_hint,
                region=request_seq,
                error=error_text,
                progress_state=snapshot,
            )
        return response, status_code

    def patched_process(self, *args: Any, **kwargs: Any):
        for result in original_process(*args, **kwargs):
            snapshot = increment_progress_state(progress_state, "pages_done", 1)
            with progress_state["_lock"]:
                next_page = min(
                    int(progress_state.get("pages_total", 1)),
                    int(progress_state.get("pages_done", 0)) + 1,
                )
                per_page_regions = dict(progress_state.get("per_page_regions", {}))
                progress_state["current_page_hint"] = next_page
                progress_state["current_page_region_done"] = 0
                progress_state["current_page_region_total"] = int(per_page_regions.get(next_page, 0))
                progress_state["last_event_at"] = time.time()
            snapshot = update_progress_state(
                progress_state,
                phase="running",
                last_backend_activity_at=time.time(),
            )
            emit("page_done", pages_done=snapshot["pages_done"])
            yield result

    pipeline.page_loader.iter_pages_with_unit_indices = patched_iter_pages_with_unit_indices
    pipeline._stream_process_layout_batch = MethodType(patched_stream_process_layout_batch, pipeline)
    pipeline.ocr_client.process = patched_ocr_process
    pipeline.process = MethodType(patched_process, pipeline)

    try:
        yield progress_state
    finally:
        pipeline.page_loader.iter_pages_with_unit_indices = original_iter
        pipeline._stream_process_layout_batch = original_layout_batch
        pipeline.ocr_client.process = original_ocr_process
        pipeline.process = original_process


def append_app_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with APP_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def format_elapsed_compact(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def build_task_id() -> str:
    return f"ocr_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"


def summarize_task_inputs(paths: list[str], limit: int = 3) -> str:
    names = [Path(path).name for path in paths[:limit]]
    if len(paths) > limit:
        names.append(f"+{len(paths) - limit} more")
    return ", ".join(names)


def append_runtime_log(
    level: str,
    event: str,
    *,
    state: dict[str, Any] | None = None,
    include_in_gui: bool = True,
    **fields: Any,
) -> str:
    parts = [f"[{level}] {event}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    message = " | ".join(parts)
    append_app_log(message)
    if include_in_gui and state is not None:
        state["log_lines"].append(message)
    return message


def needs_ascii_staging(file_path: str) -> bool:
    return not file_path.isascii()


def stage_input_for_parser(file_path: str, staging_dir: Path, index: int) -> str:
    source = Path(file_path)
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_name = f"input_{index}{source.suffix.lower()}"
    staged_path = staging_dir / staged_name
    staged_path.write_bytes(source.read_bytes())
    return str(staged_path)


def normalize_combined_markdown_page(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    normalized_lines: list[str] = []
    last_nonempty = ""
    page_marker_re = re.compile(r"^\s*第\s*\d+\s*页\s*[-:：]?\s*$")

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        # Drop standalone page markers that make the merged Markdown look artificial.
        if stripped and page_marker_re.match(stripped):
            continue

        if not stripped:
            if normalized_lines and normalized_lines[-1] == "":
                continue
            normalized_lines.append("")
            continue

        # Drop obvious duplicate title/header lines introduced by page stitching.
        if stripped == last_nonempty and (stripped.startswith("#") or len(stripped) <= 80):
            continue

        normalized_lines.append(line)
        last_nonempty = stripped

    while normalized_lines and normalized_lines[0] == "":
        normalized_lines.pop(0)
    while normalized_lines and normalized_lines[-1] == "":
        normalized_lines.pop()

    return "\n".join(normalized_lines).strip()


def should_merge_broken_paragraph(previous_paragraph: str, next_paragraph: str) -> bool:
    prev = previous_paragraph.strip()
    nxt = next_paragraph.strip()
    if not prev or not nxt:
        return False

    if prev.startswith("#") or nxt.startswith("#"):
        return False
    if re.match(r"^\[[0-9]+\]", nxt) or re.match(r"^[0-9]{1,2}[.．、]", nxt):
        return False

    sentence_endings = "。！？!?：:；;"
    if prev[-1] in sentence_endings:
        return False

    # Typical page-break leftovers are short tail fragments like "我觉得将它"
    # followed by a normal paragraph continuing the same sentence.
    if len(prev) <= 40:
        return True

    # Some OCR outputs split a paragraph into two blocks where the first block
    # ends without punctuation and the next block begins with a continuation.
    continuation_start_re = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9“\"'‘（(]")
    if len(prev) <= 120 and continuation_start_re.match(nxt):
        return True

    return False


def merge_broken_paragraphs(text: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return ""

    merged: list[str] = []
    for paragraph in paragraphs:
        if merged and should_merge_broken_paragraph(merged[-1], paragraph):
            merged[-1] = f"{merged[-1].rstrip()}{paragraph.lstrip()}"
        else:
            merged.append(paragraph)
    return "\n\n".join(merged).strip()


def build_combined_markdown(page_markdown_parts: list[str]) -> str:
    cleaned_parts = [
        cleaned
        for cleaned in (normalize_combined_markdown_page(part) for part in page_markdown_parts)
        if cleaned
    ]
    combined = "\n\n".join(cleaned_parts).strip()
    combined = merge_broken_paragraphs(combined)
    combined = re.sub(r"\n{3,}", "\n\n", combined)
    return combined


def list_listening_pids_on_port(host: str, port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except Exception:
        return []

    pids: list[int] = []
    host_candidates = {host, "0.0.0.0", "[::]", "::", "127.0.0.1", "localhost"}
    suffix = f":{port}"
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_address = parts[1]
        pid_text = parts[-1]
        if not local_address.endswith(suffix):
            continue
        local_host = local_address[: -len(suffix)]
        if local_host not in host_candidates:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def stop_processes_on_port(host: str, port: int) -> list[int]:
    stopped: list[int] = []
    for pid in list_listening_pids_on_port(host, port):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                subprocess.run(["kill", "-9", str(pid)], check=False)
            stopped.append(pid)
        except Exception:
            continue
    return stopped


def spawn_selfhosted_server_with_env(extra_env: dict[str, str] | None = None) -> None:
    if is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
        return

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env = os.environ.copy()
    if extra_env:
        env.update({key: str(value) for key, value in extra_env.items() if value is not None})
    SELFHOSTED_SERVER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    server_log_handle = SELFHOSTED_SERVER_LOG_FILE.open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(APP_ROOT / "glm_ocr_local_server.py"),
                "--host",
                SELFHOSTED_HOST,
                "--port",
                str(SELFHOSTED_PORT),
            ],
            cwd=str(APP_ROOT),
            creationflags=creationflags,
            env=env,
            stdout=server_log_handle,
            stderr=subprocess.STDOUT,
        )
    finally:
        server_log_handle.close()


def restart_selfhosted_server(
    progress,
    *,
    task_id: str | None = None,
    state: dict[str, Any] | None = None,
    reason: str = "",
    max_new_tokens_cap: int | None = None,
) -> None:
    append_runtime_log(
        "WARN",
        "backend restart start",
        state=state,
        task_id=task_id,
        host=SELFHOSTED_HOST,
        port=SELFHOSTED_PORT,
        reason=reason or "selfhosted recovery",
        max_tokens_cap=max_new_tokens_cap,
        phase="backend_restart",
    )
    stopped_pids = stop_processes_on_port(SELFHOSTED_HOST, SELFHOSTED_PORT)
    append_runtime_log(
        "INFO",
        "backend restart stop",
        state=state,
        task_id=task_id,
        host=SELFHOSTED_HOST,
        port=SELFHOSTED_PORT,
        stopped_pids=",".join(str(pid) for pid in stopped_pids) if stopped_pids else None,
        phase="backend_restart",
    )
    progress(0, desc=f"重启本地 GLM-OCR 服务 {SELFHOSTED_HOST}:{SELFHOSTED_PORT}")
    extra_env = {}
    if max_new_tokens_cap is not None:
        extra_env["GLMOCR_MAX_NEW_TOKENS"] = str(max_new_tokens_cap)
    spawn_selfhosted_server_with_env(extra_env or None)
    wait_for_local_server(
        SELFHOSTED_HOST,
        SELFHOSTED_PORT,
        task_id=task_id,
        state=state,
    )
    append_runtime_log(
        "INFO",
        "backend restart done",
        state=state,
        task_id=task_id,
        host=SELFHOSTED_HOST,
        port=SELFHOSTED_PORT,
        max_tokens_cap=max_new_tokens_cap,
        phase="backend_ready",
    )


def write_selfhosted_partial_report(
    saved_dir: Path,
    *,
    file_path: str,
    page_markdown_parts: list[str],
    page_json_payloads: list[dict[str, Any]],
    page_failures: list[dict[str, Any]],
    page_start_number: int | None = None,
    page_end_number: int | None = None,
) -> tuple[Path, Path, Path]:
    saved_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = saved_dir / "combined_pages.md"
    markdown_text = build_combined_markdown(page_markdown_parts)
    markdown_path.write_text(markdown_text, encoding="utf-8")
    source_name = sanitize_output_name(Path(file_path).stem)
    if page_start_number is not None and page_end_number is not None:
        named_markdown_path = saved_dir / f"{source_name}_{page_start_number}-{page_end_number}_完整.md"
    else:
        named_markdown_path = saved_dir / f"{source_name}_完整.md"
    named_markdown_path.write_text(markdown_text, encoding="utf-8")
    report_path = saved_dir / "partial_report.json"
    report_path.write_text(
        json.dumps(
            {
                "input_file": file_path,
                "page_start_number": page_start_number,
                "page_end_number": page_end_number,
                "combined_markdown": str(named_markdown_path),
                "completed_pages": [payload.get("page") for payload in page_json_payloads],
                "failed_pages": page_failures,
                "pages": page_json_payloads,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return markdown_path, named_markdown_path, report_path


def build_error_outputs(error_msg: str, traceback_text: str = "") -> tuple[str, str, str, str, list[str], str]:
    error_view = summarize_error_for_ui_v2(error_msg, traceback_text)
    progress_html = render_progress(
        "当前进度：任务失败",
        0.0,
        "剩余时间：0 秒",
        error_view["stage"],
    )
    json_text = json.dumps(
        {
            "error": error_msg,
            "traceback": traceback_text or None,
        },
        ensure_ascii=False,
        indent=2,
    )
    logs_text = traceback_text or error_msg
    return error_view["detail"], "", json_text, logs_text, [], progress_html


def format_remaining_time(seconds: float | None) -> str:
    if seconds is None:
        return "剩余时间：计算中"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return "剩余时间：< 1 秒"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"剩余时间：{hours} 小时 {minutes} 分 {sec} 秒"
    if minutes:
        return f"剩余时间：{minutes} 分 {sec} 秒"
    return f"剩余时间：{sec} 秒"


def format_eta(seconds: float | None) -> str:
    return format_remaining_time(seconds)


def build_summary(file_path: str, saved_dir: Path, result_dict: dict[str, Any]) -> str:
    lines = [
        f"输入文件: {file_path}",
        f"输出目录: {saved_dir}",
        f"JSON 类型: {type(result_dict.get('json_result')).__name__}",
        f"Markdown 长度: {len(result_dict.get('markdown_result') or '')} 字符",
    ]
    usage = result_dict.get("usage")
    if usage:
        lines.append("usage:")
        lines.append(json.dumps(usage, ensure_ascii=False, indent=2))
    error = result_dict.get("error")
    if error:
        lines.append(f"错误: {error}")
    return "\n".join(lines)


def is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def find_available_port(host: str, preferred_port: int, search_limit: int = 20) -> int:
    for port in range(preferred_port, preferred_port + search_limit):
        if not is_port_open(host, port):
            return port
    raise RuntimeError(
        f"无法在 {preferred_port}-{preferred_port + search_limit - 1} 范围内找到可用端口。"
    )


def wait_for_local_server(
    host: str,
    port: int,
    timeout: int = 180,
    *,
    task_id: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    start = time.time()
    warning_marks = (15, 30, 60, 120)
    emitted_marks: set[int] = set()
    last_health_error = ""
    append_runtime_log(
        "INFO",
        "backend wait start",
        state=state,
        task_id=task_id,
        host=host,
        port=port,
        timeout=f"{timeout}s",
        phase="backend_wait",
        reason="waiting for selfhosted backend",
    )
    while time.time() - start < timeout:
        if is_port_open(host, port):
            try:
                response = requests.get(
                    f"http://{host}:{port}/health",
                    timeout=2,
                    proxies={"http": None, "https": None},
                )
                if response.ok:
                    append_runtime_log(
                        "INFO",
                        "backend wait ready",
                        state=state,
                        task_id=task_id,
                        host=host,
                        port=port,
                        elapsed=format_elapsed_compact(time.time() - start),
                        phase="backend_ready",
                    )
                    return
            except Exception as exc:
                last_health_error = str(exc)
        elapsed_wait = int(time.time() - start)
        for mark in warning_marks:
            if elapsed_wait < mark or mark in emitted_marks:
                continue
            emitted_marks.add(mark)
            append_runtime_log(
                "WARN",
                "backend wait slow",
                state=state,
                task_id=task_id,
                host=host,
                port=port,
                elapsed_wait=f"{elapsed_wait}s",
                timeout=f"{timeout}s",
                phase="backend_wait",
                error=last_health_error or None,
            )
        time.sleep(2)
    elapsed = format_elapsed_compact(time.time() - start)
    append_runtime_log(
        "ERROR",
        "backend wait timeout",
        state=state,
        task_id=task_id,
        host=host,
        port=port,
        timeout=f"{timeout}s",
        elapsed=elapsed,
        phase="backend_wait",
        error=last_health_error or "backend health check timed out",
    )
    raise gr.Error(
        f"本地 GLM-OCR 服务等待超时：{host}:{port} 在 {timeout} 秒内未就绪。"
    )


def ensure_selfhosted_server(
    progress,
    *,
    task_id: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    if is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
        append_runtime_log(
            "INFO",
            "backend ready",
            state=state,
            task_id=task_id,
            host=SELFHOSTED_HOST,
            port=SELFHOSTED_PORT,
            phase="backend_ready",
            already_running=True,
        )
        return

    progress(0, desc=f"等待本地 GLM-OCR 服务 {SELFHOSTED_HOST}:{SELFHOSTED_PORT}")
    append_runtime_log(
        "INFO",
        "backend spawn start",
        state=state,
        task_id=task_id,
        host=SELFHOSTED_HOST,
        port=SELFHOSTED_PORT,
        phase="backend_spawn",
    )
    spawn_selfhosted_server()
    wait_for_local_server(
        SELFHOSTED_HOST,
        SELFHOSTED_PORT,
        task_id=task_id,
        state=state,
    )


def spawn_selfhosted_server() -> None:
    if is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
        return

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE

    subprocess.Popen(
        [
            sys.executable,
            str(APP_ROOT / "glm_ocr_local_server.py"),
            "--host",
            SELFHOSTED_HOST,
            "--port",
            str(SELFHOSTED_PORT),
        ],
        cwd=str(APP_ROOT),
        creationflags=creationflags,
    )


def fetch_backend_status(auto_start: bool = True) -> tuple[str, str]:
    try:
        if auto_start and not is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
            spawn_selfhosted_server()
            return (
                "后端: selfhosted\n状态: 启动中\n详情: 正在启动本地服务...",
                """
                <div class="status-card status-card-loading">
                  <div class="status-head">
                    <span class="status-title">后端已启动</span>
                    <span class="status-percent">模型加载中</span>
                  </div>
                  <div class="status-meter">
                    <div class="status-meter-fill" style="width:55%"></div>
                  </div>
                  <div class="status-foot">本地服务已拉起，正在初始化模型</div>
                </div>
                """,
            )

        response = requests.get(
            f"http://{SELFHOSTED_HOST}:{SELFHOSTED_PORT}/status",
            timeout=3,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        payload = response.json()
        loading = bool(payload.get("loading"))
        loaded = bool(payload.get("loaded"))
        if auto_start and not loaded and not loading:
            try:
                requests.post(
                    f"http://{SELFHOSTED_HOST}:{SELFHOSTED_PORT}/warmup",
                    timeout=3,
                    proxies={"http": None, "https": None},
                )
                loading = True
            except Exception:
                pass

        if loaded:
            banner = """
            <div class="status-card status-card-ok">
              <div class="status-head">
                <span class="status-title">后端已就绪</span>
                <span class="status-percent">Ready</span>
              </div>
              <div class="status-meter">
                <div class="status-meter-fill" style="width:100%"></div>
              </div>
              <div class="status-foot">模型已加载完成，可以开始识别</div>
            </div>
            """
        elif loading:
            banner = """
            <div class="status-card status-card-loading">
              <div class="status-head">
                <span class="status-title">后端已启动</span>
                <span class="status-percent">模型加载中</span>
              </div>
              <div class="status-meter">
                <div class="status-meter-fill" style="width:55%"></div>
              </div>
              <div class="status-foot">模型正在首次加载，首次可能需要一些时间</div>
            </div>
            """
        else:
            banner = """
            <div class="status-card">
              <div class="status-head">
                <span class="status-title">后端在线</span>
                <span class="status-percent">等待唤醒</span>
              </div>
              <div class="status-meter">
                <div class="status-meter-fill" style="width:25%"></div>
              </div>
              <div class="status-foot">服务已可访问，等待模型加载</div>
            </div>
            """
        lines = [
            "后端: selfhosted",
            f"状态: {payload.get('status')}",
            f"模型: {payload.get('model_id')}",
            f"已加载: {payload.get('loaded')}",
            f"加载中: {payload.get('loading')}",
            f"CUDA 可用: {payload.get('cuda_available')}",
            f"GPU: {payload.get('gpu_name') or 'N/A'}",
            f"Device: {payload.get('device')}",
        ]
        return "\n".join(lines), banner
    except Exception as exc:
        if auto_start:
            spawn_selfhosted_server()
            return (
                "后端: selfhosted\n状态: 启动中\n详情: 正在拉起本地服务...",
                """
                <div class="status-card status-card-loading">
                  <div class="status-head">
                    <span class="status-title">后端已启动</span>
                    <span class="status-percent">模型加载中</span>
                  </div>
                  <div class="status-meter">
                    <div class="status-meter-fill" style="width:55%"></div>
                  </div>
                  <div class="status-foot">本地服务已拉起，正在初始化模型</div>
                </div>
                """,
            )
        return (
            f"后端: selfhosted\n状态: 未连接\n详情: {exc}",
            """
            <div class="status-card status-card-warn">
              <div class="status-head">
                <span class="status-title">后端未连接</span>
                <span class="status-percent">检查服务</span>
              </div>
              <div class="status-meter">
                <div class="status-meter-fill" style="width:0%"></div>
              </div>
              <div class="status-foot">请检查本地服务是否正常启动</div>
            </div>
            """,
        )


def update_mode_visibility(mode_value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    show_api = mode_value == "maas"
    return (
        gr.update(visible=show_api),
        gr.update(visible=show_api),
    )


def process_selfhosted_rendered_pages(
    *,
    parser_kwargs: dict[str, Any],
    file_path: str,
    page_images: list[str],
    pages_total: int,
    page_start_number: int,
    page_end_number: int | None,
    file_index: int,
    task_id: str,
    event_queue: queue.Queue,
    state: dict[str, Any],
    output_root: Path,
    save_layout_visualization: bool,
    progress,
) -> tuple[Path, dict[str, Any], list[Path]]:
    saved_dir = expected_saved_dir(output_root, file_path)
    saved_dir.mkdir(parents=True, exist_ok=True)

    page_markdown_parts: list[str] = []
    page_json_payloads: list[dict[str, Any]] = []
    page_failures: list[dict[str, Any]] = []
    download_paths: list[Path] = []

    degraded_caps = (None, 2048, 1024)
    completed_pages = 0

    def push_event(event_type: str, **payload: Any) -> None:
        event_queue.put(
            {
                "type": event_type,
                "index": file_index,
                "file_path": file_path,
                **payload,
            }
        )

    for page_offset, page_image in enumerate(page_images):
        actual_page = page_start_number + page_offset
        page_success = False
        last_error = ""
        max_attempts = len(degraded_caps)

        for attempt_no, cap in enumerate(degraded_caps, start=1):
            push_event(
                "page_attempt",
                page=actual_page,
                attempt=attempt_no,
                max_attempts=max_attempts,
                max_tokens_cap=cap,
                pages_done=completed_pages,
                pages_total=pages_total,
            )
            try:
                with GlmOcr(**parser_kwargs) as page_parser:
                    with install_selfhosted_progress_hooks(
                        page_parser,
                        event_queue,
                        task_id,
                        file_index,
                        file_path,
                        pages_total,
                        initial_pages_done=completed_pages,
                        initial_pages_loaded=completed_pages,
                        initial_layout_pages_done=completed_pages,
                        initial_page_hint=actual_page,
                    ) as progress_state:
                        parse_watch_stop = threading.Event()

                        def parse_stage_watcher() -> None:
                            milestones = (
                                (0, "parse_prepare_start"),
                                (2, "pdf_opened"),
                                (6, "page_render_start"),
                                (10, "first_page_wait"),
                            )
                            emitted: set[str] = set()
                            while not parse_watch_stop.wait(1.0):
                                snapshot = snapshot_progress_state(progress_state)
                                if (
                                    snapshot.get("pages_loaded", 0) > completed_pages
                                    or snapshot.get("layout_pages_done", 0) > completed_pages
                                    or snapshot.get("regions_done", 0) > 0
                                    or snapshot.get("parse_done")
                                    or snapshot.get("save_done")
                                    or snapshot.get("failure_reason")
                                ):
                                    break
                                elapsed_wait = int(time.time() - snapshot.get("started_at", time.time()))
                                for threshold, phase_name in milestones:
                                    if elapsed_wait < threshold or phase_name in emitted:
                                        continue
                                    emitted.add(phase_name)
                                    stage_snapshot = update_progress_state(
                                        progress_state,
                                        phase=phase_name,
                                        current_page_hint=actual_page,
                                        last_backend_activity_at=time.time(),
                                    )
                                    push_event(
                                        "stage",
                                        phase_name=phase_name,
                                        progress_state=stage_snapshot,
                                    )

                        initial_stage_snapshot = update_progress_state(
                            progress_state,
                            phase="parse_prepare_start",
                            current_page_hint=actual_page,
                            last_backend_activity_at=time.time(),
                        )
                        push_event(
                            "stage",
                            phase_name="parse_prepare_start",
                            progress_state=initial_stage_snapshot,
                        )
                        parse_watcher = threading.Thread(target=parse_stage_watcher, daemon=True)
                        parse_watcher.start()
                        try:
                            result = page_parser.parse([page_image], save_layout_visualization=save_layout_visualization)
                        finally:
                            parse_watch_stop.set()
                            parse_watcher.join(timeout=0.2)
                        parse_snapshot = update_progress_state(progress_state, parse_done=True)
                        push_event("parse_done", progress_state=parse_snapshot)

                page_result = result[0] if isinstance(result, list) else result
                page_output_dir = saved_dir / f"page_{actual_page:04d}"
                page_output_dir.mkdir(parents=True, exist_ok=True)
                page_result.save(
                    output_dir=page_output_dir,
                    save_layout_visualization=save_layout_visualization,
                )
                saved_files = collect_saved_artifacts(page_output_dir)
                download_paths.extend(saved_files)
                result_dict = page_result.to_dict()
                page_markdown_parts.append((result_dict.get("markdown_result") or "").strip())
                page_json_payloads.append(
                    {
                        "page": actual_page,
                        "saved_dir": str(page_output_dir),
                        "result": result_dict,
                    }
                )
                completed_pages += 1
                push_event(
                    "save_done",
                    progress_state={
                        "pages_total": pages_total,
                        "pages_done": completed_pages,
                        "pages_loaded": completed_pages,
                        "layout_pages_done": completed_pages,
                        "regions_total": 0,
                        "regions_done": 0,
                        "parse_done": True,
                        "save_done": True,
                        "phase": "finished",
                        "current_page_hint": min(page_start_number + page_offset + 1, page_start_number + pages_total - 1),
                        "started_at": time.time(),
                        "last_event_at": time.time(),
                    },
                    saved_dir=str(page_output_dir),
                )
                page_success = True
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = is_selfhosted_timeout_error(exc) or "127.0.0.1:5002" in last_error
                if attempt_no >= max_attempts or not retryable:
                    push_event(
                        "page_failure",
                        page=actual_page,
                        attempt=attempt_no,
                        max_attempts=max_attempts,
                        error=last_error,
                        pages_done=completed_pages,
                        pages_total=pages_total,
                    )
                    page_failures.append(
                        {
                            "page": actual_page,
                            "attempts": attempt_no,
                            "error": last_error,
                            "max_tokens_cap": cap,
                        }
                    )
                    break

                push_event(
                    "page_retry",
                    page=actual_page,
                    attempt=attempt_no + 1,
                    max_attempts=max_attempts,
                    error=last_error,
                    max_tokens_cap=degraded_caps[attempt_no],
                    pages_done=completed_pages,
                    pages_total=pages_total,
                )
                push_event(
                    "service_restart",
                    page=actual_page,
                    attempt=attempt_no + 1,
                    max_attempts=max_attempts,
                    reason=last_error,
                    max_tokens_cap=degraded_caps[attempt_no],
                    pages_done=completed_pages,
                    pages_total=pages_total,
                )
                restart_selfhosted_server(
                    progress,
                    task_id=task_id,
                    state=state,
                    reason=f"page={actual_page} retry={attempt_no + 1}",
                    max_new_tokens_cap=degraded_caps[attempt_no],
                )

        if not page_success:
            continue

    markdown_path, named_markdown_path, report_path = write_selfhosted_partial_report(
        saved_dir,
        file_path=file_path,
        page_markdown_parts=page_markdown_parts,
        page_json_payloads=page_json_payloads,
        page_failures=page_failures,
        page_start_number=page_start_number,
        page_end_number=page_end_number,
    )
    download_paths.extend([markdown_path, named_markdown_path, report_path])

    aggregate_result = {
        "markdown_result": build_combined_markdown(page_markdown_parts),
        "json_result": {
            "completed_pages": [item["page"] for item in page_json_payloads],
            "failed_pages": page_failures,
            "pages": page_json_payloads,
        },
        "usage": None,
        "error": None if not page_failures else f"{len(page_failures)} pages failed after retries",
    }
    if not page_json_payloads:
        raise RuntimeError(
            f"Selfhosted page processing produced no successful pages. Last error: {last_error}"
        )
    return saved_dir, aggregate_result, download_paths


def run_ocr(
    files: list[Any],
    mode: str,
    api_key: str,
    env_file: str,
    config_path: str,
    output_dir: str,
    save_layout_visualization: bool,
    start_page: str,
    end_page: str,
    progress=gr.Progress(track_tqdm=False),
):
    task_id = build_task_id()
    task_started_at = time.time()
    try:
        append_app_log("run_ocr precheck start")
        api_key_text = normalize_optional_text(api_key)
        env_file_text = normalize_optional_text(env_file)
        config_path_text = normalize_optional_text(config_path)
        output_dir_text = normalize_optional_text(output_dir)
        start_page_text = normalize_optional_text(start_page)
        end_page_text = normalize_optional_text(end_page)

        paths = collect_paths(files)
        start_page_id = parse_optional_int(start_page_text, "PDF 起始页")
        end_page_id = parse_optional_int(end_page_text, "PDF 结束页")
        if start_page_id and end_page_id and start_page_id > end_page_id:
            raise gr.Error("PDF 起始页不能大于结束页。")
        workload = estimate_units(paths, start_page_id, end_page_id)
        output_root = Path(output_dir_text or DEFAULT_OUTPUT_DIR).resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        if (
            mode == "maas"
            and not api_key_text
            and not env_file_text
            and not os.environ.get("GLMOCR_API_KEY")
        ):
            raise gr.Error("MaaS 模式需要 API Key。请填写 API Key、设置环境变量，或提供 .env 文件。")

        start_page_id = parse_optional_int(start_page_text, "PDF 起始页")
        end_page_id = parse_optional_int(end_page_text, "PDF 结束页")
        if start_page_id and end_page_id and start_page_id > end_page_id:
            raise gr.Error("PDF 起始页不能大于结束页。")
    except Exception as exc:
        tb = traceback.format_exc()
        append_app_log(tb)
        error_msg = f"{type(exc).__name__}: {exc}"
        failure_log = append_runtime_log(
            "ERROR",
            "task end",
            task_id=task_id,
            status="failed",
            elapsed=format_elapsed_compact(time.time() - task_started_at),
            error=error_msg,
            include_in_gui=False,
        )
        yield build_error_outputs(error_msg, f"{failure_log}\n{tb}")
        return

    try:
        parser_kwargs: dict[str, Any] = {
            "config_path": config_path_text or None,
            "mode": mode,
            "env_file": env_file_text or None,
        }
        if mode == "maas" and api_key_text:
            parser_kwargs["api_key"] = api_key_text

        event_queue: queue.Queue = queue.Queue()
        state = {
            "task_id": task_id,
            "task_started_at": task_started_at,
            "summaries": [],
            "markdown_parts": [],
            "json_payloads": [],
            "log_lines": [],
            "download_paths": [],
            "error": None,
            "done": False,
            "saved_count": 0,
            "active_progress": {},
            "current_progress": None,
            "current_file_index": None,
        }

        def worker() -> None:
            staging_dir: Path | None = None
            task_end_logged = False

            def log_task_end(status: str, error: str | None = None) -> None:
                nonlocal task_end_logged
                if task_end_logged:
                    return
                task_end_logged = True
                fields: dict[str, Any] = {
                    "task_id": task_id,
                    "status": status,
                    "elapsed": format_elapsed_compact(time.time() - task_started_at),
                }
                if error:
                    fields["error"] = error
                append_runtime_log("INFO" if status == "success" else "ERROR", "task end", **fields)

            try:
                total = len(paths)
                total_items = int(sum(units for _, units in workload))
                total_pages = total_items if total == 1 and Path(paths[0]).suffix.lower() == ".pdf" else None
                append_runtime_log(
                    "INFO",
                    "task start",
                    state=state,
                    task_id=task_id,
                    mode=mode,
                    input=summarize_task_inputs(paths),
                    total_files=total,
                    total_items=total_items,
                    total_pages=total_pages,
                )
                if mode == "selfhosted":
                    append_app_log("selfhosted preflight start")
                    ensure_selfhosted_server(progress, task_id=task_id, state=state)
                    parser_kwargs["ocr_api_host"] = SELFHOSTED_HOST
                    parser_kwargs["ocr_api_port"] = SELFHOSTED_PORT
                append_app_log(f"run_ocr start mode={mode} files={len(paths)} output={output_root}")
                parser_inputs: list[tuple[str, str | list[str], dict[str, Any]]] = []
                if mode == "selfhosted":
                    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
                    staging_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(STAGING_ROOT)))
                    append_app_log(f"staging dir created {staging_dir}")

                for index, file_path in enumerate(paths, start=1):
                    parser_input_path: str | list[str] = file_path
                    parser_meta: dict[str, Any] = {}
                    if mode == "selfhosted" and staging_dir is not None:
                        suffix = Path(file_path).suffix.lower()
                        if suffix == ".pdf":
                            _, actual_start_page, actual_end_page, _ = resolve_pdf_page_range(
                                Path(file_path),
                                start_page_id,
                                end_page_id,
                            )
                            parser_input_path, selected_pages = render_pdf_range_to_images(
                                file_path,
                                staging_dir,
                                index,
                                start_page_id,
                                end_page_id,
                            )
                            parser_meta["page_start_number"] = actual_start_page
                            parser_meta["page_end_number"] = actual_end_page
                            if start_page_id is not None or end_page_id is not None:
                                state["log_lines"].append(
                                    f"[{index}/{total}] 已按页范围渲染 PDF：{Path(file_path).name} -> {selected_pages} 页"
                                )
                                append_app_log(
                                    f"rendered pdf range original={file_path} staged_pages={selected_pages} first_image={parser_input_path[0] if parser_input_path else ''}"
                                )
                            else:
                                state["log_lines"].append(
                                    f"[{index}/{total}] 已为 selfhosted 模式逐页渲染 PDF：{Path(file_path).name} -> {selected_pages} 页"
                                )
                                append_app_log(
                                    f"rendered pdf full original={file_path} staged_pages={selected_pages} first_image={parser_input_path[0] if parser_input_path else ''}"
                                )
                    if (
                        mode == "selfhosted"
                        and staging_dir is not None
                        and isinstance(parser_input_path, str)
                        and needs_ascii_staging(file_path)
                    ):
                        parser_input_path = stage_input_for_parser(file_path, staging_dir, index)
                        state["log_lines"].append(
                            f"[{index}/{total}] 检测到非 ASCII 路径，已使用临时英文文件名处理"
                        )
                        append_app_log(
                            f"staged input original={file_path} staged={parser_input_path}"
                        )
                    parser_inputs.append((file_path, parser_input_path, parser_meta))

                append_app_log("parser creation start")
                append_app_log("parser creation done")
                for index, (file_path, parser_input_path, parser_meta) in enumerate(parser_inputs, start=1):
                        pages_total = workload[index - 1][1]
                        event_queue.put(
                            {
                                "type": "file_start",
                                "index": index,
                                "total": total,
                                "file_path": file_path,
                                "label": Path(file_path).name,
                                "pages_total": pages_total,
                            }
                        )
                        append_runtime_log(
                            "INFO",
                            "task item start",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            input=Path(file_path).name,
                            total_items=pages_total,
                            total_pages=pages_total if Path(file_path).suffix.lower() == ".pdf" else None,
                        )
                        append_runtime_log(
                            "INFO",
                            "parse start",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            parser="GlmOcr",
                            mode=mode,
                            file=Path(file_path).name,
                            input=(
                                f"{len(parser_input_path)} rendered pages"
                                if isinstance(parser_input_path, list)
                                else Path(parser_input_path).name
                            ),
                            total_pages=pages_total,
                        )
                        append_app_log(f"parse start file={file_path} parser_input={parser_input_path}")

                        parse_kwargs = {
                            "save_layout_visualization": save_layout_visualization,
                        }
                        if mode == "maas":
                            if start_page_id is not None:
                                parse_kwargs["start_page_id"] = start_page_id
                            if end_page_id is not None:
                                parse_kwargs["end_page_id"] = end_page_id

                        if mode == "selfhosted" and isinstance(parser_input_path, list):
                            saved_dir, result_dict, page_downloads = process_selfhosted_rendered_pages(
                                parser_kwargs=parser_kwargs,
                                file_path=file_path,
                                page_images=parser_input_path,
                                pages_total=pages_total,
                                page_start_number=int(parser_meta.get("page_start_number", 1)),
                                page_end_number=(
                                    int(parser_meta["page_end_number"])
                                    if parser_meta.get("page_end_number") is not None
                                    else None
                                ),
                                file_index=index,
                                task_id=task_id,
                                event_queue=event_queue,
                                state=state,
                                output_root=output_root,
                                save_layout_visualization=save_layout_visualization,
                                progress=progress,
                            )
                            append_runtime_log(
                                "INFO" if not result_dict.get("error") else "WARN",
                                "parse end",
                                state=state,
                                task_id=task_id,
                                item=index,
                                total=total,
                                file=Path(file_path).name,
                                total_pages=pages_total,
                                elapsed=format_elapsed_compact(time.time() - task_started_at),
                                failed_pages=len(result_dict.get("json_result", {}).get("failed_pages", [])),
                            )
                            append_runtime_log(
                                "INFO" if not result_dict.get("error") else "WARN",
                                "save done",
                                state=state,
                                task_id=task_id,
                                item=index,
                                total=total,
                                file=Path(file_path).name,
                                saved_dir=str(saved_dir),
                            )
                            state["summaries"].append(build_summary(file_path, saved_dir, result_dict))
                            state["markdown_parts"].append(
                                f"# {Path(file_path).name}\n\n{result_dict.get('markdown_result') or ''}".strip()
                            )
                            state["json_payloads"].append(
                                {
                                    "input_file": file_path,
                                    "saved_dir": str(saved_dir),
                                    "result": result_dict,
                                }
                            )
                            state["log_lines"].append(f"[{index}/{total}] Saved output: {saved_dir}")
                            state["saved_count"] += 1
                            for child in page_downloads:
                                state["download_paths"].append(str(child))
                            append_runtime_log(
                                "INFO" if not result_dict.get("error") else "WARN",
                                "task item end",
                                state=state,
                                task_id=task_id,
                                item=index,
                                total=total,
                                status="success" if not result_dict.get("error") else "partial_failed",
                                output=saved_dir,
                            )
                            event_queue.put(
                                {
                                    "type": "file_done",
                                    "index": index,
                                    "total": total,
                                    "file_path": file_path,
                                    "saved_dir": str(saved_dir),
                                }
                            )
                            continue

                        with GlmOcr(**parser_kwargs) as parser:
                            if mode == "selfhosted":
                                with install_selfhosted_progress_hooks(
                                    parser,
                                    event_queue,
                                    task_id,
                                    index,
                                    file_path,
                                    pages_total,
                                ) as progress_state:
                                    parse_watch_stop = threading.Event()

                                    def parse_stage_watcher() -> None:
                                        milestones = (
                                            (0, "parse_prepare_start"),
                                            (2, "pdf_opened"),
                                            (6, "page_render_start"),
                                            (10, "first_page_wait"),
                                        )
                                        emitted: set[str] = set()
                                        while not parse_watch_stop.wait(1.0):
                                            snapshot = snapshot_progress_state(progress_state)
                                            if (
                                                snapshot.get("pages_loaded", 0) > 0
                                                or snapshot.get("layout_pages_done", 0) > 0
                                                or snapshot.get("regions_done", 0) > 0
                                                or snapshot.get("parse_done")
                                                or snapshot.get("save_done")
                                                or snapshot.get("failure_reason")
                                            ):
                                                break
                                            elapsed_wait = int(time.time() - snapshot.get("started_at", task_started_at))
                                            for threshold, phase_name in milestones:
                                                if elapsed_wait < threshold or phase_name in emitted:
                                                    continue
                                                emitted.add(phase_name)
                                                stage_snapshot = update_progress_state(
                                                    progress_state,
                                                    phase=phase_name,
                                                    last_backend_activity_at=time.time(),
                                                )
                                                event_queue.put(
                                                    {
                                                        "type": "stage",
                                                        "index": index,
                                                        "file_path": file_path,
                                                        "phase_name": phase_name,
                                                        "progress_state": stage_snapshot,
                                                    }
                                                )

                                    initial_stage_snapshot = update_progress_state(
                                        progress_state,
                                        phase="parse_prepare_start",
                                        last_backend_activity_at=time.time(),
                                    )
                                    event_queue.put(
                                        {
                                            "type": "stage",
                                            "index": index,
                                            "file_path": file_path,
                                            "phase_name": "parse_prepare_start",
                                            "progress_state": initial_stage_snapshot,
                                        }
                                    )
                                    parse_watcher = threading.Thread(
                                        target=parse_stage_watcher,
                                        daemon=True,
                                    )
                                    parse_watcher.start()
                                    try:
                                        result = parser.parse(parser_input_path, **parse_kwargs)
                                    finally:
                                        parse_watch_stop.set()
                                        parse_watcher.join(timeout=0.2)
                                    if progress_state is not None:
                                        parse_snapshot = update_progress_state(progress_state, parse_done=True)
                                        event_queue.put(
                                            {
                                                "type": "parse_done",
                                                "index": index,
                                                "file_path": file_path,
                                                "progress_state": parse_snapshot,
                                            }
                                        )
                            else:
                                result = parser.parse(parser_input_path, **parse_kwargs)
                        append_runtime_log(
                            "INFO",
                            "parse end",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            file=Path(file_path).name,
                            total_pages=pages_total,
                            elapsed=format_elapsed_compact(time.time() - task_started_at),
                        )
                        if getattr(result, "original_images", None):
                            result.original_images = [file_path]

                        append_runtime_log(
                            "INFO",
                            "save start",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            file=Path(file_path).name,
                            output_dir=str(output_root),
                        )
                        append_app_log(f"result save start file={file_path}")
                        result.save(
                            output_dir=output_root,
                            save_layout_visualization=save_layout_visualization,
                        )

                        saved_dir = resolve_saved_dir(output_root, file_path)
                        if not saved_dir.exists():
                            raise RuntimeError(
                                f"Output directory was not created after save: {saved_dir}"
                            )
                        saved_files = collect_saved_artifacts(saved_dir)
                        if not any(path.suffix.lower() in {".json", ".md"} for path in saved_files):
                            raise RuntimeError(
                                f"No JSON or Markdown output was produced in: {saved_dir}"
                            )
                        append_runtime_log(
                            "INFO",
                            "save done",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            file=Path(file_path).name,
                            saved_dir=str(saved_dir),
                        )
                        result_dict = result.to_dict()
                        state["summaries"].append(build_summary(file_path, saved_dir, result_dict))
                        state["markdown_parts"].append(
                            f"# {Path(file_path).name}\n\n{result.markdown_result or ''}".strip()
                        )
                        state["json_payloads"].append(
                            {
                                "input_file": file_path,
                                "saved_dir": str(saved_dir),
                                "result": result_dict,
                            }
                        )
                        state["log_lines"].append(f"[{index}/{total}] Saved output: {saved_dir}")
                        append_app_log(f"result save done file={file_path} saved_dir={saved_dir}")
                        append_runtime_log(
                            "INFO",
                            "task item end",
                            state=state,
                            task_id=task_id,
                            item=index,
                            total=total,
                            status="success",
                            output=saved_dir,
                        )
                        state["saved_count"] += 1
                        if mode == "selfhosted":
                            progress_snapshot = state["active_progress"].get(index, {})
                            event_queue.put(
                                {
                                    "type": "save_done",
                                    "index": index,
                                    "file_path": file_path,
                                    "progress_state": {
                                        **progress_snapshot,
                                        "pages_total": pages_total,
                                        "parse_done": True,
                                        "save_done": True,
                                    },
                                    "saved_dir": str(saved_dir),
                                }
                            )

                        for child in saved_files:
                            state["download_paths"].append(str(child))

                        event_queue.put(
                            {
                                "type": "file_done",
                                "index": index,
                                "total": total,
                                "file_path": file_path,
                                "saved_dir": str(saved_dir),
                            }
                        )
                log_task_end("success")
                append_app_log("run_ocr finished successfully")
            except MissingApiKeyError as exc:
                state["error"] = f"缺少 API Key: {exc}"
                tb = traceback.format_exc()
                state["log_lines"].append(tb)
                append_app_log(tb)
                log_task_end("failed", state["error"])
            except Exception as exc:
                state["error"] = summarize_task_failure(exc, mode)
                tb = traceback.format_exc()
                state["log_lines"].append(tb)
                append_app_log(tb)
                if mode == "selfhosted" and is_selfhosted_timeout_error(exc):
                    append_runtime_log(
                        "ERROR",
                        "backend timeout",
                        state=state,
                        task_id=task_id,
                        service=f"{SELFHOSTED_HOST}:{SELFHOSTED_PORT}",
                        phase="ocr_request",
                        timeout="300s",
                        error=state["error"],
                    )
                log_task_end("failed", state["error"])
            finally:
                if staging_dir is not None:
                    try:
                        shutil.rmtree(staging_dir, ignore_errors=True)
                    except Exception:
                        pass
                state["done"] = True
                event_queue.put({"type": "done"})

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()

        total_units = sum(units for _, units in workload)
        completed_units = 0.0
        total_elapsed_start = time.time()
        current_label = "当前进度：等待任务开始"
        current_stage = "当前阶段：准备解析任务"
        total_units = max(1.0, float(total_units))

        initial_progress = render_progress(
            current_label,
            0.0,
            "剩余时间：计算中",
            current_stage,
        )
        yield "", "", "", "", [], initial_progress

        while True:
            while True:
                try:
                    event = event_queue.get_nowait()
                except queue.Empty:
                    break

                if event["type"] == "file_start":
                    state["current_file_index"] = event["index"]
                    state["active_progress"][event["index"]] = {
                        "pages_total": event.get("pages_total", workload[event["index"] - 1][1]),
                        "pages_done": 0,
                        "pages_loaded": 0,
                        "layout_pages_done": 0,
                        "regions_total": 0,
                        "regions_done": 0,
                        "parse_done": False,
                        "save_done": False,
                        "phase": "preparing",
                        "started_at": time.time(),
                        "last_event_at": time.time(),
                    }
                    state["current_progress"] = state["active_progress"][event["index"]]
                    current_label, current_stage, _, _, _ = describe_selfhosted_progress(
                        state["current_progress"]
                    )
                    state["log_lines"].append(
                        f"[{event['index']}/{event['total']}] 开始处理 {event['file_path']}"
                    )
                elif event["type"] == "stage":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    append_runtime_log(
                        "INFO",
                        "stage",
                        state=state,
                        task_id=task_id,
                        phase=phase,
                        current=f"{current_count}",
                        total=f"{total_count}",
                    )
                elif event["type"] == "backend_wait":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    wait_level = "WARN" if int(event.get("elapsed_wait", 0)) >= 60 else "INFO"
                    append_runtime_log(
                        wait_level,
                        "backend wait",
                        state=state,
                        task_id=task_id,
                        service=event.get("service"),
                        phase=event.get("phase_name", phase),
                        request_id=event.get("request_id"),
                        page=event.get("page"),
                        region=event.get("region"),
                        current=f"{current_count}",
                        total=f"{total_count}",
                        elapsed_wait=f"{int(event.get('elapsed_wait', 0))}s",
                    )
                elif event["type"] == "backend_timeout":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    append_runtime_log(
                        "ERROR",
                        "backend timeout",
                        state=state,
                        task_id=task_id,
                        service=event.get("service"),
                        phase=event.get("phase_name", phase),
                        request_id=event.get("request_id"),
                        page=event.get("page"),
                        region=event.get("region"),
                        current=f"{current_count}",
                        total=f"{total_count}",
                        timeout=f"{event.get('timeout_seconds')}s",
                        error=event.get("error"),
                    )
                elif event["type"] == "backend_failure":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    append_runtime_log(
                        "ERROR",
                        "backend failure",
                        state=state,
                        task_id=task_id,
                        service=event.get("service"),
                        phase=event.get("phase_name", phase),
                        request_id=event.get("request_id"),
                        page=event.get("page"),
                        region=event.get("region"),
                        current=f"{current_count}",
                        total=f"{total_count}",
                        error=event.get("error"),
                    )
                elif event["type"] == "page_attempt":
                    snapshot = dict(state["active_progress"].get(event["index"], {}) or {})
                    snapshot.update(
                        {
                            "pages_total": event.get("pages_total", workload[event["index"] - 1][1]),
                            "pages_done": event.get("pages_done", snapshot.get("pages_done", 0)),
                            "phase": "running",
                            "current_page_hint": event.get("page"),
                            "last_event_at": time.time(),
                        }
                    )
                    state["active_progress"][event["index"]] = snapshot
                    state["current_progress"] = snapshot
                    current_label = f"当前进度：{snapshot.get('pages_done', 0)} / {snapshot.get('pages_total', 1)} 页"
                    current_stage = (
                        f"当前阶段：正在处理第 {event.get('page')} 页"
                        if int(event.get("attempt", 1)) == 1
                        else f"当前阶段：正在重试第 {event.get('page')} 页（第 {event.get('attempt')}/{event.get('max_attempts')} 次）"
                    )
                    append_runtime_log(
                        "INFO",
                        "page attempt",
                        state=state,
                        task_id=task_id,
                        page=event.get("page"),
                        attempt=event.get("attempt"),
                        max_attempts=event.get("max_attempts"),
                        max_tokens_cap=event.get("max_tokens_cap"),
                    )
                elif event["type"] == "page_retry":
                    snapshot = dict(state["active_progress"].get(event["index"], {}) or {})
                    snapshot.update(
                        {
                            "pages_total": event.get("pages_total", workload[event["index"] - 1][1]),
                            "pages_done": event.get("pages_done", snapshot.get("pages_done", 0)),
                            "phase": "retrying",
                            "current_page_hint": event.get("page"),
                            "failure_reason": event.get("error"),
                            "last_event_at": time.time(),
                        }
                    )
                    state["active_progress"][event["index"]] = snapshot
                    state["current_progress"] = snapshot
                    current_label, _, _, _, _ = describe_selfhosted_progress(snapshot)
                    current_stage = f"当前阶段：正在重试第 {event.get('page')} 页（第 {event.get('attempt')}/{event.get('max_attempts')} 次）"
                    append_runtime_log(
                        "WARN",
                        "page retry",
                        state=state,
                        task_id=task_id,
                        page=event.get("page"),
                        attempt=event.get("attempt"),
                        max_attempts=event.get("max_attempts"),
                        max_tokens_cap=event.get("max_tokens_cap"),
                        error=event.get("error"),
                    )
                elif event["type"] == "service_restart":
                    snapshot = dict(state["active_progress"].get(event["index"], {}) or {})
                    snapshot.update(
                        {
                            "pages_total": event.get("pages_total", workload[event["index"] - 1][1]),
                            "pages_done": event.get("pages_done", snapshot.get("pages_done", 0)),
                            "phase": "restarting_service",
                            "current_page_hint": event.get("page"),
                            "last_event_at": time.time(),
                        }
                    )
                    state["active_progress"][event["index"]] = snapshot
                    state["current_progress"] = snapshot
                    current_label, _, _, _, _ = describe_selfhosted_progress(snapshot)
                    current_stage = (
                        f"当前阶段：正在重启本地 OCR 服务并重试第 {event.get('page')} 页"
                    )
                    append_runtime_log(
                        "WARN",
                        "service restart",
                        state=state,
                        task_id=task_id,
                        page=event.get("page"),
                        attempt=event.get("attempt"),
                        max_attempts=event.get("max_attempts"),
                        max_tokens_cap=event.get("max_tokens_cap"),
                        reason=event.get("reason"),
                    )
                elif event["type"] == "page_failure":
                    snapshot = dict(state["active_progress"].get(event["index"], {}) or {})
                    snapshot.update(
                        {
                            "pages_total": event.get("pages_total", workload[event["index"] - 1][1]),
                            "pages_done": event.get("pages_done", snapshot.get("pages_done", 0)),
                            "phase": "page_failed",
                            "current_page_hint": event.get("page"),
                            "failure_reason": event.get("error"),
                            "last_event_at": time.time(),
                        }
                    )
                    state["active_progress"][event["index"]] = snapshot
                    state["current_progress"] = snapshot
                    current_label, _, _, _, _ = describe_selfhosted_progress(snapshot)
                    current_stage = f"当前阶段：第 {event.get('page')} 页失败，继续后续页面"
                    append_runtime_log(
                        "ERROR",
                        "page failure",
                        state=state,
                        task_id=task_id,
                        page=event.get("page"),
                        attempt=event.get("attempt"),
                        max_attempts=event.get("max_attempts"),
                        error=event.get("error"),
                    )
                elif event["type"] == "page_loaded":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    state["log_lines"].append(
                        f"[{event['index']}/{len(paths)}] 页面加载 {snapshot.get('pages_loaded', 0)}/{snapshot.get('pages_total', 0)}"
                    )
                    if current_count == 1 or current_count % 25 == 0 or current_count == total_count:
                        current_file_units = float(workload[event["index"] - 1][1])
                        progress_percent = compute_selfhosted_file_fraction(snapshot) * 100.0
                        remaining_seconds = estimate_selfhosted_eta_seconds(
                            snapshot,
                            current_file_units,
                            total_units,
                            completed_units,
                            task_started_at,
                        )
                        stage_text = progress_stage_text(snapshot, phase)
                        append_runtime_log(
                            "INFO",
                            "progress",
                            state=state,
                            task_id=task_id,
                            phase=phase,
                            current=f"{current_count}",
                            total=f"{total_count}",
                            percent=f"{progress_percent:.1f}%",
                            stage=stage_text,
                            remaining=format_remaining_time(remaining_seconds),
                        )
                elif event["type"] == "layout_batch_done":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    state["log_lines"].append(
                        f"[{event['index']}/{len(paths)}] 版面分析完成批次：页 {snapshot.get('layout_pages_done', 0)}/{snapshot.get('pages_total', 0)}，regions={snapshot.get('regions_total', 0)}"
                    )
                    if current_count > 0:
                        current_file_units = float(workload[event["index"] - 1][1])
                        progress_percent = (current_count / max(1, total_count)) * 100.0
                        remaining_seconds = estimate_selfhosted_eta_seconds(
                            snapshot,
                            current_file_units,
                            total_units,
                            completed_units,
                            task_started_at,
                        )
                        append_runtime_log(
                            "INFO",
                            "progress",
                            state=state,
                            task_id=task_id,
                            phase=phase,
                            current=f"{current_count}",
                            total=f"{total_count}",
                            percent=f"{progress_percent:.1f}%",
                            stage=current_stage.replace("当前阶段：", ""),
                            remaining=format_remaining_time(remaining_seconds),
                        )
                elif event["type"] == "page_region_metrics":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    append_runtime_log(
                        "INFO",
                        "page metrics",
                        state=state,
                        task_id=task_id,
                        page=event.get("page"),
                        regions=event.get("regions"),
                    )
                    state["log_lines"].append(
                        f"[{event['index']}/{len(paths)}] 第 {event.get('page')} 页拆分 {event.get('regions')} 个 region"
                    )
                elif event["type"] == "region_done":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                elif event["type"] == "page_done":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    snapshot = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(snapshot)
                    state["log_lines"].append(
                        f"[{event['index']}/{len(paths)}] 页面完成 {current_count}/{total_count}"
                    )
                    current_file_units = float(workload[event["index"] - 1][1])
                    progress_percent = (current_count / max(1, total_count)) * 100.0
                    remaining_seconds = estimate_selfhosted_eta_seconds(
                        snapshot,
                        current_file_units,
                        total_units,
                        completed_units,
                        task_started_at,
                    )
                    append_runtime_log(
                        "INFO",
                        "progress",
                        state=state,
                        task_id=task_id,
                        phase=phase,
                        current=f"{current_count}",
                        total=f"{total_count}",
                        percent=f"{progress_percent:.1f}%",
                        stage=current_stage.replace("当前阶段：", ""),
                        remaining=format_remaining_time(remaining_seconds),
                    )
                elif event["type"] == "parse_done":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(event["progress_state"])
                    state["log_lines"].append(f"[{event['index']}/{len(paths)}] 解析完成，开始保存输出")
                    current_file_units = float(workload[event["index"] - 1][1])
                    progress_percent = (current_count / max(1, total_count)) * 100.0
                    remaining_seconds = estimate_selfhosted_eta_seconds(
                        event["progress_state"],
                        current_file_units,
                        total_units,
                        completed_units,
                        task_started_at,
                    )
                    stage_text = progress_stage_text(event["progress_state"], phase)
                    append_runtime_log(
                        "INFO",
                        "progress",
                        state=state,
                        task_id=task_id,
                        phase=phase,
                        current=f"{current_count}",
                        total=f"{total_count}",
                        percent=f"{progress_percent:.1f}%",
                        stage=current_stage.replace("当前阶段：", ""),
                        remaining=format_remaining_time(remaining_seconds),
                    )
                elif event["type"] == "save_done":
                    state["active_progress"][event["index"]] = event["progress_state"]
                    state["current_progress"] = event["progress_state"]
                    current_label, current_stage, phase, current_count, total_count = describe_selfhosted_progress(event["progress_state"])
                    state["log_lines"].append(f"[{event['index']}/{len(paths)}] 保存完成")
                    append_runtime_log(
                        "INFO",
                        "progress",
                        state=state,
                        task_id=task_id,
                        phase=phase,
                        current=f"{current_count}",
                        total=f"{total_count}",
                        percent="100.0%",
                        stage="当前阶段：已完成",
                        remaining="剩余时间：0 秒",
                    )
                elif event["type"] == "file_done":
                    completed_units += workload[event["index"] - 1][1]
                    state["active_progress"].pop(event["index"], None)
                    state["current_progress"] = None
                    state["current_file_index"] = None
                    current_label = f"当前进度：已完成 {event['index']}/{event['total']} 项"
                    current_stage = "当前阶段：等待下一个任务"
                elif event["type"] == "done":
                    pass

            elapsed = time.time() - total_elapsed_start
            if completed_units > 0:
                avg_seconds_per_unit = elapsed / completed_units
                remaining_units = max(0.0, total_units - completed_units)
                eta_seconds = remaining_units * avg_seconds_per_unit
            else:
                heuristic_per_unit = 5.0 if mode == "selfhosted" else 3.0
                eta_seconds = total_units * heuristic_per_unit

            if not state["done"]:
                if mode == "selfhosted" and state["current_progress"] is not None:
                    snapshot = snapshot_progress_state(state["current_progress"])
                    current_file_index = state["current_file_index"]
                    current_file_units = (
                        float(workload[current_file_index - 1][1])
                        if current_file_index is not None and 0 < current_file_index <= len(workload)
                        else 0.0
                    )
                    current_pages, current_total_pages = describe_selfhosted_page_counts(snapshot)
                    display_percent = (
                        (completed_units + float(current_pages)) / total_units
                    ) * 100.0
                    if state["saved_count"] < len(paths):
                        display_percent = min(display_percent, 99.0)
                    eta_seconds = estimate_selfhosted_eta_seconds(
                        snapshot,
                        current_file_units,
                        total_units,
                        completed_units,
                        task_started_at,
                    )
                    current_stage = progress_stage_text(snapshot, str(snapshot.get("phase") or "preparing"))
                    eta_text = format_remaining_time(eta_seconds)
                else:
                    smoothing_target = completed_units
                    if completed_units < total_units:
                        elapsed_units = min(total_units, max(completed_units, elapsed / max(eta_seconds, 1e-6) * total_units))
                        smoothing_target = max(completed_units, elapsed_units)
                    display_percent = (smoothing_target / total_units) * 100.0
                    if state["saved_count"] < len(paths):
                        display_percent = min(display_percent, 99.0)
                    current_stage = "当前阶段：正在处理任务"
                    eta_text = format_remaining_time(eta_seconds)
                progress_html = render_progress(current_label, display_percent, eta_text, current_stage)
                summary_text = "\n\n" + ("\n" + ("-" * 60) + "\n\n").join(state["summaries"]) if state["summaries"] else ""
                markdown_text = "\n\n".join(state["markdown_parts"])
                json_text = json.dumps(state["json_payloads"], ensure_ascii=False, indent=2)
                logs_text = "\n".join(state["log_lines"])
                yield summary_text.strip(), markdown_text, json_text, logs_text, state["download_paths"], progress_html
                time.sleep(0.8)
                continue

            if state["error"]:
                yield build_error_outputs(
                    state["error"],
                    "\n".join(state["log_lines"] or [state["error"]]),
                )
                break

            progress_html = render_progress("当前进度：已完成", 100.0, "剩余时间：0 秒", "当前阶段：已完成")
            summary_text = "\n\n" + ("\n" + ("-" * 60) + "\n\n").join(state["summaries"]) if state["summaries"] else ""
            markdown_text = "\n\n".join(state["markdown_parts"])
            json_text = json.dumps(state["json_payloads"], ensure_ascii=False, indent=2)
            logs_text = "\n".join(state["log_lines"])
            yield summary_text.strip(), markdown_text, json_text, logs_text, state["download_paths"], progress_html
            break
    except Exception as exc:
        tb = traceback.format_exc()
        append_app_log(tb)
        yield build_error_outputs(f"{type(exc).__name__}: {exc}", tb)


def build_app() -> gr.Blocks:
    css = """
    :root {
      --page-bg: linear-gradient(180deg, #f6f2e8 0%, #efe9db 100%);
      --panel-bg: rgba(255, 252, 245, 0.92);
      --accent: #8a3b12;
      --accent-2: #d98a2b;
      --text: #1f1c18;
      --border: rgba(85, 52, 31, 0.15);
    }
    .gradio-container {
      background: var(--page-bg);
      color: var(--text);
    }
    .app-shell {
      max-width: 1280px;
      margin: 0 auto;
    }
    .hero {
      background: radial-gradient(circle at top left, rgba(217, 138, 43, 0.22), transparent 40%),
                  linear-gradient(135deg, rgba(138, 59, 18, 0.08), rgba(255, 252, 245, 0.9));
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 24px;
      margin-bottom: 18px;
    }
    .panel {
      background: var(--panel-bg);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 8px;
    }
    .panel-left,
    .panel-right {
      background: rgba(255, 252, 245, 0.8);
      border: 1px solid rgba(138, 59, 18, 0.12);
      border-radius: 22px;
      box-shadow: 0 10px 30px rgba(44, 28, 15, 0.05);
      padding: 12px;
    }
    .surface-card {
      background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(250,246,238,0.98));
      border: 1px solid rgba(138, 59, 18, 0.10);
      border-radius: 16px;
      box-shadow: 0 6px 18px rgba(44, 28, 15, 0.04);
      overflow: hidden;
    }
    .surface-card :where(input, textarea, select) {
      border-radius: 12px !important;
    }
    .surface-card .wrap {
      border-radius: 16px;
    }
    .status-card {
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,243,234,0.96));
      border: 1px solid rgba(138, 59, 18, 0.18);
      border-radius: 16px;
      box-shadow: 0 8px 24px rgba(44, 28, 15, 0.06);
      padding: 14px 16px;
      margin-bottom: 12px;
    }
    .status-card-progress {
      background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(251,246,237,0.98));
    }
    .status-card-ok {
      border-color: rgba(35, 116, 57, 0.24);
      background: linear-gradient(180deg, rgba(236, 248, 239, 0.96), rgba(247, 252, 248, 0.96));
      color: #1f6b34;
    }
    .status-card-loading {
      border-color: rgba(178, 123, 30, 0.24);
      background: linear-gradient(180deg, rgba(255, 247, 224, 0.96), rgba(255, 251, 242, 0.96));
      color: #7a4b00;
    }
    .status-card-warn {
      border-color: rgba(183, 28, 28, 0.24);
      background: linear-gradient(180deg, rgba(252, 234, 234, 0.96), rgba(255, 247, 247, 0.96));
      color: #9b1c1c;
    }
    .status-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
    }
    .status-title {
      letter-spacing: 0.2px;
    }
    .status-percent {
      font-variant-numeric: tabular-nums;
      color: inherit;
    }
    .status-stage {
      margin-bottom: 8px;
      font-size: 12px;
      opacity: 0.82;
    }
    .status-meter {
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: rgba(138, 59, 18, 0.12);
      overflow: hidden;
    }
    .status-meter-fill {
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #8a3b12, #d98a2b);
      transition: width 0.25s ease;
    }
    .status-card-ok .status-meter-fill {
      background: linear-gradient(90deg, #2f8f4e, #74c98a);
    }
    .status-card-loading .status-meter-fill {
      background: linear-gradient(90deg, #b46a00, #e0a22f);
    }
    .status-card-warn .status-meter-fill {
      background: linear-gradient(90deg, #c63d3d, #ef7a7a);
    }
    .status-foot {
      margin-top: 8px;
      font-size: 12px;
      opacity: 0.8;
    }
    """

    with gr.Blocks(title="GLM OCR Web GUI") as app:
        with gr.Column(elem_classes=["app-shell"]):
            gr.HTML(
                """
                <div class="hero">
                  <h1 style="margin:0 0 8px 0;">GLM OCR Web GUI</h1>
                  <p style="margin:0;font-size:15px;">
                    本地 Windows OCR 界面，支持图片和 PDF。
                    可在 <code>selfhosted</code> 或 <code>maas</code> 模式下使用。
                  </p>
                </div>
                """
            )

            with gr.Row(equal_height=False):
                with gr.Column(scale=5, elem_classes=["panel-left"]):
                    backend_banner = gr.HTML(
                        value="""
                        <div style='padding:12px 14px;border-radius:12px;background:#f2f3f5;border:1px solid #d3d7de;color:#41464b;font-weight:600;'>
                          正在检测后端...
                        </div>
                        """
                    )
                    backend_status = gr.Textbox(label="后端状态", lines=7, value="后端: selfhosted\n状态: 初始化中", elem_classes=["surface-card"])
                    progress_html = gr.HTML(
                        value=render_progress(
                            "当前进度：等待任务开始",
                            0.0,
                            "剩余时间：计算中",
                            "当前阶段：准备解析任务",
                        )
                    )
                    files = gr.Files(
                        label="上传图片或 PDF",
                        file_count="multiple",
                        file_types=["image", ".pdf"],
                        elem_classes=["surface-card"],
                    )
                    with gr.Row():
                        mode = gr.Dropdown(
                            label="模式",
                            choices=["maas", "selfhosted"],
                            value="selfhosted",
                            elem_classes=["surface-card"],
                        )
                        save_layout_visualization = gr.Checkbox(
                            label="保存版面分析图",
                            value=True,
                        )
                    api_panel = gr.Column(visible=False, elem_classes=["surface-card"])
                    with api_panel:
                        api_key = gr.Textbox(
                            label="API Key",
                            type="password",
                            placeholder="MaaS 模式可填写；也可通过环境变量或 .env 提供",
                            elem_classes=["surface-card"],
                        )
                        with gr.Row():
                            env_file = gr.Textbox(label=".env 文件", placeholder="可选", elem_classes=["surface-card"])
                            config_path = gr.Textbox(label="YAML 配置文件", placeholder="可选", elem_classes=["surface-card"])
                    output_dir = gr.Textbox(
                        label="输出目录",
                        value=str(DEFAULT_OUTPUT_DIR),
                        elem_classes=["surface-card"],
                    )
                    with gr.Row():
                        start_page = gr.Textbox(label="PDF 起始页", placeholder="可选，1 开始", elem_classes=["surface-card"])
                        end_page = gr.Textbox(label="PDF 结束页", placeholder="可选，1 开始", elem_classes=["surface-card"])
                    run_button = gr.Button("开始识别", variant="primary")

                with gr.Column(scale=6, elem_classes=["panel-right"]):
                    summary = gr.Textbox(label="摘要", lines=10, elem_classes=["surface-card"])
                    with gr.Tabs():
                        with gr.Tab("Markdown"):
                            markdown = gr.Textbox(label="Markdown 结果", lines=18, elem_classes=["surface-card"])
                        with gr.Tab("JSON"):
                            json_output = gr.Code(label="JSON 结果", language="json", elem_classes=["surface-card"])
                        with gr.Tab("日志"):
                            logs = gr.Textbox(label="运行日志", lines=18, elem_classes=["surface-card"])
                    downloads = gr.Files(label="输出文件", elem_classes=["surface-card"])

            run_button.click(
                fn=run_ocr,
                inputs=[
                    files,
                    mode,
                    api_key,
                    env_file,
                    config_path,
                    output_dir,
                    save_layout_visualization,
                    start_page,
                    end_page,
                ],
                outputs=[summary, markdown, json_output, logs, downloads, progress_html],
            )
            mode.change(
                fn=update_mode_visibility,
                inputs=[mode],
                outputs=[api_key, api_panel],
            )
            app.load(
                fn=lambda: fetch_backend_status(auto_start=True),
                outputs=[backend_status, backend_banner],
            )
            timer = gr.Timer(2.0)
            timer.tick(
                fn=lambda: fetch_backend_status(auto_start=True),
                outputs=[backend_status, backend_banner],
            )

    app._codex_css = css
    return app


def _patch_gradio_startup_probe() -> None:
    original_get = gr_blocks.httpx.get

    def patched_get(*args, **kwargs):
        kwargs.setdefault("trust_env", False)
        return original_get(*args, **kwargs)

    gr_blocks.httpx.get = patched_get

    no_proxy_hosts = "127.0.0.1,localhost"
    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "").strip()
        if existing:
            if "127.0.0.1" not in existing and "localhost" not in existing:
                os.environ[key] = f"{existing},{no_proxy_hosts}"
        else:
            os.environ[key] = no_proxy_hosts


def main() -> None:
    app = build_app()
    _patch_gradio_startup_probe()
    port = find_available_port("127.0.0.1", 7860)
    print(f"GLM OCR Web GUI starting at http://127.0.0.1:{port}")
    app.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=True,
        theme=gr.themes.Soft(),
        css=getattr(app, "_codex_css", None),
        ssr_mode=False,
    )


if __name__ == "__main__":
    main()
