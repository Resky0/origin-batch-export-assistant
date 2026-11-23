from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urlencode

from PIL import Image


SUPPORTED_PROJECT_EXTENSIONS = {".opju", ".opj"}
APP_VERSION = "0.6.0"
REPORT_VERSION = 2
ORIGIN_READY_MARKER = "ORIGIN_BRIDGE_READY"
ORIGIN_OPENED_MARKER = "ORIGIN_BRIDGE_STAGE_OPENED"
ORIGIN_EXPORTED_MARKER = "ORIGIN_BRIDGE_STAGE_EXPORTED"
ORIGIN_DONE_MARKER = "ORIGIN_BRIDGE_STAGE_DONE"
DEFAULT_SITE_URL = "https://layout-assistant.xyz"
DEFAULT_ALLOWED_ORIGINS = {
    "https://layout-assistant.xyz",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:4174",
    "http://127.0.0.1:4174",
}


@dataclass(frozen=True)
class BridgeLimits:
    min_panels: int = 2
    max_panels: int = 12
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 150 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    export_width: int = 3000


class ExportStage(str, Enum):
    PREFLIGHT = "preflight"
    STAGING = "staging"
    LAUNCHING = "launching"
    WAITING = "waiting"
    VALIDATING = "validating"
    COMPLETE = "complete"


@dataclass(frozen=True)
class OriginInstallationInfo:
    executable_path: str
    product_name: str
    product_version: str
    file_version: str
    marketing_version: str
    version_tuple: tuple[int, ...]
    supports_opju: bool | None
    warning: str | None = None


@dataclass(frozen=True)
class OriginProbeResult:
    success: bool
    message: str
    elapsed_seconds: float
    log_path: str


@dataclass(frozen=True)
class ExportProgress:
    project_index: int
    total: int
    source_path: str
    stage: str
    status: str
    message: str


@dataclass
class ExportedGraph:
    id: str
    source_path: str
    source_name: str
    graph_index: int
    graph_name: str
    image_path: str
    width: int
    height: int
    size: int
    eligible: bool = True
    error: str | None = None


