# GLM OCR Studio

面向 Windows 的 `glmocr` 本地 OCR 工具。

当前主入口是 Web GUI，桌面启动器作为备用入口保留。


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

## 项目结构

- `docs/maintenance/` - 工程修复日志与维护记录
- `docs/troubleshooting/` - 面向使用者的排障文档
- `logs/runtime/` - 稳定保留的运行日志
- `logs/debug/` - 一次性调试、验证、烟雾测试日志
- `glm_ocr_outputs_web/` - Web GUI 识别输出
- `glm_ocr_outputs/` - 桌面/本地识别输出

## 输出内容

默认输出会放到仓库内的目录：

- `glm_ocr_outputs`
- `glm_ocr_outputs_web`

常见导出包括：

- Markdown
- JSON
- 可选的版面分析图

## 长 PDF 推荐工作流

对于页数很多、版面复杂、或者以前容易卡住的 PDF，建议按下面的方式使用：

1. 先确认本地 `selfhosted` 后端已经就绪。
2. 如果 PDF 很大，先测单页或短页范围，再放大到整段范围。
3. 现在 `selfhosted + PDF` 会先把 PDF 渲染成图片页，再按页处理。
4. 如果任务看起来变慢，优先看：
   - `logs/runtime/glm_ocr_web_gui.log`
   - `logs/runtime/glm_ocr_local_server.log`
5. 如果某一页特别重，当前实现会把卡住页、重试状态、服务重启状态暴露出来，而不是整本书直接黑箱失败。

这也是当前推荐用于书籍、扫描版 PDF、图文混排 PDF、以及曾经在 `parser.parse(...)` 阶段看起来像卡死的文档的工作流。

## 说明

- `selfhosted` 模式不需要 API Key。
- 只有选择 `maas` 时，API 输入区才会显示。
- 输出目录和缓存目录都已加入 Git 忽略。
- Web GUI 是主入口，桌面 GUI 作为备用。
- 运行日志默认写入 `logs/runtime/`。
- 排障文档和修复记录统一放在 `docs/` 目录下。

## 致谢

- 基于 [`NaserTahiri/GLM-OCR-GUI`](https://github.com/NaserTahiri/GLM-OCR-GUI)
- 使用 Codex 完成适配与重构
- 基于 `glmocr` SDK
