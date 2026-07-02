from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .core import (
    DEFAULT_SITE_URL,
    BridgeHTTPServer,
    BridgeSession,
    ExportJob,
    ExportProgress,
    ExportedGraph,
    OriginBatchExporter,
    OriginInstallationInfo,
    build_import_url,
    collect_project_files,
    create_figgrid,
    discover_origin_executable,
    open_in_edge,
    probe_origin_environment,
    start_bridge_server,
    write_export_report,
)


class OriginBridgeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Origin 批量排版助手")
        self.root.geometry("1040x850")
        self.root.minsize(900, 720)

        discovered = discover_origin_executable()
        self.origin_var = tk.StringVar(value=str(discovered or ""))
        self.output_var = tk.StringVar(value=str(Path.home() / "Documents"))
        self.site_var = tk.StringVar(value=DEFAULT_SITE_URL)
        self.title_var = tk.StringVar(value="Origin 批量排版")
        self.status_var = tk.StringVar(value="添加 Origin 项目后开始批量导出。")
        self.origin_status_var = tk.StringVar(value="尚未检测 Origin 环境")
        self.count_var = tk.StringVar(value="已选择 0/12 张")
        self.progress_var = tk.DoubleVar(value=0)

        self.projects: list[Path] = []
        self.job: ExportJob | None = None
        self.graphs: list[ExportedGraph] = []
        self.selected: dict[str, tk.BooleanVar] = {}
        self.thumbnail_refs: list[ImageTk.PhotoImage] = []
        self.exporter: OriginBatchExporter | None = None
        self.origin_info: OriginInstallationInfo | None = None
        self.probe_signature: tuple[str, int, int] | None = None
        self.bridge_server: BridgeHTTPServer | None = None
        self.bridge_session: BridgeSession | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

        container = ttk.Frame(self.root, padding=18)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="Origin 批量排版助手", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            container,
            text="批量导出 .opju/.opj 图页，选择 2–12 张后自动送入 Layout Assistant。",
        ).pack(anchor="w", pady=(3, 14))

        settings = ttk.LabelFrame(container, text="运行设置", padding=10)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Origin").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(settings, textvariable=self.origin_var).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(settings, text="选择…", command=self._choose_origin).grid(
            row=0, column=2, padx=(8, 0)
        )
        self.check_origin_button = ttk.Button(
            settings, text="检测环境", command=self._check_environment
        )
        self.check_origin_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Label(settings, textvariable=self.origin_status_var).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(5, 0)
        )

        ttk.Label(settings, text="输出目录").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        ttk.Entry(settings, textvariable=self.output_var).grid(
            row=2, column=1, sticky="ew", pady=(8, 0)
        )
        ttk.Button(settings, text="选择…", command=self._choose_output).grid(
            row=2, column=2, padx=(8, 0), pady=(8, 0)
        )

        sources = ttk.LabelFrame(container, text="Origin 项目", padding=10)
        sources.pack(fill="x", pady=(12, 0))
        toolbar = ttk.Frame(sources)
        toolbar.pack(fill="x")
        self.add_files_button = ttk.Button(toolbar, text="添加文件", command=self._add_files)
        self.add_files_button.pack(side="left")
        self.add_folder_button = ttk.Button(
            toolbar, text="添加文件夹", command=self._add_folder
        )
        self.add_folder_button.pack(side="left", padx=(8, 0))
        self.clear_button = ttk.Button(toolbar, text="清空", command=self._clear_projects)
        self.clear_button.pack(side="left", padx=(8, 0))

        self.project_table = ttk.Treeview(
            sources,
            columns=("project", "status", "stage", "attempt"),
            show="headings",
            height=5,
        )
        self.project_table.heading("project", text="项目")
        self.project_table.heading("status", text="状态")
        self.project_table.heading("stage", text="阶段")
        self.project_table.heading("attempt", text="尝试")
        self.project_table.column("project", width=570, stretch=True)
        self.project_table.column("status", width=90, anchor="center", stretch=False)
        self.project_table.column("stage", width=90, anchor="center", stretch=False)
        self.project_table.column("attempt", width=55, anchor="center", stretch=False)
        self.project_table.pack(fill="x", pady=(8, 0))

        action_row = ttk.Frame(container)
        action_row.pack(fill="x", pady=(12, 0))
        self.export_button = ttk.Button(
            action_row, text="扫描并导出全部图页", command=self._start_export
        )
        self.export_button.pack(side="left")
        self.cancel_button = ttk.Button(
            action_row, text="取消", command=self._cancel_export, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.continue_button = ttk.Button(
            action_row,
            text="继续未完成",
            command=self._continue_export,
            state="disabled",
        )
        self.continue_button.pack(side="left", padx=(8, 0))
        self.retry_button = ttk.Button(
            action_row,
            text="重试失败项目",
            command=self._retry_failed,
            state="disabled",
        )
        self.retry_button.pack(side="left", padx=(8, 0))
        self.log_button = ttk.Button(
            action_row, text="查看日志", command=self._open_log, state="disabled"
        )
        self.log_button.pack(side="left", padx=(8, 0))
        ttk.Label(action_row, textvariable=self.status_var).pack(side="left", padx=(14, 0))
        ttk.Progressbar(container, variable=self.progress_var, maximum=100).pack(
            fill="x", pady=(6, 0)
        )

        selection = ttk.LabelFrame(container, text="选择本次排版图片", padding=10)
        selection.pack(fill="both", expand=True, pady=(12, 0))
        selection_header = ttk.Frame(selection)
        selection_header.pack(fill="x")
        ttk.Label(selection_header, textvariable=self.count_var).pack(side="left")
        ttk.Label(selection_header, text="按当前顺序生成面板").pack(side="right")

        self.graph_canvas = tk.Canvas(selection, highlightthickness=0, height=250)
        scrollbar = ttk.Scrollbar(
            selection, orient="vertical", command=self.graph_canvas.yview
        )
        self.graph_frame = ttk.Frame(self.graph_canvas)
        self.graph_window = self.graph_canvas.create_window(
            (0, 0), window=self.graph_frame, anchor="nw"
        )
        self.graph_canvas.configure(yscrollcommand=scrollbar.set)
        self.graph_canvas.pack(side="left", fill="both", expand=True, pady=(8, 0))
        scrollbar.pack(side="right", fill="y", pady=(8, 0))
        self.graph_frame.bind(
            "<Configure>",
            lambda _event: self.graph_canvas.configure(
                scrollregion=self.graph_canvas.bbox("all")
            ),
        )
        self.graph_canvas.bind(
            "<Configure>",
            lambda event: self.graph_canvas.itemconfigure(
                self.graph_window, width=event.width
            ),
        )

        handoff = ttk.LabelFrame(container, text="生成工程并排版", padding=10)
        handoff.pack(fill="x", pady=(12, 0))
        handoff.columnconfigure(1, weight=1)
        ttk.Label(handoff, text="工程名称").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(handoff, textvariable=self.title_var).grid(
            row=0, column=1, sticky="ew"
        )
        self.handoff_button = ttk.Button(
            handoff,
            text="生成 .figgrid 并一键排版",
            command=self._create_and_handoff,
            state="disabled",
        )
        self.handoff_button.grid(row=0, column=2, padx=(8, 0))
        self.open_folder_button = ttk.Button(
            handoff, text="打开输出目录", command=self._open_output, state="disabled"
        )
        self.open_folder_button.grid(row=0, column=3, padx=(8, 0))

        ttk.Label(
            container,
            text="所有项目和图片只在本机处理；源 .opju/.opj 不会被修改。",
        ).pack(anchor="w", pady=(10, 0))

    def _choose_origin(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Origin64.exe",
            filetypes=[("Origin", "Origin*.exe"), ("程序", "*.exe")],
        )
        if path:
            self.origin_var.set(path)
            self.origin_info = None
            self.probe_signature = None
            self.origin_status_var.set("Origin 路径已更改，请重新检测环境")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择批量导出目录")
        if path:
            self.output_var.set(path)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 Origin 项目",
            filetypes=[("Origin 项目", "*.opju *.opj"), ("所有文件", "*.*")],
        )
        self._merge_projects(paths)

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="选择包含 Origin 项目的文件夹")
        if path:
            self._merge_projects([path])

    def _merge_projects(self, paths: list[str] | tuple[str, ...]) -> None:
        combined = collect_project_files([*self.projects, *paths])
        self.projects = combined
        self.job = None
        self._refresh_project_table()
        self.status_var.set(f"已添加 {len(self.projects)} 个 Origin 项目。")
        self._set_export_busy(False)

    def _clear_projects(self) -> None:
        self.projects = []
        self.job = None
        self._refresh_project_table()
        self.status_var.set("项目列表已清空。")
        self._set_export_busy(False)

    def _refresh_project_table(self) -> None:
        for item in self.project_table.get_children():
            self.project_table.delete(item)
        results = {
            result.project_index: result for result in self.job.results
        } if self.job else {}
        for index, project in enumerate(self.projects, start=1):
            result = results.get(index)
            self.project_table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    str(project),
                    result.status if result else "pending",
                    result.last_stage if result else "preflight",
                    result.attempt if result else 0,
                ),
            )

    def _set_export_busy(self, busy: bool) -> None:
        normal = "disabled" if busy else "normal"
        for button in (
            self.add_files_button,
            self.add_folder_button,
            self.clear_button,
            self.export_button,
            self.check_origin_button,
        ):
            button.configure(state=normal)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.continue_button.configure(
            state=(
                "normal"
                if not busy and self.job and self.job.pending_results
                else "disabled"
            )
        )
        self.retry_button.configure(
            state=(
                "normal"
                if not busy and self.job and self.job.failed_results
                else "disabled"
            )
        )
        self.log_button.configure(
            state=(
                "normal"
                if not busy
                and self.job
                and (self.job.output_directory / "export.log").is_file()
                else "disabled"
            )
        )

    def _start_export(self) -> None:
        if not self.projects:
            messagebox.showwarning("尚未添加项目", "请先添加 .opju 或 .opj 文件。")
            return
        origin_path = Path(self.origin_var.get().strip())
        output_root = Path(self.output_var.get().strip())
        if not origin_path.is_file():
            messagebox.showerror("Origin 路径无效", "请选择有效的 Origin64.exe。")
            return
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出目录无效", str(exc))
            return
        self._ensure_environment(
            lambda: self._run_export_operation("export", output_root=output_root)
        )

    def _origin_signature(self, path: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
            return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None

    def _check_environment(self) -> None:
        self._ensure_environment(None, force=True)

    def _ensure_environment(
        self,
        continuation,
        *,
        force: bool = False,
    ) -> None:
        origin_path = Path(self.origin_var.get().strip())
        signature = self._origin_signature(origin_path)
        if signature is None:
            messagebox.showerror("Origin 路径无效", "请选择有效的 Origin 可执行文件。")
            return
        if not force and signature == self.probe_signature and self.origin_info:
            if continuation:
                continuation()
            return
        self._set_export_busy(True)
        self.cancel_button.configure(state="disabled")
        self.status_var.set("正在检测 Origin 环境…")
        self.origin_status_var.set("检测中…")

        def worker() -> None:
            try:
                info, result = probe_origin_environment(origin_path)
                if not result.success:
                    raise RuntimeError(result.message)
            except Exception as exc:
                message = str(exc)
                self.root.after(
                    0,
                    lambda message=message: self._environment_failed(message),
                )
                return
            self.root.after(
                0,
                lambda info=info, result=result, signature=signature: (
                    self._environment_finished(info, result.message, signature, continuation)
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _environment_failed(self, message: str) -> None:
        self._set_export_busy(False)
        self.origin_status_var.set("环境检测失败")
        self.status_var.set("Origin 环境不可用。")
        messagebox.showerror("Origin 环境检测失败", message)

    def _environment_finished(
        self,
        info: OriginInstallationInfo,
        message: str,
        signature: tuple[str, int, int],
        continuation,
    ) -> None:
        self.origin_info = info
        self.probe_signature = signature
        self.origin_status_var.set(
            f"{info.marketing_version} · {info.product_version or info.file_version or '版本未知'} · 检测通过"
        )
        self.status_var.set(message)
        self._set_export_busy(False)
        if continuation:
            continuation()

    def _run_export_operation(
        self, operation: str, *, output_root: Path | None = None
    ) -> None:
        origin_path = Path(self.origin_var.get().strip())
        self.exporter = OriginBatchExporter(origin_path)
        if self.job and self.origin_info:
            self.job.origin = self.origin_info
        self._set_export_busy(True)
        self.progress_var.set(0)
        self.status_var.set("正在准备批量导出…")

        def progress(event: ExportProgress) -> None:
            self.root.after(0, lambda event=event: self._apply_progress(event))

        def worker() -> None:
            try:
                if operation == "export":
                    assert output_root is not None
                    job = self.exporter.export(
                        self.projects, output_root, progress=progress
                    )
                elif operation == "resume":
                    assert self.job is not None
                    job = self.exporter.resume(self.job, progress=progress)
                else:
                    assert self.job is not None
                    job = self.exporter.retry_failed(self.job, progress=progress)
            except Exception as exc:
                self._schedule_export_failure(exc)
                return
            self.root.after(0, lambda job=job: self._export_finished(job))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_progress(self, event: ExportProgress) -> None:
        self.status_var.set(f"[{event.project_index}/{event.total}] {event.message}")
        self.progress_var.set(event.project_index / max(event.total, 1) * 100)
        item = str(event.project_index)
        if self.project_table.exists(item):
            values = list(self.project_table.item(item, "values"))
            while len(values) < 4:
                values.append("")
            values[1] = event.status
            values[2] = event.stage
            if self.job and event.project_index <= len(self.job.results):
                values[3] = self.job.results[event.project_index - 1].attempt
            self.project_table.item(item, values=values)

    def _continue_export(self) -> None:
        if self.job:
            self._ensure_environment(
                lambda: self._run_export_operation("resume")
            )

    def _retry_failed(self) -> None:
        if self.job:
            self._ensure_environment(
                lambda: self._run_export_operation("retry")
            )

    def _schedule_export_failure(self, error: Exception) -> None:
        message = str(error)
        self.root.after(0, lambda message=message: self._export_failed(message))

    def _cancel_export(self) -> None:
        if self.exporter:
            self.exporter.cancel()
            self.status_var.set("正在取消当前任务…")

    def _export_failed(self, error: str) -> None:
        self._set_export_busy(False)
        self.status_var.set("批量导出失败。")
        messagebox.showerror("批量导出失败", error)

    def _export_finished(self, job: ExportJob) -> None:
        self.job = job
        self.projects = [Path(project) for project in job.projects]
        self._refresh_project_table()
        self._set_export_busy(False)
        self.progress_var.set(100 if job.status != "cancelled" else self.progress_var.get())
        self.graphs = [graph for graph in job.graphs if graph.eligible]
        self.selected = {
            graph.id: tk.BooleanVar(value=index < 12)
            for index, graph in enumerate(self.graphs)
        }
        self._render_graphs()
        failed = sum(result.status != "success" for result in job.results)
        self.status_var.set(
            f"任务结束（{job.status}）：{len(job.graphs)} 张图片"
            + (f"，{failed} 个项目有问题。" if failed else "。")
        )
        self.open_folder_button.configure(state="normal")
        if not self.graphs:
            messagebox.showwarning(
                "没有可排版图片", "未导出符合网站限制的 PNG，请查看 export-report.json。"
            )

    def _render_graphs(self) -> None:
        for child in self.graph_frame.winfo_children():
            child.destroy()
        self.thumbnail_refs.clear()
        for index, graph in enumerate(self.graphs):
            row = ttk.Frame(self.graph_frame, padding=(2, 5))
            row.pack(fill="x")
            check = ttk.Checkbutton(
                row,
                variable=self.selected[graph.id],
                command=self._update_selection_state,
            )
            check.pack(side="left")
            try:
                with Image.open(graph.image_path) as image:
                    preview = image.convert("RGB")
                    preview.thumbnail((112, 76))
                    thumbnail = ImageTk.PhotoImage(preview)
                self.thumbnail_refs.append(thumbnail)
                ttk.Label(row, image=thumbnail).pack(side="left", padx=(6, 10))
            except Exception:
                ttk.Label(row, text="预览失败", width=14).pack(
                    side="left", padx=(6, 10)
                )
            ttk.Label(
                row,
                text=(
                    f"{graph.source_name} / {graph.graph_name}\n"
                    f"{graph.width} × {graph.height} · {graph.size / 1024 / 1024:.2f} MB"
                ),
            ).pack(side="left", fill="x", expand=True)
            ttk.Button(
                row,
                text="↑",
                width=3,
                state="disabled" if index == 0 else "normal",
                command=lambda i=index: self._move_graph(i, -1),
            ).pack(side="right")
            ttk.Button(
                row,
                text="↓",
                width=3,
                state="disabled" if index == len(self.graphs) - 1 else "normal",
                command=lambda i=index: self._move_graph(i, 1),
            ).pack(side="right", padx=(0, 4))
            ttk.Separator(self.graph_frame).pack(fill="x")
        self._update_selection_state()

    def _move_graph(self, index: int, direction: int) -> None:
        target = index + direction
        if target < 0 or target >= len(self.graphs):
            return
        self.graphs[index], self.graphs[target] = self.graphs[target], self.graphs[index]
        self._render_graphs()

    def _selected_graphs(self) -> list[ExportedGraph]:
        return [graph for graph in self.graphs if self.selected[graph.id].get()]

    def _update_selection_state(self) -> None:
        count = len(self._selected_graphs())
        self.count_var.set(f"已选择 {count}/12 张")
        self.handoff_button.configure(
            state="normal" if self.job and 2 <= count <= 12 else "disabled"
        )

    def _create_and_handoff(self) -> None:
        if not self.job:
            return
        selected = self._selected_graphs()
        if not 2 <= len(selected) <= 12:
            messagebox.showwarning("图片数量无效", "请选择 2–12 张图片。")
            return
        project_path = self.job.output_directory / "selected.figgrid"
        try:
            create_figgrid(selected, project_path, self.title_var.get())
            write_export_report(self.job, selected)
            self._stop_bridge()
            server, session, _thread = start_bridge_server(project_path)
            self.bridge_server = server
            self.bridge_session = session
            url = build_import_url(
                self.site_var.get(), server.server_address[1], session.token
            )
            if not open_in_edge(url):
                raise RuntimeError("无法打开浏览器。")
            self.status_var.set("工程已生成，正在等待网站接收…")
            self.root.after(1000, self._poll_bridge)
        except Exception as exc:
            messagebox.showerror(
                "无法一键排版",
                f"{exc}\n\n工程包仍可在输出目录中手动导入。",
            )

    def _poll_bridge(self) -> None:
        session = self.bridge_session
        if not session:
            return
        if session.completed.is_set():
            self.status_var.set("网站已接收工程，可以开始排版。")
            self._stop_bridge()
            return
        if session.expired:
            self.status_var.set("自动导入已过期，请在网站中手动导入 selected.figgrid。")
            self._stop_bridge()
            return
        self.root.after(1000, self._poll_bridge)

    def _stop_bridge(self) -> None:
        if self.bridge_server:
            self.bridge_server.shutdown()
            self.bridge_server.server_close()
        self.bridge_server = None
        self.bridge_session = None

    def _open_output(self) -> None:
        if self.job and self.job.output_directory.exists():
            os.startfile(self.job.output_directory)

    def _open_log(self) -> None:
        if not self.job:
            return
        log_path = self.job.output_directory / "export.log"
        if log_path.is_file():
            os.startfile(log_path)

    def _on_close(self) -> None:
        if self.exporter:
            self.exporter.cancel()
        self._stop_bridge()
        self.root.destroy()


def main() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    OriginBridgeApp(root)
    root.mainloop()