@dataclass
class ProjectExportResult:
    project_index: int
    source_path: str
    status: str = "pending"
    attempt: int = 0
    source_sha256_before: str = ""
    source_sha256_after: str = ""
    graphs: list[ExportedGraph] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    last_stage: str = ExportStage.PREFLIGHT.value
    log_path: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class ExportJob:
    output_directory: Path
    images_directory: Path
    origin: OriginInstallationInfo
    projects: list[str]
    results: list[ProjectExportResult]
    status: str = "pending"
    selected_graph_ids: list[str] = field(default_factory=list)

    @property
    def graphs(self) -> list[ExportedGraph]:
        return [graph for result in self.results for graph in result.graphs]

    @property
    def pending_results(self) -> list[ProjectExportResult]:
        return [
            result
            for result in self.results
            if result.status in {"pending", "cancelled"}
        ]

    @property
    def failed_results(self) -> list[ProjectExportResult]:
        return [
            result
            for result in self.results
            if result.status in {"failed", "timeout", "no-graphs", "source-changed"}
        ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_project_files(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    collected: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        candidates: Iterable[Path]
        if path.is_dir():
            candidates = (
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix.lower() in SUPPORTED_PROJECT_EXTENSIONS
            )
        elif path.is_file() and path.suffix.lower() in SUPPORTED_PROJECT_EXTENSIONS:
            candidates = [path]
        else:
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            collected.append(resolved)
    return collected


def _registry_install_locations() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
    uninstall_keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    locations: list[Path] = []
    for root in roots:
        for uninstall_key in uninstall_keys:
            try:
                with winreg.OpenKey(root, uninstall_key) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            child_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, child_name) as child:
                                display_name, _ = winreg.QueryValueEx(child, "DisplayName")
                                if not str(display_name).lower().startswith("origin"):
                                    continue
                                install_location, _ = winreg.QueryValueEx(
                                    child, "InstallLocation"
                                )
                                if install_location:
                                    locations.append(Path(str(install_location)))
                        except OSError:
                            continue
            except OSError:
                continue
    return locations


def _start_menu_shortcut_targets() -> list[Path]:
    if os.name != "nt":
        return []
    script = r"""
$roots = @(
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
  "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
)
$shell = New-Object -ComObject WScript.Shell
Get-ChildItem $roots -Filter '*.lnk' -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^Origin' -or $_.DirectoryName -match 'OriginLab Origin' } |
  ForEach-Object { $shell.CreateShortcut($_.FullName).TargetPath }
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [Path(line.strip()) for line in completed.stdout.splitlines() if line.strip()]


def _origin_executables_under(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    candidates = list(directory.glob("Origin*_64.exe"))
    candidates.extend(directory.glob("Origin64.exe"))
    candidates.extend(directory.glob("Origin*.exe"))
    return candidates


def discover_origin_executable(configured: str | os.PathLike[str] | None = None) -> Path | None:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    if os.environ.get("ORIGIN_EXE"):
        candidates.append(Path(os.environ["ORIGIN_EXE"]))

    for location in _registry_install_locations():
        candidates.extend(_origin_executables_under(location))
    candidates.extend(_start_menu_shortcut_targets())

    common_roots = [
        Path(r"C:\Program Files\OriginLab"),
        Path(r"C:\Program Files (x86)\OriginLab"),
        Path(r"D:\SofeWare\Origin"),
    ]
    for root in common_roots:
        if root.name.lower() == "origin":
            candidates.extend(_origin_executables_under(root))
        elif root.exists():
            for directory in root.glob("Origin*"):
                candidates.extend(_origin_executables_under(directory))

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file() and resolved.name.lower().startswith("origin"):
            return resolved
    return None


def _file_version_strings(path: Path) -> dict[str, str]:
    if os.name != "nt":
        return {}
    try:
        version = ctypes.windll.version
        handle = ctypes.c_uint(0)
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
        if not size:
            return {}
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, data):
            return {}
        translation_ptr = ctypes.c_void_p()
        translation_length = ctypes.c_uint(0)
        if not version.VerQueryValueW(
            data,
            r"\VarFileInfo\Translation",
            ctypes.byref(translation_ptr),
            ctypes.byref(translation_length),
        ):
            return {}
        translations = ctypes.cast(
            translation_ptr, ctypes.POINTER(ctypes.c_ushort)
        )
        language, codepage = translations[0], translations[1]
        values: dict[str, str] = {}
        for key in ("ProductName", "ProductVersion", "FileVersion"):
            value_ptr = ctypes.c_void_p()
            value_length = ctypes.c_uint(0)
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            if version.VerQueryValueW(
                data,
                query,
                ctypes.byref(value_ptr),
                ctypes.byref(value_length),
            ) and value_ptr.value:
                values[key] = ctypes.wstring_at(value_ptr.value).strip()
        return values
    except (AttributeError, OSError, ValueError, IndexError):
        return {}


def _parse_version(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers[:4])


def _marketing_version(version: tuple[int, ...]) -> str:
    if len(version) < 2:
        return "未知版本"
    known = {
        (9, 5): "Origin 2018",
        (9, 6): "Origin 2019",
        (9, 7): "Origin 2020",
        (9, 8): "Origin 2021",
        (9, 9): "Origin 2022",
        (10, 0): "Origin 2023",
        (10, 1): "Origin 2024",
        (10, 2): "Origin 2025",
        (11, 0): "Origin 2026",
    }
    return known.get(version[:2], f"Origin {version[0]}.{version[1]}")


def inspect_origin_installation(origin_executable: Path) -> OriginInstallationInfo:
    executable = Path(origin_executable).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError("未找到有效的 Origin 可执行文件。")
    if not executable.name.casefold().startswith("origin"):
        raise ValueError("所选程序不是 Origin 可执行文件。")
    values = _file_version_strings(executable)
    product_name = values.get("ProductName", "Origin")
    product_version = values.get("ProductVersion", "")
    file_version = values.get("FileVersion", "")
    version_tuple = _parse_version(product_version or file_version)
    supports_opju = None if len(version_tuple) < 2 else version_tuple[:2] >= (9, 5)
    warning = None
    if not version_tuple:
        warning = "无法读取 Origin 版本，将通过隐藏启动探针验证兼容性。"
    elif supports_opju is False:
        warning = "该版本早于 Origin 2018，只能处理 .opj 项目。"
    return OriginInstallationInfo(
        executable_path=str(executable),
        product_name=product_name,
        product_version=product_version,
        file_version=file_version,
        marketing_version=_marketing_version(version_tuple),
        version_tuple=version_tuple,
        supports_opju=supports_opju,
        warning=warning,
    )


def validate_origin_projects(
    installation: OriginInstallationInfo, projects: Sequence[Path]
) -> None:
    missing = [str(project) for project in projects if not Path(project).is_file()]
    if missing:
        raise FileNotFoundError(f"项目文件不存在：{missing[0]}")
    unsupported = [
        str(project)
        for project in projects
        if Path(project).suffix.casefold() == ".opju"
        and installation.supports_opju is False
    ]
    if unsupported:
        raise RuntimeError("Origin 2017 及更早版本无法打开 .opju 项目。")


def _matching_processes(tasklist_output: str, executable_name: str) -> list[tuple[int, str]]:
    expected_name = Path(executable_name).name.casefold()
    matches: list[tuple[int, str]] = []
    for row in csv.reader(tasklist_output.splitlines()):
        if len(row) < 2 or row[0].casefold() != expected_name:
            continue
        try:
            matches.append((int(row[1]), row[0]))
        except ValueError:
            continue
    return matches


def running_origin_processes(origin_executable: Path) -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _matching_processes(completed.stdout, origin_executable.name)


def _read_text_best_effort(path: Path) -> str:
    if not path.is_file():
        return ""
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeError, OSError):
            continue
    return ""


def probe_origin_environment(
    origin_executable: Path, *, timeout_seconds: int = 60
) -> tuple[OriginInstallationInfo, OriginProbeResult]:
    installation = inspect_origin_installation(origin_executable)
    if running_origin_processes(Path(installation.executable_path)):
        raise RuntimeError("检测到所选 Origin 正在运行。请先保存工作并关闭 Origin。")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="layout_origin_probe_") as directory:
        log_path = Path(directory) / "origin-probe.log"
        script = f'type "{ORIGIN_READY_MARKER}";doc -s;exit;'
        process = subprocess.Popen(
            build_origin_command_line(Path(installation.executable_path), script, log_path),
            executable=installation.executable_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=10)
            raise RuntimeError(
                "Origin 环境检测超时，可能存在许可证、首次启动或隐藏弹窗。"
            ) from exc
        log_text = _read_text_best_effort(log_path)
        # Origin 2021 returns a large application-specific code even after a
        # successful scripted exit, so the SLOG marker is the reliable signal.
        success = ORIGIN_READY_MARKER in log_text
        message = (
            "Origin 隐藏启动和 LabTalk 执行正常。"
            if success
            else "Origin 已退出，但未返回环境检测完成标记。"
        )
        return installation, OriginProbeResult(
            success=success,
            message=message,
            elapsed_seconds=round(time.monotonic() - started, 3),
            log_path="",
        )


def _safe_filename(value: str, fallback: str = "project") -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned[:80] or fallback).strip(" .")


def _natural_graph_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.stem)
    return (int(match.group(1)) if match else 0, path.name.lower())


def build_labtalk_script(source_copy: Path, export_directory: Path, width: int) -> str:
    # Paths are created by this tool in an ASCII-only temporary directory.
    return (
        f'doc -o "{source_copy}";'
        f'type "{ORIGIN_OPENED_MARKER}";'
        f'expGraph type:=png path:="{export_directory}" export:=project '
        f'filename:=Graph## tr1.width:={int(width)} tr1.unit:=2;'
        f'type "{ORIGIN_EXPORTED_MARKER}";'
        # expGraph changes the active project's export settings. Clear Origin's
        # modified flag on the disposable copy so exit never prompts to save it.
        f'doc -s;type "{ORIGIN_DONE_MARKER}";exit;'
    )


def build_origin_command_line(
    origin_executable: Path, script: str, log_path: Path | None = None
) -> str:
    # Origin's -RS switch consumes the rest of the raw Windows command line.
    # Passing the script as a normal argv item adds quotes that Origin 2021 does
    # not parse consistently, so preserve the validated command-line shape.
    executable = str(origin_executable).replace('"', '')
    switches = "-HS"
    if log_path is not None:
        safe_log_path = str(log_path).replace('"', "")
        switches += f' -SLOG "{safe_log_path}"'
    return f'"{executable}" {switches} -RS {script}'


def _copy_exported_image(source: Path, target: Path) -> Path:
    candidate = target
    suffix = 2
    while True:
        try:
            output = candidate.open("xb")
        except FileExistsError:
            candidate = target.with_name(f"{target.stem}_{suffix}{target.suffix}")
            suffix += 1
            continue
        try:
            with output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output)
        except OSError:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


class OriginBatchExporter:
    def __init__(
        self,
        origin_executable: Path,
        *,
        limits: BridgeLimits | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.origin_executable = Path(origin_executable)
        self.limits = limits or BridgeLimits()
        self.timeout_seconds = timeout_seconds
        self.cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[bytes] | None = None
        self.installation: OriginInstallationInfo | None = None

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._process_lock:
            process = self._active_process
            if process and process.poll() is None:
                process.kill()

    def export(
        self,
        projects: Sequence[Path],
        output_root: Path,
        *,
        progress: Callable[[ExportProgress], None] | None = None,
    ) -> ExportJob:
        self.installation = inspect_origin_installation(self.origin_executable)
        if running_origin_processes(self.origin_executable):
            raise RuntimeError("检测到 Origin 正在运行。请先保存工作并关闭 Origin。")
        if not projects:
            raise ValueError("请至少添加一个 .opju 或 .opj 文件。")
        normalized_projects = [Path(project).expanduser().resolve() for project in projects]
        validate_origin_projects(self.installation, normalized_projects)

        output_directory = Path(output_root)
        images_directory = output_directory
        images_directory.mkdir(parents=True, exist_ok=True)
        results = [
            ProjectExportResult(project_index=index, source_path=str(source))
            for index, source in enumerate(normalized_projects, start=1)
        ]
        job = ExportJob(
            output_directory=output_directory,
            images_directory=images_directory,
            origin=self.installation,
            projects=[str(project) for project in normalized_projects],
            results=results,
        )
        return self._run_job(job, results, progress=progress)

    def resume(
        self,
        job: ExportJob,
        *,
        progress: Callable[[ExportProgress], None] | None = None,
    ) -> ExportJob:
        return self._run_job(job, job.pending_results, progress=progress)

    def retry_failed(
        self,
        job: ExportJob,
        *,
        progress: Callable[[ExportProgress], None] | None = None,
    ) -> ExportJob:
        return self._run_job(job, job.failed_results, progress=progress)

    def _run_job(
        self,
        job: ExportJob,
        targets: Sequence[ProjectExportResult],
        *,
        progress: Callable[[ExportProgress], None] | None,
    ) -> ExportJob:
        if running_origin_processes(self.origin_executable):
            raise RuntimeError("检测到 Origin 正在运行。请先保存工作并关闭 Origin。")
        validate_origin_projects(job.origin, [Path(result.source_path) for result in targets])
        self.installation = job.origin
        self.cancel_event.clear()
        job.status = "running"
        temp_root = Path(tempfile.mkdtemp(prefix="layout_origin_"))
        try:
            total = len(job.results)
            for target in targets:
                if self.cancel_event.is_set():
                    break
                self._cleanup_project_outputs(job, target)
                target.status = "running"
                target.attempt += 1
                target.error = None
                target.error_code = None
                target.graphs = []
                try:
                    result = self._export_one(
                        Path(target.source_path),
                        target.project_index,
                        target.attempt,
                        temp_root,
                        job,
                        progress,
                    )
                except Exception as exc:
                    result = ProjectExportResult(
                        project_index=target.project_index,
                        source_path=target.source_path,
                        status="failed",
                        attempt=target.attempt,
                        error=f"项目导出发生未预期错误：{exc}",
                        error_code="UNEXPECTED_ERROR",
                        last_stage=target.last_stage,
                    )
                    self._emit(
                        progress,
                        project_index=target.project_index,
                        total=total,
                        source=Path(target.source_path),
                        stage=ExportStage.COMPLETE,
                        status="failed",
                        message=result.error,
                    )
                job.results[target.project_index - 1] = result
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
        if self.cancel_event.is_set():
            job.status = "cancelled"
        elif any(result.status in {"pending", "cancelled"} for result in job.results):
            job.status = "incomplete"
        elif any(result.status != "success" for result in job.results):
            job.status = "completed-with-errors"
        else:
            job.status = "success"
        return job

    def _emit(
        self,
        progress: Callable[[ExportProgress], None] | None,
        *,
        project_index: int,
        total: int,
        source: Path,
        stage: ExportStage,
        status: str,
        message: str,
    ) -> None:
        if progress:
            progress(
                ExportProgress(
                    project_index=project_index,
                    total=total,
                    source_path=str(source),
                    stage=stage.value,
                    status=status,
                    message=message,
                )
            )

    def _cleanup_project_outputs(
        self, job: ExportJob, result: ProjectExportResult
    ) -> None:
        root = job.images_directory.resolve()
        candidates = {Path(graph.image_path) for graph in result.graphs}
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                continue

    def _export_one(
        self,
        source: Path,
        project_index: int,
        attempt: int,
        temp_root: Path,
        job: ExportJob,
        progress: Callable[[ExportProgress], None] | None,
    ) -> ProjectExportResult:
        started = time.monotonic()
        total = len(job.results)
        origin_log_temp = temp_root / (
            f"origin_{project_index:04d}_attempt_{attempt:02d}.log"
        )
        result = ProjectExportResult(
            project_index=project_index,
            source_path=str(source),
            status="running",
            attempt=attempt,
        )
        try:
            result.source_sha256_before = sha256_file(source)
        except OSError as exc:
            result.status = "failed"
            result.error_code = "SOURCE_UNAVAILABLE"
            result.error = f"无法读取源项目：{exc}"
            result.last_stage = ExportStage.PREFLIGHT.value
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            self._emit(
                progress,
                project_index=project_index,
                total=total,
                source=source,
                stage=ExportStage.PREFLIGHT,
                status=result.status,
                message=result.error,
            )
            return result

        safe_source = temp_root / (
            f"source_{project_index:04d}_attempt_{attempt:02d}{source.suffix.lower()}"
        )
        safe_output = temp_root / f"export_{project_index:04d}_attempt_{attempt:02d}"
        safe_output.mkdir()
        result.last_stage = ExportStage.STAGING.value
        self._emit(
            progress,
            project_index=project_index,
            total=total,
            source=source,
            stage=ExportStage.STAGING,
            status="running",
            message=f"正在复制临时副本：{source.name}",
        )
        shutil.copy2(source, safe_source)
        script = build_labtalk_script(
            safe_source, safe_output, self.limits.export_width
        )
        status = "success"
        error: str | None = None
        error_code: str | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            result.last_stage = ExportStage.LAUNCHING.value
            origin_log_temp.touch(exist_ok=True)
            self._emit(
                progress,
                project_index=project_index,
                total=total,
                source=source,
                stage=ExportStage.LAUNCHING,
                status="running",
                message=f"正在启动 Origin：{source.name}",
            )
            process = subprocess.Popen(
                build_origin_command_line(
                    self.origin_executable, script, origin_log_temp
                ),
                executable=str(self.origin_executable),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._process_lock:
                self._active_process = process
            result.last_stage = ExportStage.WAITING.value
            self._emit(
                progress,
                project_index=project_index,
                total=total,
                source=source,
                stage=ExportStage.WAITING,
                status="running",
                message=f"Origin 正在导出：{source.name}",
            )
            deadline = time.monotonic() + self.timeout_seconds
            while process.poll() is None:
                if self.cancel_event.is_set():
                    process.kill()
                    process.wait(timeout=10)
                    status = "cancelled"
                    error_code = "CANCELLED"
                    error = "用户已取消批量导出。"
                    break
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(timeout=10)
                    status = "timeout"
                    error_code = "ORIGIN_TIMEOUT"
                    error = (
                        f"Origin 超过 {self.timeout_seconds} 秒未完成，已终止本次导出。"
                    )
                    break
                time.sleep(0.2)
            if self.cancel_event.is_set() and status == "success":
                status = "cancelled"
                error_code = "CANCELLED"
                error = "用户已取消批量导出。"
        except OSError as exc:
            status = "failed"
            error_code = "ORIGIN_START_FAILED"
            error = f"无法启动 Origin：{exc}"
        finally:
            with self._process_lock:
                self._active_process = None

        graphs: list[ExportedGraph] = []
        log_text = _read_text_best_effort(origin_log_temp)
        if ORIGIN_DONE_MARKER in log_text:
            result.last_stage = ExportStage.COMPLETE.value
        elif ORIGIN_EXPORTED_MARKER in log_text:
            result.last_stage = ExportStage.VALIDATING.value
        elif ORIGIN_OPENED_MARKER in log_text:
            result.last_stage = ExportStage.WAITING.value

        if status == "success":
            result.last_stage = ExportStage.VALIDATING.value
            self._emit(
                progress,
                project_index=project_index,
                total=total,
                source=source,
                stage=ExportStage.VALIDATING,
                status="running",
                message=f"正在校验导出图片：{source.name}",
            )
            exported_paths = sorted(safe_output.glob("*.png"), key=_natural_graph_key)
        else:
            exported_paths = []
        for graph_index, exported in enumerate(exported_paths, start=1):
            final_name = (
                f"{project_index:04d}_{_safe_filename(source.stem)}_"
                f"Graph{graph_index:02d}.png"
            )
            final_path = _copy_exported_image(exported, job.images_directory / final_name)
            graph_error: str | None = None
            eligible = True
            try:
                with Image.open(final_path) as image:
                    width, height = image.size
                    image.verify()
                size = final_path.stat().st_size
                if size > self.limits.max_file_bytes:
                    eligible = False
                    graph_error = "图片超过 25MB，不能加入排版工程。"
                elif width * height > self.limits.max_image_pixels:
                    eligible = False
                    graph_error = "图片超过 4000 万像素，不能加入排版工程。"
            except Exception as exc:  # Pillow exposes several decode exceptions.
                width = height = 0
                size = final_path.stat().st_size
                eligible = False
                graph_error = f"导出的 PNG 无法读取：{exc}"
            graphs.append(
                ExportedGraph(
                    id=str(uuid.uuid4()),
                    source_path=str(source),
                    source_name=source.name,
                    graph_index=graph_index,
                    graph_name=f"Graph{graph_index:02d}",
                    image_path=str(final_path),
                    width=width,
                    height=height,
                    size=size,
                    eligible=eligible,
                    error=graph_error,
                )
            )

        if status == "success" and not graphs:
            if ORIGIN_DONE_MARKER in log_text or ORIGIN_EXPORTED_MARKER in log_text:
                status = "no-graphs"
                error_code = "NO_GRAPHS"
                error = "项目中没有找到可导出的独立图页或布局页。"
            else:
                status = "failed"
                error_code = "ORIGIN_EXITED"
                return_code = process.returncode if process else "unknown"
                error = f"Origin 未完成导出脚本，退出代码：{return_code}。"
        try:
            result.source_sha256_after = sha256_file(source)
        except OSError as exc:
            status = "failed"
            error_code = "SOURCE_UNAVAILABLE"
            error = f"导出后无法读取源项目：{exc}"
        if (
            result.source_sha256_after
            and result.source_sha256_after != result.source_sha256_before
        ):
            status = "source-changed"
            error_code = "SOURCE_CHANGED"
            error = "源项目在导出期间发生变化，请检查文件。"
        if error and error_code == "ORIGIN_TIMEOUT":
            error += (
                f" 最后阶段：{result.last_stage}。请检查 Origin 许可证或隐藏弹窗后重试。"
            )
        elif error and error_code in {"ORIGIN_EXITED", "ORIGIN_START_FAILED"}:
            error += f" 最后阶段：{result.last_stage}。"
        result.status = status
        result.graphs = graphs
        result.error = error
        result.error_code = error_code
        result.last_stage = (
            ExportStage.COMPLETE.value
            if status == "success"
            else result.last_stage
        )
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        self._emit(
            progress,
            project_index=project_index,
            total=total,
            source=source,
            stage=ExportStage.COMPLETE,
            status=status,
            message=(
                f"导出完成：{source.name}，{len(graphs)} 张图片"
                if status == "success"
                else f"导出未完成：{source.name}，{error or status}"
            ),
        )
        return result


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def create_figgrid(
    graphs: Sequence[ExportedGraph],
    output_path: Path,
    title: str,
    *,
    limits: BridgeLimits | None = None,
) -> Path:
    limits = limits or BridgeLimits()
    if not limits.min_panels <= len(graphs) <= limits.max_panels:
        raise ValueError(f"排版工程必须包含 {limits.min_panels}–{limits.max_panels} 张图片。")
    if any(not graph.eligible for graph in graphs):
        raise ValueError("选择中包含不符合网站限制的图片。")

    total_bytes = 0
    now = _iso_now()
    project_id = str(uuid.uuid4())
    manifest_assets: list[dict[str, object]] = []
    panel_order: list[str] = []
    panels: dict[str, dict[str, object]] = {}
    asset_files: list[tuple[Path, str]] = []

    for graph in graphs:
        image_path = Path(graph.image_path)
        if not image_path.is_file():
            raise FileNotFoundError(f"图片不存在：{image_path}")
        size = image_path.stat().st_size
        if size > limits.max_file_bytes:
            raise ValueError(f"{image_path.name} 超过 25MB。")
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise ValueError("工程图片总大小超过 150MB。")
        with Image.open(image_path) as image:
            width, height = image.size
            image.verify()
        if width * height > limits.max_image_pixels:
            raise ValueError(f"{image_path.name} 超过 4000 万像素。")
        asset_id = str(uuid.uuid4())
        asset_path = f"assets/{asset_id}.png"
        manifest_assets.append(
            {
                "id": asset_id,
                "name": image_path.name,
                "mime": "image/png",
                "width": width,
                "height": height,
                "size": size,
                "path": asset_path,
                "sha256": sha256_file(image_path),
            }
        )
        panel_order.append(asset_id)
        panels[asset_id] = {
            "assetId": asset_id,
            "fit": "contain",
            "zoom": 1,
            "offsetX": 0,
            "offsetY": 0,
            "hiddenLabel": False,
        }
        asset_files.append((image_path, asset_path))

    manifest = {
        "schemaVersion": 2,
        "id": project_id,
        "title": title.strip()[:80] or "Origin 批量排版",
        "createdAt": now,
        "updatedAt": now,
        "assets": manifest_assets,
        "panelOrder": panel_order,
        "panels": panels,
        "layoutProfile": "classic",
        "exportSettings": {"width": limits.export_width},
        "style": {
            "gap": 20,
            "padding": 24,
            "background": "#ffffff",
            "labelMode": "uppercase",
            "labelPosition": "top-left",
            "labelFont": "arial",
            "labelWeight": 700,
            "labelOffsetX": 13,
            "labelOffsetY": 13,
            "labelSize": 36,
            "labelColor": "#111827",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        for image_path, asset_path in asset_files:
            archive.write(image_path, asset_path)
    return output_path


class BridgeSession:
    def __init__(self, project_path: Path, ttl_seconds: int = 300) -> None:
        self.project_path = Path(project_path)
        self.token = secrets.token_urlsafe(32)
        self.expires_at = time.monotonic() + ttl_seconds
        self.completed = threading.Event()

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        session: BridgeSession,
        allowed_origins: set[str],
    ) -> None:
        self.session = session
        self.allowed_origins = allowed_origins
        super().__init__(address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _expected_paths(self) -> tuple[str, str]:
        base = f"/v1/sessions/{self.server.session.token}"
        return f"{base}/project", f"{base}/complete"

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin in self.server.allowed_origins else None

    def _send_cors_headers(self, origin: str) -> None:
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status: int, payload: dict[str, object], origin: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers(origin)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self) -> tuple[str | None, int | None]:
        origin = self._allowed_origin()
        if not origin:
            self.send_error(HTTPStatus.FORBIDDEN)
            return None, None
        if self.server.session.completed.is_set():
            self._send_json(HTTPStatus.GONE, {"error": "session-completed"}, origin)
            return None, None
        if self.server.session.expired:
            self._send_json(HTTPStatus.GONE, {"error": "session-expired"}, origin)
            return None, None
        return origin, HTTPStatus.OK

    def do_OPTIONS(self) -> None:
        project_path, complete_path = self._expected_paths()
        origin = self._allowed_origin()
        if not origin or self.path not in {project_path, complete_path}:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers(origin)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        project_path, _ = self._expected_paths()
        if self.path != project_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        origin, _ = self._authorize()
        if not origin:
            return
        project = self.server.session.project_path
        try:
            size = project.stat().st_size
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "project-unavailable"},
                origin,
            )
            return
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers(origin)
        self.send_header("Content-Type", "application/x-figgrid")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with project.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def do_POST(self) -> None:
        _, complete_path = self._expected_paths()
        if self.path != complete_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        origin, _ = self._authorize()
        if not origin:
            return
        self.server.session.completed.set()
        self._send_json(HTTPStatus.OK, {"ok": True}, origin)


def start_bridge_server(
    project_path: Path,
    *,
    ttl_seconds: int = 300,
    allowed_origins: set[str] | None = None,
) -> tuple[BridgeHTTPServer, BridgeSession, threading.Thread]:
    session = BridgeSession(project_path, ttl_seconds=ttl_seconds)
    server = BridgeHTTPServer(
        ("127.0.0.1", 0), session, allowed_origins or set(DEFAULT_ALLOWED_ORIGINS)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, session, thread


def build_import_url(site_url: str, port: int, token: str) -> str:
    base = site_url.rstrip("/")
    query = urlencode({"port": int(port), "token": token})
    return f"{base}/#/bridge-import?{query}"


def open_in_edge(url: str) -> bool:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            subprocess.Popen([str(candidate), url])
            return True
    return bool(webbrowser.open(url))
