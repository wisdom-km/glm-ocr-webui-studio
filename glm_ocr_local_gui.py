import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from tkinter import END, LEFT, RIGHT, BOTH, X, Y, filedialog, messagebox, StringVar, BooleanVar
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import requests
from glmocr import GlmOcr
from glmocr.maas_client import MissingApiKeyError


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".pdf"}
DEFAULT_OUTPUT_DIR = Path.cwd() / "glm_ocr_outputs"
SELFHOSTED_HOST = "127.0.0.1"
SELFHOSTED_PORT = 5002
APP_ROOT = Path(__file__).resolve().parent


class GlmOcrGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GLM OCR Local GUI")
        self.root.geometry("1180x780")
        self.root.minsize(980, 680)

        self.queue: Queue = Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.current_parser: GlmOcr | None = None
        self.selected_files: list[str] = []
        self.last_saved_paths: list[Path] = []
        self.last_result: dict | None = None

        self.mode_var = StringVar(value="maas")
        self.api_key_var = StringVar(value=os.environ.get("GLMOCR_API_KEY", ""))
        self.env_file_var = StringVar()
        self.config_var = StringVar()
        self.output_dir_var = StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.status_var = StringVar(value="就绪")
        self.progress_var = StringVar(value="未开始")
        self.save_layout_var = BooleanVar(value=True)
        self.start_page_var = StringVar()
        self.end_page_var = StringVar()

        self._build_ui()
        self._poll_queue()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self._build_file_section(outer)
        self._build_settings_section(outer)
        self._build_results_section(outer)

    def _build_file_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="输入文件", padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        help_text = (
            "支持图片和 PDF。可批量加入多个文件；MaaS 模式下需要有效的 GLMOCR API Key。"
        )
        ttk.Label(frame, text=help_text).grid(row=0, column=0, sticky="w", pady=(0, 8))

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.file_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=8)
        self.file_list.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=scrollbar.set)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        ttk.Button(button_frame, text="添加文件", command=self.add_files).pack(side=LEFT)
        ttk.Button(button_frame, text="添加目录", command=self.add_directory).pack(side=LEFT, padx=(8, 0))
        ttk.Button(button_frame, text="移除选中", command=self.remove_selected).pack(side=LEFT, padx=(8, 0))
        ttk.Button(button_frame, text="清空列表", command=self.clear_files).pack(side=LEFT, padx=(8, 0))

    def _build_settings_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="运行设置", padding=10)
        frame.grid(row=1, column=0, sticky="nsew", pady=(12, 12))
        for col in range(4):
            frame.columnconfigure(col, weight=1)

        ttk.Label(frame, text="模式").grid(row=0, column=0, sticky="w")
        mode_combo = ttk.Combobox(
            frame,
            textvariable=self.mode_var,
            values=("maas", "selfhosted"),
            state="readonly",
        )
        mode_combo.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_mode_hint())

        ttk.Label(frame, text="API Key").grid(row=0, column=1, sticky="w")
        self.api_key_entry = ttk.Entry(frame, textvariable=self.api_key_var, show="*")
        self.api_key_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10))

        ttk.Label(frame, text="配置文件").grid(row=0, column=2, sticky="w")
        config_wrap = ttk.Frame(frame)
        config_wrap.grid(row=1, column=2, sticky="ew", padx=(0, 10))
        config_wrap.columnconfigure(0, weight=1)
        ttk.Entry(config_wrap, textvariable=self.config_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(config_wrap, text="浏览", command=self.browse_config, width=8).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(frame, text=".env 文件").grid(row=0, column=3, sticky="w")
        env_wrap = ttk.Frame(frame)
        env_wrap.grid(row=1, column=3, sticky="ew")
        env_wrap.columnconfigure(0, weight=1)
        ttk.Entry(env_wrap, textvariable=self.env_file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(env_wrap, text="浏览", command=self.browse_env_file, width=8).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(frame, text="输出目录").grid(row=2, column=0, sticky="w", pady=(10, 0))
        output_wrap = ttk.Frame(frame)
        output_wrap.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 2), padx=(0, 10))
        output_wrap.columnconfigure(0, weight=1)
        ttk.Entry(output_wrap, textvariable=self.output_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_wrap, text="浏览", command=self.browse_output_dir, width=8).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(frame, text="PDF 起始页").grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="PDF 结束页").grid(row=2, column=3, sticky="w", pady=(10, 0))
        ttk.Entry(frame, textvariable=self.start_page_var).grid(row=3, column=2, sticky="ew", padx=(0, 10))
        ttk.Entry(frame, textvariable=self.end_page_var).grid(row=3, column=3, sticky="ew")

        toggles = ttk.Frame(frame)
        toggles.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(
            toggles,
            text="保存布局可视化结果",
            variable=self.save_layout_var,
        ).pack(side=LEFT)

        action_frame = ttk.Frame(frame)
        action_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(action_frame, text="开始识别", command=self.start_processing).pack(side=LEFT)
        ttk.Button(action_frame, text="取消", command=self.cancel_processing).pack(side=LEFT, padx=(8, 0))
        ttk.Button(action_frame, text="打开输出目录", command=self.open_output_dir).pack(side=LEFT, padx=(8, 0))

        ttk.Label(frame, textvariable=self.status_var).grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.progress_var, foreground="#555555").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(2, 0)
        )

        self.mode_hint_label = ttk.Label(frame, foreground="#555555")
        self.mode_hint_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._update_mode_hint()

    def _build_results_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="结果预览", padding=10)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        self.summary_text = self._make_text_tab(notebook, "摘要")
        self.markdown_text = self._make_text_tab(notebook, "Markdown")
        self.json_text = self._make_text_tab(notebook, "JSON")
        self.log_text = self._make_text_tab(notebook, "日志")

    def _make_text_tab(self, notebook: ttk.Notebook, title: str) -> ScrolledText:
        container = ttk.Frame(notebook)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        text = ScrolledText(container, wrap="word", font=("Consolas", 10))
        text.grid(row=0, column=0, sticky="nsew")
        text.configure(state="disabled")
        notebook.add(container, text=title)
        return text

    def _set_text(self, widget: ScrolledText, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", END)
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _append_log(self, content: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, content.rstrip() + "\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _update_mode_hint(self) -> None:
        if self.mode_var.get() == "maas":
            text = "MaaS 模式走智谱云端接口。你当前环境最容易直接跑通这一模式，但需要可用 API Key。"
            self.api_key_entry.configure(state="normal")
        else:
            text = "selfhosted 模式要求你本地已启动 GLM-OCR 服务端；这个 GUI 只负责调用 SDK，不负责拉起 vLLM/SGLang。"
            self.api_key_entry.configure(state="disabled")
        self.mode_hint_label.configure(text=text)

    def add_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择图片或 PDF",
            filetypes=[
                ("支持的文件", "*.jpg *.jpeg *.png *.bmp *.gif *.webp *.pdf"),
                ("所有文件", "*.*"),
            ],
        )
        self._add_paths(files)

    def add_directory(self) -> None:
        folder = filedialog.askdirectory(title="选择目录")
        if not folder:
            return
        files = sorted(
            str(path)
            for path in Path(folder).iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        self._add_paths(files)

    def _add_paths(self, paths) -> None:
        new_paths = []
        for raw in paths:
            path = str(Path(raw).resolve())
            if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if path not in self.selected_files:
                self.selected_files.append(path)
                new_paths.append(path)
        for path in new_paths:
            self.file_list.insert(END, path)
        if new_paths:
            self.status_var.set(f"已加入 {len(new_paths)} 个文件")

    def remove_selected(self) -> None:
        indices = list(self.file_list.curselection())
        for index in reversed(indices):
            self.file_list.delete(index)
            del self.selected_files[index]

    def clear_files(self) -> None:
        self.file_list.delete(0, END)
        self.selected_files.clear()

    def browse_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_dir_var.set(folder)

    def browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 YAML 配置文件",
            filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")],
        )
        if path:
            self.config_var.set(path)

    def browse_env_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 .env 文件",
            filetypes=[("环境文件", "*.env"), ("所有文件", "*.*")],
        )
        if path:
            self.env_file_var.set(path)

    def start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("任务进行中", "当前已有任务在运行，请先等待完成或点击取消。")
            return
        if not self.selected_files:
            messagebox.showwarning("缺少输入", "请先加入至少一个图片或 PDF 文件。")
            return

        output_dir = Path(self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)

        mode = self.mode_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if mode == "maas" and not api_key and not self.env_file_var.get().strip() and not os.environ.get("GLMOCR_API_KEY"):
            messagebox.showwarning("缺少 API Key", "MaaS 模式需要 API Key。请填写 API Key、设置环境变量，或选择 .env 文件。")
            return

        start_page = self._parse_optional_int(self.start_page_var.get().strip(), "PDF 起始页")
        end_page = self._parse_optional_int(self.end_page_var.get().strip(), "PDF 结束页")
        if start_page is None and self.start_page_var.get().strip():
            return
        if end_page is None and self.end_page_var.get().strip():
            return
        if start_page and end_page and start_page > end_page:
            messagebox.showerror("页码错误", "PDF 起始页不能大于结束页。")
            return

        self.cancel_event.clear()
        self.last_saved_paths.clear()
        self.last_result = None
        self._set_text(self.summary_text, "")
        self._set_text(self.markdown_text, "")
        self._set_text(self.json_text, "")
        self._append_log("开始任务")
        self.status_var.set("准备启动")
        self.progress_var.set(f"共 {len(self.selected_files)} 个文件")

        options = {
            "mode": mode,
            "api_key": api_key or None,
            "env_file": self.env_file_var.get().strip() or None,
            "config_path": self.config_var.get().strip() or None,
            "output_dir": output_dir,
            "save_layout_visualization": self.save_layout_var.get(),
            "start_page_id": start_page,
            "end_page_id": end_page,
            "files": list(self.selected_files),
        }

        self.worker = threading.Thread(target=self._run_worker, args=(options,), daemon=True)
        self.worker.start()

    def cancel_processing(self) -> None:
        if not self.worker or not self.worker.is_alive():
            self.status_var.set("当前没有运行中的任务")
            return
        self.cancel_event.set()
        self.status_var.set("已请求取消，当前文件完成后会停止")
        self._append_log("收到取消请求")

    def open_output_dir(self) -> None:
        folder = Path(self.output_dir_var.get().strip() or DEFAULT_OUTPUT_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(folder)])

    def _parse_optional_int(self, value: str, label: str) -> int | None:
        if not value:
            return None
        try:
            number = int(value)
        except ValueError:
            messagebox.showerror("输入错误", f"{label} 必须是整数。")
            return None
        if number <= 0:
            messagebox.showerror("输入错误", f"{label} 必须大于 0。")
            return None
        return number

    def _is_port_open(self, host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            return sock.connect_ex((host, port)) == 0

    def _wait_for_local_server(self, host: str, port: int, timeout: int = 180) -> None:
        start = time.time()
        while time.time() - start < timeout:
            if self._is_port_open(host, port):
                try:
                    response = requests.get(
                        f"http://{host}:{port}/health",
                        timeout=2,
                        proxies={"http": None, "https": None},
                    )
                    if response.ok:
                        return
                except Exception:
                    pass
            time.sleep(2)
        raise TimeoutError(f"本地 GLM-OCR 服务未能在 {timeout} 秒内启动成功。")

    def _ensure_selfhosted_server(self) -> None:
        if self._is_port_open(SELFHOSTED_HOST, SELFHOSTED_PORT):
            return

        self.queue.put(("log", "未检测到本地服务，正在启动 glm_ocr_local_server.py"))
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen(
            [
                sys.executable,
                str(APP_ROOT / "glm_ocr_local_server.py"),
                "--host",
                SELFHOSTED_HOST,
                "--port",
                str(SELFHOSTED_PORT),
            ],
            cwd=str(APP_ROOT),
            creationflags=creationflags,
        )
        self._wait_for_local_server(SELFHOSTED_HOST, SELFHOSTED_PORT)

    def _run_worker(self, options: dict) -> None:
        files = options["files"]
        output_dir: Path = options["output_dir"]
        self.queue.put(("status", "初始化 GLM-OCR"))

        parser_kwargs = {
            "config_path": options["config_path"],
            "mode": options["mode"],
            "env_file": options["env_file"],
        }
        if options["mode"] == "maas" and options["api_key"]:
            parser_kwargs["api_key"] = options["api_key"]
        if options["mode"] == "selfhosted":
            self._ensure_selfhosted_server()
            parser_kwargs["ocr_api_host"] = SELFHOSTED_HOST
            parser_kwargs["ocr_api_port"] = SELFHOSTED_PORT

        try:
            with GlmOcr(**parser_kwargs) as parser:
                self.current_parser = parser
                for index, file_path in enumerate(files, start=1):
                    if self.cancel_event.is_set():
                        self.queue.put(("status", "任务已取消"))
                        self.queue.put(("log", "在处理新文件前停止"))
                        break

                    name = Path(file_path).name
                    self.queue.put(("status", f"识别中: {name}"))
                    self.queue.put(("progress", f"{index}/{len(files)}"))
                    self.queue.put(("log", f"[{index}/{len(files)}] 开始处理 {file_path}"))

                    parse_kwargs = {
                        "save_layout_visualization": options["save_layout_visualization"],
                    }
                    if options["mode"] == "maas":
                        if options["start_page_id"] is not None:
                            parse_kwargs["start_page_id"] = options["start_page_id"]
                        if options["end_page_id"] is not None:
                            parse_kwargs["end_page_id"] = options["end_page_id"]

                    result = parser.parse(file_path, **parse_kwargs)
                    result.save(
                        output_dir=output_dir,
                        save_layout_visualization=options["save_layout_visualization"],
                    )

                    saved_dir = output_dir / Path(file_path).stem
                    self.last_saved_paths.append(saved_dir)
                    self.last_result = result.to_dict()
                    summary = self._build_summary(file_path, saved_dir, result)
                    json_text = result.to_json()
                    markdown_text = result.markdown_result or ""

                    self.queue.put(("summary", summary))
                    self.queue.put(("markdown", markdown_text))
                    self.queue.put(("json", json_text))
                    self.queue.put(("log", f"[{index}/{len(files)}] 已输出到 {saved_dir}"))

                else:
                    self.queue.put(("status", "全部处理完成"))
                    self.queue.put(("progress", f"已完成 {len(files)} 个文件"))
                    self.queue.put(("log", "任务完成"))
                    return

                self.queue.put(("progress", "已停止"))
        except MissingApiKeyError as exc:
            self.queue.put(("error", f"缺少 API Key: {exc}"))
        except Exception as exc:
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            self.current_parser = None

    def _build_summary(self, file_path: str, saved_dir: Path, result) -> str:
        info = result.to_dict()
        lines = [
            f"输入文件: {file_path}",
            f"输出目录: {saved_dir}",
            f"结果类型: {type(info.get('json_result')).__name__}",
            f"Markdown 长度: {len(info.get('markdown_result') or '')} 字符",
        ]
        usage = info.get("usage")
        if usage:
            lines.append("usage:")
            lines.append(json.dumps(usage, ensure_ascii=False, indent=2))
        error = info.get("error")
        if error:
            lines.append(f"错误: {error}")
        return "\n".join(lines)

    def _poll_queue(self) -> None:
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except Empty:
                break

            if kind == "status":
                self.status_var.set(payload)
            elif kind == "progress":
                self.progress_var.set(payload)
            elif kind == "summary":
                self._set_text(self.summary_text, payload)
            elif kind == "markdown":
                self._set_text(self.markdown_text, payload)
            elif kind == "json":
                self._set_text(self.json_text, payload)
            elif kind == "log":
                self._append_log(payload)
            elif kind == "error":
                self.status_var.set("执行失败")
                self.progress_var.set("已停止")
                self._append_log(payload)
                messagebox.showerror("GLM-OCR 运行失败", payload)

        self.root.after(150, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    GlmOcrGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
