# GLM OCR Studio

Windows-first OCR studio for `glmocr`.

The web GUI is the primary entry point. The desktop launcher is kept as a
secondary fallback.

If you prefer Chinese, open [README_zh.md](README_zh.md).

## Overview

This repository packages a local Windows OCR workflow around the installed
`glmocr` SDK.

It is a Codex-assisted derivative of
[`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI) and is
adapted for local use on Windows.

## Highlights

- `selfhosted` mode with automatic local backend startup
- `maas` mode for API-key-based cloud use
- Image and PDF input
- Real-time progress and ETA
- Automatic backend status refresh
- Optional layout analysis export
- Web GUI as the recommended interface

## Quick Start

```powershell
conda activate glm-ocr
python .\glm_ocr_web_gui.py
```

If you want the desktop fallback:

```powershell
conda activate glm-ocr
python .\glm_ocr_local_gui.py
```

One-click launcher:

```bat
launch_glm_ocr_desktop.bat
```

The launcher tries `conda run -n glm-ocr python` first, then falls back to
`py -3`, then `python`.

## How It Works

1. Choose `selfhosted` for local OCR.
2. The web UI starts the local backend automatically if port `5002` is not up.
3. Upload an image or PDF.
4. The app writes Markdown and JSON outputs into the configured output folder.

`maas` is available only when you want API-key-based remote usage.

## Files

- `glm_ocr_web_gui.py` - web UI
- `glm_ocr_local_gui.py` - desktop UI
- `glm_ocr_local_server.py` - local OCR backend
- `launch_glm_ocr_desktop.bat` - one-click launcher
- `launch_glm_ocr_web_gui.bat` - web UI launcher
- `launch_glm_ocr_local_server.bat` - backend launcher

## Output

By default, outputs are written under the repository-local output folders:

- `glm_ocr_outputs`
- `glm_ocr_outputs_web`

Typical exports include:

- Markdown
- JSON
- Optional layout analysis artifacts

## Notes

- `selfhosted` mode does not need an API key.
- API fields are hidden unless `maas` is selected.
- Output and cache folders are ignored by Git.
- The web GUI is the primary interface; the desktop GUI is kept as a fallback.

## Attribution

- Based on [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- Built and adapted with Codex
- Based on the `glmocr` SDK
