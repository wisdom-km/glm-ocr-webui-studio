# GLM OCR Studio

面向 Windows 的 `glmocr` 本地 OCR 工具。

当前主入口是 Web GUI，桌面启动器作为备用入口保留。

如果你更习惯中文，请看 [README_zh.md](README_zh.md)。

## 概览

这个项目把已安装的 `glmocr` SDK 封装成一个更适合 Windows 本地使用的 OCR 工作流。

它是基于开源项目 [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
做的 Codex 适配与重构。

## 主要特性

- `selfhosted` 模式，自动启动本地后端
- `maas` 模式，支持 API Key 云端调用
- 支持图片和 PDF
- 实时进度和预计剩余时间
- 后端状态自动刷新
- 可选的版面分析图导出
- Web GUI 作为推荐入口

## 快速开始

```powershell
conda activate glm-ocr
python .\glm_ocr_web_gui.py
```

如果想使用桌面版备用入口：

```powershell
conda activate glm-ocr
python .\glm_ocr_local_gui.py
```

一键启动器：

```bat
launch_glm_ocr_desktop.bat
```

启动器会先尝试 `conda run -n glm-ocr python`，然后回退到 `py -3`，
最后回退到 `python`。

## 工作方式

1. 选择 `selfhosted`，使用本地 OCR。
2. 如果 `5002` 端口没有起来，Web GUI 会自动拉起本地后端。
3. 上传图片或 PDF。
4. 程序会把 Markdown 和 JSON 输出写到你配置的输出目录。

只有在你需要 API Key 云端调用时，才选择 `maas`。

## 文件说明

- `glm_ocr_web_gui.py` - Web 界面
- `glm_ocr_local_gui.py` - 桌面界面
- `glm_ocr_local_server.py` - 本地 OCR 后端
- `launch_glm_ocr_desktop.bat` - 一键启动器
- `launch_glm_ocr_web_gui.bat` - Web 启动器
- `launch_glm_ocr_local_server.bat` - 后端启动器

## 输出内容

默认输出会放到仓库内的目录：

- `glm_ocr_outputs`
- `glm_ocr_outputs_web`

常见导出包括：

- Markdown
- JSON
- 可选的版面分析图

## 说明

- `selfhosted` 模式不需要 API Key。
- 只有选择 `maas` 时，API 输入区才会显示。
- 输出目录和缓存目录都已加入 Git 忽略。
- Web GUI 是主入口，桌面 GUI 作为备用。

## 致谢

- 基于 [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- 使用 Codex 完成适配与重构
- 基于 `glmocr` SDK
- README 的结构和发布组织方式参考了
  [`wisdom-km/LYRIC-SYNC`](https://github.com/wisdom-km/LYRIC-SYNC)
