# GLM OCR Studio Bug Fix Log

Date: 2026-03-18

This log records two concrete fixes made to the Web GUI OCR path. The focus is on the failure mode, root cause, and the exact repair strategy.

## 1. `config_path.strip()` crash during OCR startup

### Symptom

Clicking `Start Recognition` could fail immediately with:

```text
AttributeError: 'NoneType' object has no attribute 'strip'
```

The error appeared before OCR work actually started, and the UI only showed a generic error state.

### Root Cause

The request path in `glm_ocr_web_gui.py` assumed every optional Gradio textbox value was a string.

In practice, Gradio can pass `None` for empty optional inputs. The code then did:

```python
config_path.strip() or None
```

That same pattern also existed for other optional values such as:

- `api_key`
- `env_file`
- `output_dir`
- `start_page`
- `end_page`

So the startup path was not robust against empty form fields.

### Fix

I added a small normalization helper and applied it at the top of `run_ocr()`:

- `None` becomes `""`
- strings are stripped safely
- non-string values are converted with `str(...).strip()`

Then the request payload is built only from normalized values:

- `config_path_text`
- `env_file_text`
- `api_key_text`
- `output_dir_text`
- `start_page_text`
- `end_page_text`

This removes the `None.strip()` crash class without changing the overall OCR flow.

### Result

Starting a job with `config_path` left empty no longer crashes during request construction.

If a later stage fails, the app continues to surface that failure instead of dying at startup.

## 2. OCR failure visibility and generic `Error` panels

### Symptom

The UI could show red `Error` placeholders in multiple output panels, while the actual reason for failure was not visible.

The app also appeared to stall at `0.0%` in some cases.

### Root Cause

There were two separate problems:

1. Some exceptions were escaping the OCR generator path and were being turned into Gradio's generic error state.
2. The OCR execution path had weak instrumentation, so the real failing stage was not obvious from the UI.

On top of that, `selfhosted` OCR on Windows had a second concrete issue when local input paths were not safe for the downstream loader, especially for non-ASCII filenames.

### Fix

I changed the Web GUI error handling to be failure-observable:

- Added `append_app_log()` stage markers.
- Added `build_error_outputs()` so failures are returned as normal output values.
- Ensured the summary, JSON, logs, and progress area all receive a concrete error payload.
- Added a staging path for `selfhosted` inputs when the filename contains non-ASCII characters.
- Added cleanup and trace logging around the parser, parse, and save stages.

### Result

If OCR fails now:

- the summary panel shows the real error message
- the logs panel shows the traceback
- the JSON panel shows a structured error object
- the progress area shows an error state instead of silently dying

This makes downstream failures visible instead of masking them behind a generic UI error.

## Verification Notes

The following cases were checked during debugging:

- `run_ocr(..., config_path=None, env_file=None, api_key=None, output_dir=None)` no longer crashes on `.strip()`
- successful OCR runs still produce Markdown / JSON / output files
- failure cases now produce actionable logs instead of only a generic red panel

## Summary

The two fixes together improve both correctness and debuggability:

- startup request normalization now handles empty optional inputs safely
- OCR failures are now visible in the UI and logs rather than hidden by Gradio's generic error handling

## Why This Upgrade Was Needed

This was not just a cosmetic cleanup. The app had a real usability gap:

- a blank optional field could stop the OCR job before it reached the model
- the UI masked the real failure stage behind generic error pills
- debugging required guessing instead of reading the actual traceback

The upgrade path fixes that by making the request boundary defensive and by keeping later-stage failures visible. The result is a more usable OCR workflow on Windows without changing the overall app design or adding unnecessary refactoring.

## 3. False `100%` completion and mismatched output folder detection

### Symptom

Large PDF jobs could appear to finish in the Web GUI even though:

- the progress bar showed `100.0%`
- the output file area stayed empty
- the expected output folder name did not appear in the configured output directory

In practice, the OCR job was often still inside `parser.parse(...)`, or the GUI was looking for the wrong saved directory name.

### Root Cause

There were two concrete issues:

1. The progress bar used a smoothed estimate and could visually reach `100%` before `result.save(...)` had actually completed.
2. The GUI assumed the saved folder name was always `Path(file_path).stem`, while `glmocr` internally sanitizes Windows-illegal characters before saving.

That meant a long filename such as one containing `:` or `?` could be saved under a sanitized folder name, while the GUI kept checking a different unsanitized path.

### Fix

I aligned the GUI with the actual `glmocr` save behavior:

- added a local output-name sanitizer that mirrors `glmocr`'s save logic
- resolved the expected saved directory using the sanitized stem
- verified that `.md` and `.json` files actually exist after `result.save(...)`
- prevented the progress bar from claiming `100%` until save completion is confirmed

### Result

The Web GUI no longer reports a finished job before output files are truly written.

If saving does not actually produce Markdown or JSON files, the run now fails explicitly instead of showing a false success state.

## 4. Initial selfhosted progress event integration

### Symptom

The old progress implementation was file-level and estimate-based. For large PDFs this produced misleading UI behavior:

- labels looked active
- percent could be disconnected from the actual OCR stage
- logs were too sparse to show whether the pipeline was still making progress

### Root Cause

The `selfhosted` execution path runs the `glmocr` pipeline locally in the GUI process, but the Web GUI was only tracking coarse start / done events.

That meant the page loading, layout batching, OCR region processing, and save stages were not being reflected in the UI as first-class progress events.

### Fix

I added runtime hooks around the local `glmocr` pipeline so the GUI can observe selfhosted stages such as:

- page loading
- layout batch completion
- OCR region completion
- parse completion
- save completion

These hooks now feed the GUI's active progress state instead of relying only on file-level smoothing.

### Result

The current version is materially closer to real execution progress in `selfhosted` mode, and it prevents the most misleading false-finish behavior.

There are still follow-up improvements to make around locking, ETA calculation, and event ordering, but the app now exposes more of the real OCR lifecycle than before.
