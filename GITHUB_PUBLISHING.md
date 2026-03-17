# GitHub Publishing Notes

Use these values when creating the public repository.

## Recommended Repository Name

`glm-ocr-studio`

## Short Description

Local Windows web GUI for glmocr with selfhosted and maas modes.

## About / Long Description

`GLM OCR Studio` is a Windows-first OCR interface built around the `glmocr` SDK.
It focuses on the web GUI as the main entry point and includes a lightweight
desktop launcher for convenience.

The project supports:

- `selfhosted` mode with automatic local backend startup
- `maas` mode for API-key-based cloud use
- Image and PDF input
- Real-time progress and ETA
- Automatic backend status refresh
- Optional layout analysis export

This repository is a Codex-assisted derivative of
[`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI),
adapted for the local Windows workflow used in this environment.

## Topics

- `ocr`
- `windows`
- `python`
- `gradio`
- `fastapi`
- `glmocr`
- `document-processing`

## First Release Notes

### GLM OCR Studio 0.1.0

Initial public release of `GLM OCR Studio`.

Highlights:

- Web GUI as the primary interface
- Automatic local `selfhosted` backend startup
- `maas` support when an API key is needed
- Image and PDF input
- Real-time progress bar with ETA
- Automatic backend status updates
- Optional layout analysis export

Notes:

- The desktop GUI is included as a secondary launcher.
- The project is adapted from the open-source `NaserTahiri/GLM-OCR-GUI`
  project and built with Codex.
