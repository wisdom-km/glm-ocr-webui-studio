# GLM OCR Local GUI

Windows desktop and web UI for `glmocr`.

This project is a Codex-assisted derivative of the open-source
[`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI) project.
It was adapted for a local Windows workflow and for the `glmocr` SDK already
installed in this environment.

## Features

- Local `selfhosted` mode with automatic startup
- Cloud `maas` mode when you want to use an API key
- Image and PDF input
- Real-time progress and ETA
- Automatic backend status refresh
- Optional layout analysis export

## Layout

- `glm_ocr_web_gui.py`: web UI
- `glm_ocr_local_gui.py`: desktop UI
- `glm_ocr_local_server.py`: local OCR backend
- `launch_glm_ocr_desktop.bat`: one-click launcher
- `launch_glm_ocr_web_gui.bat`: web UI launcher
- `launch_glm_ocr_local_server.bat`: backend launcher

## Run

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
- `maas` mode shows the API fields only when selected.
- Output and cache folders are ignored by Git.

## Credits

- Based on [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- Built and adapted with Codex
- Based on the `glmocr` SDK
- UI adapted for a local Windows workflow
