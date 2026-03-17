# GLM OCR Studio

Local Windows GUI for `glmocr`, with both desktop and web entry points.

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
G:\BaseWare\Anaconda\envs\glm-ocr\python.exe .\glm_ocr_web_gui.py
```

Desktop UI:

```powershell
G:\BaseWare\Anaconda\envs\glm-ocr\python.exe .\glm_ocr_local_gui.py
```

One-click launcher:

```bat
launch_glm_ocr_desktop.bat
```

## Notes

- `selfhosted` mode does not need an API key.
- API fields are hidden unless `maas` is selected.
- Output and cache folders are ignored by Git.

## Credits

- Based on [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- Built and adapted with Codex
- Based on the `glmocr` SDK
