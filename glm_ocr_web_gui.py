import json
import os
import queue
import socket
import subprocess
import sys
import time
import threading
import traceback
from pathlib import Path
from typing import Any

import gradio as gr
import gradio.blocks as gr_blocks
import requests
import pypdfium2 as pdfium

from glmocr import GlmOcr
from glmocr.maas_client import MissingApiKeyError


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = APP_ROOT / "glm_ocr_outputs_web"
APP_LOG_FILE = APP_ROOT / "glm_ocr_web_gui.log"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".pdf"}
SELFHOSTED_HOST = "127.0.0.1"
SELFHOSTED_PORT = 5002
BAR_TEMPLATE = """
<div class="status-card status-card-progress">
  <div class="status-head">
    <span class="status-title">{label}</span>
    <span class="status-percent">{percent:.1f}%</span>
  </div>
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
    if hasattr(file_obj, "path") and getattr(file_obj, "path"):
        return str(file_obj.path)
    if hasattr(file_obj, "name"):
        return str(file_obj.name)
    if isinstance(file_obj, dict):
        for key in ("path", "name"):
            value = file_obj.get(key)
            if value:
                return str(value)
    raise ValueError(f"Unsupported upload payload: {type(file_obj)!r}")


def parse_optional_int(value: str | None, label: str) -> int | None:
    if value is None:
        return None
    value = value.strip()
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
        if not path.exists():
            raise gr.Error(f"上传文件不存在，请重新选择文件: {raw_path}")
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
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


def estimate_units(paths: list[str]) -> list[tuple[str, int]]:
    units: list[tuple[str, int]] = []
    for file_path in paths:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".pdf":
            units.append((file_path, max(1, count_pdf_pages(Path(file_path)))))
        else:
            units.append((file_path, 1))
    return units


def render_progress(label: str, percent: float, eta: str) -> str:
    return BAR_TEMPLATE.format(label=label, percent=max(0.0, min(100.0, percent)), eta=eta)


def append_app_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    APP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with APP_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "预计剩余时间: 计算中"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return "预计剩余时间: < 1 秒"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"预计剩余时间: {hours} 小时 {minutes} 分 {sec} 秒"
    if minutes:
        return f"预计剩余时间: {minutes} 分 {sec} 秒"
    return f"预计剩余时间: {sec} 秒"


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


def wait_for_local_server(host: str, port: int, timeout: int = 180) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(host, port):
            try:
                response = requests.get(
                    f"http://{host}:{port}/health",
                    timeout=2,
                    proxies={"http": None, "https": None},
                )
                if response.ok:
                    return
            except Exception:
                pass
        time.sleep(2)
    raise gr.Error(f"本地 GLM-OCR 服务未能在 {timeout} 秒内启动成功。")


def ensure_selfhosted_server(progress) -> None:
    if is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
        return

    progress(0, desc="启动本地 GLM-OCR 服务")
    spawn_selfhosted_server()
    wait_for_local_server(SELFHOSTED_HOST, SELFHOSTED_PORT)


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
    paths = collect_paths(files)
    workload = estimate_units(paths)
    output_root = Path(output_dir.strip() or DEFAULT_OUTPUT_DIR).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    if (
        mode == "maas"
        and not api_key.strip()
        and not env_file.strip()
        and not os.environ.get("GLMOCR_API_KEY")
    ):
        raise gr.Error("MaaS 模式需要 API Key。请填写 API Key、设置环境变量，或提供 .env 文件。")

    start_page_id = parse_optional_int(start_page, "PDF 起始页")
    end_page_id = parse_optional_int(end_page, "PDF 结束页")
    if start_page_id and end_page_id and start_page_id > end_page_id:
        raise gr.Error("PDF 起始页不能大于结束页。")

    parser_kwargs: dict[str, Any] = {
        "config_path": config_path.strip() or None,
        "mode": mode,
        "env_file": env_file.strip() or None,
    }
    if mode == "maas" and api_key.strip():
        parser_kwargs["api_key"] = api_key.strip()

    event_queue: queue.Queue = queue.Queue()
    state = {
        "summaries": [],
        "markdown_parts": [],
        "json_payloads": [],
        "log_lines": [],
        "download_paths": [],
        "error": None,
        "done": False,
    }

    def worker() -> None:
        try:
            if mode == "selfhosted":
                ensure_selfhosted_server(progress)
                parser_kwargs["ocr_api_host"] = SELFHOSTED_HOST
                parser_kwargs["ocr_api_port"] = SELFHOSTED_PORT

            total = len(paths)
            append_app_log(f"run_ocr start mode={mode} files={len(paths)} output={output_root}")
            with GlmOcr(**parser_kwargs) as parser:
                for index, file_path in enumerate(paths, start=1):
                    event_queue.put(
                        {
                            "type": "file_start",
                            "index": index,
                            "total": total,
                            "file_path": file_path,
                            "label": Path(file_path).name,
                        }
                    )

                    parse_kwargs = {
                        "save_layout_visualization": save_layout_visualization,
                    }
                    if mode == "maas":
                        if start_page_id is not None:
                            parse_kwargs["start_page_id"] = start_page_id
                        if end_page_id is not None:
                            parse_kwargs["end_page_id"] = end_page_id

                    result = parser.parse(file_path, **parse_kwargs)
                    result.save(
                        output_dir=output_root,
                        save_layout_visualization=save_layout_visualization,
                    )

                    saved_dir = output_root / Path(file_path).stem
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
                    state["log_lines"].append(f"[{index}/{total}] 完成，输出目录 {saved_dir}")

                    if saved_dir.exists():
                        for child in sorted(saved_dir.rglob("*")):
                            if child.is_file():
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
            append_app_log("run_ocr finished successfully")
        except MissingApiKeyError as exc:
            state["error"] = f"缺少 API Key: {exc}"
            state["log_lines"].append(traceback.format_exc())
            append_app_log(traceback.format_exc())
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            state["log_lines"].append(tb)
            append_app_log(tb)
        finally:
            state["done"] = True
            event_queue.put({"type": "done"})

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    total_units = sum(units for _, units in workload)
    completed_units = 0.0
    total_elapsed_start = time.time()
    current_label = "初始化"
    total_units = max(1.0, float(total_units))

    initial_progress = render_progress("初始化", 0.0, "预计剩余时间: 计算中")
    yield "", "", "", "", [], initial_progress

    while True:
        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                break

            if event["type"] == "file_start":
                current_label = f"处理 {event['label']}"
                state["log_lines"].append(
                    f"[{event['index']}/{event['total']}] 开始处理 {event['file_path']}"
                )
            elif event["type"] == "file_done":
                completed_units += workload[event["index"] - 1][1]
                current_label = f"完成 {event['index']}/{event['total']}"
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
            # Move the bar smoothly toward the estimated completion point while a file is running.
            smoothing_target = completed_units
            if completed_units < total_units:
                elapsed_units = min(total_units, max(completed_units, elapsed / max(eta_seconds, 1e-6) * total_units))
                smoothing_target = max(completed_units, elapsed_units)
            display_percent = (smoothing_target / total_units) * 100.0
            eta_text = format_eta(eta_seconds)
            progress_html = render_progress(current_label, display_percent, eta_text)
            summary_text = "\n\n" + ("\n" + ("-" * 60) + "\n\n").join(state["summaries"]) if state["summaries"] else ""
            markdown_text = "\n\n".join(state["markdown_parts"])
            json_text = json.dumps(state["json_payloads"], ensure_ascii=False, indent=2)
            logs_text = "\n".join(state["log_lines"])
            yield summary_text.strip(), markdown_text, json_text, logs_text, state["download_paths"], progress_html
            time.sleep(0.8)
            continue

        if state["error"]:
            progress_html = render_progress("错误", 0.0, "预计剩余时间: 计算中")
            summary_text = f"错误: {state['error']}"
            markdown_text = "\n\n".join(state["markdown_parts"])
            json_text = json.dumps(state["json_payloads"], ensure_ascii=False, indent=2)
            logs_text = "\n".join(state["log_lines"] or [state["error"]])
            yield summary_text, markdown_text, json_text, logs_text, state["download_paths"], progress_html
            break

        progress_html = render_progress("完成", 100.0, "预计剩余时间: 0 秒")
        summary_text = "\n\n" + ("\n" + ("-" * 60) + "\n\n").join(state["summaries"]) if state["summaries"] else ""
        markdown_text = "\n\n".join(state["markdown_parts"])
        json_text = json.dumps(state["json_payloads"], ensure_ascii=False, indent=2)
        logs_text = "\n".join(state["log_lines"])
        yield summary_text.strip(), markdown_text, json_text, logs_text, state["download_paths"], progress_html
        break


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
                        value=render_progress("就绪", 0.0, "预计剩余时间: 计算中")
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
