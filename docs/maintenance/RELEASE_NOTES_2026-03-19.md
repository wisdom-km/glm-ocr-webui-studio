# Release Notes - 2026-03-19

This release focuses on selfhosted OCR reliability, long-PDF survivability, and project maintainability.

## Highlights

- Improved selfhosted PDF reliability on Windows
- Added stronger runtime observability for OCR tasks and backend waits
- Stabilized local model loading in the bundled selfhosted server
- Changed long-PDF processing to a safer page-oriented path
- Improved merged Markdown output quality
- Reorganized logs and troubleshooting documents into fixed folders

## What Changed

### 1. Selfhosted PDF reliability

The selfhosted PDF path is now safer for long and layout-heavy documents.

Key change:

- `selfhosted + PDF` now renders PDF pages into images first and processes them page by page, instead of relying on the higher-risk direct whole-PDF parse path

Benefits:

- one problematic page is less likely to kill the whole document
- current page context is easier to observe
- retries and service recovery can happen at a smaller boundary

### 2. Better runtime logs and failure visibility

The GUI/runtime path now exposes:

- task start
- task end
- elapsed
- page / region / request context
- backend wait states
- timeout-related diagnostics

Benefits:

- jobs that look slow are easier to distinguish from jobs that are actually stuck
- operators can identify the failing page and request faster

### 3. Local backend stability

The local selfhosted server now loads more defensively on Windows by preferring the local snapshot path and avoiding the unstable loading path that previously produced client-closed errors.

Benefits:

- backend startup is more repeatable
- local service readiness is easier to trust during long runs

### 4. Improved merged Markdown output

Full-document Markdown output is now more usable.

Improvements include:

- clearer full-document naming
- no artificial page headers in merged output
- blank-line cleanup
- duplicate short-line cleanup
- conservative merge of obvious page-break residue

### 5. Project structure cleanup

Logs and operator documents are now organized into stable folders:

- `logs/runtime/`
- `logs/debug/`
- `docs/maintenance/`
- `docs/troubleshooting/`

Benefits:

- repository root stays cleaner
- runtime logs and one-off debug logs are separated
- engineering notes and operator playbooks are easier to maintain

## Recommended Usage After This Release

For long PDFs:

1. Prefer `selfhosted` only after backend readiness is confirmed.
2. Test one page or a short page range first if the document is complex.
3. Use the runtime logs under `logs/runtime/` when diagnosing slow pages.
4. Treat page-level context as the main debugging unit for difficult documents.

## Main Files Updated In This Release

- `glm_ocr_web_gui.py`
- `glm_ocr_local_server.py`
- `launch_glm_ocr_desktop.bat`
- `launch_glm_ocr_local_server.bat`
- `README.md`
- `README_zh.md`
- `docs/maintenance/BUG_FIX_LOG.md`
- `docs/troubleshooting/USER_PDF_DEBUG_PLAYBOOK.md`

## Validation Snapshot

Validation during this cycle included:

- single-page OCR remained functional
- short selfhosted PDF ranges completed successfully after the page-oriented path landed
- lightweight debug validation script passed

## Notes

This release prioritizes reliability and diagnosability over maximum throughput for complex selfhosted PDFs.
