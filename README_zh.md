# GLM OCR Studio

面向 Windows 的本地 OCR Web GUI，基于 `glmocr`，并提供可选的桌面启动器。

本项目是基于开源项目 [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI) 做的 Codex 适配与重构，目标是更适合本机 Windows 环境和当前已安装的 `glmocr` SDK。

## 功能

- `selfhosted` 模式，自动启动本地后端
- `maas` 模式，支持 API Key 云端调用
- 支持图片和 PDF 输入
- 实时进度与预计剩余时间
- 后端状态自动刷新
- 可选的版面分析图导出

## 推荐入口

优先使用 Web GUI。它是当前最完整、最主要的入口。

桌面 GUI 仍保留为备用入口，但优化程度较低。

## 文件说明

- `glm_ocr_web_gui.py` - Web 界面
- `glm_ocr_local_gui.py` - 桌面界面
- `glm_ocr_local_server.py` - 本地 OCR 后端
- `launch_glm_ocr_desktop.bat` - 一键启动器
- `launch_glm_ocr_web_gui.bat` - Web 启动器
- `launch_glm_ocr_local_server.bat` - 后端启动器

## 使用方法

Web UI：

```powershell
conda activate glm-ocr
python .\glm_ocr_web_gui.py
```

桌面 UI：

```powershell
conda activate glm-ocr
python .\glm_ocr_local_gui.py
```

一键启动器：

```bat
launch_glm_ocr_desktop.bat
```

启动器会优先尝试 `conda run -n glm-ocr python`，然后回退到 `py -3` 或 `python`。

如果你没有使用 Conda，就把上面的 `python` 替换成你自己环境里的 Python 可执行文件。

## 说明

- `selfhosted` 模式不需要 API Key。
- 只有选择 `maas` 时，API 输入区才会显示。
- 输出目录和缓存目录都已加入 Git 忽略。
- Web GUI 是主入口，桌面 GUI 作为备用。

## 致谢

- 基于 [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- 使用 Codex 完成适配与重构
- 基于 `glmocr` SDK
