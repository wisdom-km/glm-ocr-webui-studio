from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("glm_ocr_web_gui.py")
spec = importlib.util.spec_from_file_location("glm_ocr_web_gui", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def check_preparing_state() -> None:
    progress = {
        "pages_total": 133,
        "pages_loaded": 0,
        "layout_pages_done": 0,
        "regions_total": 0,
        "regions_done": 0,
        "parse_done": False,
        "save_done": False,
        "phase": "pdf_opened",
    }
    label, stage, phase, current, total = module.describe_selfhosted_progress(progress)
    assert current == 0 and total == 133
    assert "0 / 133" in label
    assert "打开 PDF" in stage
    assert phase == "pdf_opened"


def check_timeout_summary() -> None:
    message = (
        "Exception: OCR request failed: {'error': \"Error during recognition: "
        "HTTPConnectionPool(host='127.0.0.1', port=5002): Read timed out. "
        "(read timeout=300)\"}, status_code: 500"
    )
    summary = module.summarize_error_for_ui(message)
    assert "超时" in summary["summary"]
    assert "127.0.0.1:5002" in summary["summary"]
    assert "300" in summary["detail"]


def check_eta_behavior() -> None:
    progress = {
        "pages_total": 10,
        "pages_loaded": 2,
        "layout_pages_done": 1,
        "regions_total": 20,
        "regions_done": 4,
        "parse_done": False,
        "save_done": False,
        "started_at": 0.0,
    }
    eta = module.estimate_selfhosted_eta_seconds(
        progress,
        current_file_units=10.0,
        total_units=10.0,
        completed_units=0.0,
        task_started_at=0.0,
    )
    assert eta is not None


def check_page_range_units() -> None:
    original_counter = module.count_pdf_pages
    try:
        module.count_pdf_pages = lambda path: 133
        units = module.estimate_units(
            ["C:\\fake\\book.pdf"],
            start_page_id=13,
            end_page_id=133,
        )
        assert units == [("C:\\fake\\book.pdf", 121)]
    finally:
        module.count_pdf_pages = original_counter


def check_timeout_extract() -> None:
    message = (
        "HTTPConnectionPool(host='127.0.0.1', port=5002): Read timed out. "
        "(read timeout=300)"
    )
    details = module.extract_backend_timeout_details(message)
    assert details is not None
    assert details["service"] == "127.0.0.1:5002"
    assert details["timeout_seconds"] == 300


def check_render_progress_contains_stage() -> None:
    html = module.render_progress(
        "当前进度：0 / 121 页",
        0.0,
        "剩余时间：计算中",
        "当前阶段：正在打开 PDF",
    )
    assert "当前阶段" in html
    assert "0 / 121" in html


def check_runtime_log_format() -> None:
    line = module.append_runtime_log(
        "INFO",
        "task start",
        task_id="ocr_test",
        mode="selfhosted",
        input="demo.pdf",
        total_pages=10,
        include_in_gui=False,
    )
    assert "[INFO] task start" in line
    assert "task_id=ocr_test" in line
    assert "total_pages=10" in line


if __name__ == "__main__":
    check_preparing_state()
    check_timeout_summary()
    check_eta_behavior()
    check_page_range_units()
    check_timeout_extract()
    check_render_progress_contains_stage()
    check_runtime_log_format()
    print("DEBUG_VALIDATION_OK")
