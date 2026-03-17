# GLM OCR Studio

Local Windows web GUI for `glmocr`, with an optional desktop launcher.

This repository is a Codex-assisted derivative of the open-source
[`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI) project.
It was adapted for a local Windows workflow and for the `glmocr` SDK already
installed in this environment.

## Features

- `selfhosted` mode with automatic local backend startup
- `maas` mode for API-key-based cloud use
- Image and PDF input
- Real-time progress and ETA
- Automatic backend status refresh
- Optional layout analysis export

## Recommended UI

Use the web GUI first. It is the most polished and the main entry point for this repository.

The desktop GUI is still included for convenience, but it is secondary and less optimized.

## Files

- `glm_ocr_web_gui.py` - web UI
- `glm_ocr_local_gui.py` - desktop UI
- `glm_ocr_local_server.py` - local OCR backend
- `launch_glm_ocr_desktop.bat` - one-click launcher
- `launch_glm_ocr_web_gui.bat` - web UI launcher
- `launch_glm_ocr_local_server.bat` - backend launcher

## Usage

Web UI:

```powershell
conda activate glm-ocr
python .\glm_ocr_web_gui.py
```

Desktop UI:

```powershell
conda activate glm-ocr
python .\glm_ocr_local_gui.py
```

One-click launcher:

```bat
launch_glm_ocr_desktop.bat
```

The launcher will try `conda run -n glm-ocr python` first, then fall back to
`py -3` or `python` on your PATH.

If you are not using Conda, replace the `python` command with the Python
executable from your own environment.

## Notes

- `selfhosted` mode does not need an API key.
- API fields are hidden unless `maas` is selected.
- Output and cache folders are ignored by Git.
- The web GUI is the primary interface; the desktop GUI is kept as a fallback.

## Credits

- Based on [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- Built and adapted with Codex
- Based on the `glmocr` SDK
