# GLM OCR Studio Bug Fix Log

Date: 2026-03-19

This document is the long-lived engineering change log for repair work that materially changed reliability, observability, or operator workflow.

## 1. Scope

This log tracks fixes that affected:

- OCR task startup stability
- Web GUI error visibility
- selfhosted backend observability
- long PDF survivability
- merged Markdown post-processing
- project-level maintainability for logs and troubleshooting docs

## 2. Timeline Summary

### Phase 1. Startup robustness

We removed request-construction crashes caused by optional Gradio values arriving as `None`.

Main result:

- empty optional fields no longer break OCR startup before work begins

### Phase 2. Failure visibility

We changed the Web GUI so failures show up as normal structured outputs instead of disappearing behind generic Gradio error panels.

Main result:

- traceback, summary, JSON, and progress state now stay aligned during failures

### Phase 3. Selfhosted runtime observability

We added explicit lifecycle and progress instrumentation for selfhosted OCR:

- task start
- task end
- elapsed
- phase changes
- backend wait
- timeout context
- page / region / request identifiers

Main result:

- “looks frozen” became diagnosable

### Phase 4. Local server stability on Windows

We stabilized local model loading in `glm_ocr_local_server.py` by preferring local snapshot loading and avoiding the unstable client path that was producing:

```text
Cannot send a request, as the client has been closed.
```

Main result:

- local service startup became repeatable

### Phase 5. Long PDF survivability

We changed the high-risk selfhosted rendered-PDF path from one large parse session to page-by-page processing.

Main result:

- single heavy pages no longer kill the whole document by default
- partial outputs can survive page-level failures
- retry and restart hooks exist at the page boundary

### Phase 6. Merged Markdown quality

We improved the full-document Markdown output so it is usable as a deliverable, not only as a raw concatenation.

Main result:

- clearer full-document filename
- no artificial page headers
- blank-line cleanup
- duplicate short-line cleanup
- conservative broken-paragraph merge

### Phase 7. Repository hygiene

We moved logs and troubleshooting documents into stable folders instead of leaving them in the repository root.

Main result:

- `logs/runtime/` for durable runtime logs
- `logs/debug/` for one-off validation/debug logs
- `docs/maintenance/` for engineering history
- `docs/troubleshooting/` for operator guidance

## 3. Root Causes Confirmed

### 3.1 Input normalization bugs

Optional GUI fields were not normalized defensively, so empty Gradio inputs could crash request setup.

### 3.2 Weak failure surfacing

Exceptions could escape the OCR generator path and be reduced to generic UI error states.

### 3.3 Observability gap

The original selfhosted path did not expose enough runtime structure to answer:

- which page was active
- which region was active
- whether the backend was waiting or dead
- how long a request had been stuck

### 3.4 Local service load instability

The original local backend path could fail while loading or accessing model assets.

### 3.5 Long-session fragility

Complex PDFs with many regions could poison a long-lived selfhosted parse session. One heavy page could cause a timeout that failed the whole document.

## 4. Repair Strategy

### 4.1 Normalize early

Normalize all optional GUI inputs at the request boundary.

### 4.2 Emit structured runtime state

Prefer:

- stage logs
- request IDs
- page IDs
- region IDs
- elapsed fields

over free-form text without execution context.

### 4.3 Shrink the failure boundary

Page-level isolation was the highest-leverage stability change. It converts “whole document dies” into “current page can retry or fail explicitly”.

### 4.4 Prefer reliability over throughput on heavy pages

For problematic pages, conservative generation limits and recoverability matter more than raw speed.

### 4.5 Separate durable docs from transient logs

Project root should stay focused on entry points and core source files. Operational records belong in stable subfolders.

## 5. Files Most Affected

- `glm_ocr_web_gui.py`
- `glm_ocr_local_server.py`
- `launch_glm_ocr_desktop.bat`
- `launch_glm_ocr_local_server.bat`
- `README.md`
- `README_zh.md`

## 6. Validation Highlights

Key checkpoints from this repair cycle:

- single-page OCR remained functional
- short PDFs remained functional
- problematic reproduced ranges became diagnosable
- page-by-page selfhosted processing completed `12-20`
- page-by-page selfhosted processing completed `12-30`
- merged Markdown output remained compatible with existing page directories

## 7. Maintenance Rules

When adding new diagnostics or repair notes in the future:

1. Add engineering-facing repair history here.
2. Put operator/user instructions into `../troubleshooting/`.
3. Keep runtime logs under `../../logs/runtime/`.
4. Keep temporary validation logs under `../../logs/debug/`.
5. Avoid placing new long-lived operational notes in the repository root.

## 8. Lessons Learned

### Most important lesson

Do not optimize a long OCR workflow before it is observable.

### Most effective technical change

Reducing the failure boundary from “entire rendered PDF batch” to “single page” was more valuable than simply increasing timeouts.

### Most useful debugging habit

Always align:

- GUI state
- runtime log state
- local server log state
- output directory contents

before concluding where the failure lives.
