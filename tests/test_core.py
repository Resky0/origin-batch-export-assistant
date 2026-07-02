import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from origin_bridge.core import (
    BridgeLimits,
    ExportedGraph,
    ORIGIN_READY_MARKER,
    build_origin_command_line,
    build_import_url,
    build_labtalk_script,
    collect_project_files,
    create_figgrid,
    discover_origin_executable,
    inspect_origin_installation,
    probe_origin_environment,
    validate_origin_projects,
    _matching_processes,
)


def make_png(path: Path, size: tuple[int, int], color: str) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


class CoreTests(unittest.TestCase):
    def test_collects_projects_recursively_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "nested"
            nested.mkdir()
            first = root / "一.opju"
            second = nested / "two.opj"
            ignored = nested / "readme.txt"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            ignored.write_text("ignore", encoding="utf-8")

            collected = collect_project_files([root, first])

            self.assertEqual([first.resolve(), second.resolve()], collected)

    def test_configured_origin_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Origin64.exe"
            executable.write_bytes(b"stub")
            self.assertEqual(executable.resolve(), discover_origin_executable(executable))

    def test_inspects_origin_version_and_opju_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Origin64.exe"
            executable.write_bytes(b"stub")
            with patch(
                "origin_bridge.core._file_version_strings",
                return_value={
                    "ProductName": "Origin",
                    "ProductVersion": "9.8",
                    "FileVersion": "9.8.0",
                },
            ):
                info = inspect_origin_installation(executable)

            self.assertEqual("Origin 2021", info.marketing_version)
            self.assertTrue(info.supports_opju)

    def test_rejects_opju_for_pre_2018_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Origin64.exe"
            project = root / "legacy.opju"
            executable.write_bytes(b"stub")
            project.write_bytes(b"project")
            with patch(
                "origin_bridge.core._file_version_strings",
                return_value={"ProductVersion": "9.4", "FileVersion": "9.4.0"},
            ):
                info = inspect_origin_installation(executable)

            with self.assertRaisesRegex(RuntimeError, "2017"):
                validate_origin_projects(info, [project])

    def test_probe_accepts_origin_specific_exit_code_when_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Origin64.exe"
            executable.write_bytes(b"stub")
            process = unittest.mock.Mock()
            process.returncode = 918618112
            process.wait.return_value = process.returncode
            with (
                patch("origin_bridge.core._file_version_strings", return_value={}),
                patch("origin_bridge.core.running_origin_processes", return_value=[]),
                patch("origin_bridge.core.subprocess.Popen", return_value=process),
                patch(
                    "origin_bridge.core._read_text_best_effort",
                    return_value=ORIGIN_READY_MARKER,
                ),
            ):
                _info, result = probe_origin_environment(executable)

            self.assertTrue(result.success)

    def test_probe_reports_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Origin64.exe"
            executable.write_bytes(b"stub")
            process = unittest.mock.Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired("origin", 1), 0]
            with (
                patch("origin_bridge.core._file_version_strings", return_value={}),
                patch("origin_bridge.core.running_origin_processes", return_value=[]),
                patch("origin_bridge.core.subprocess.Popen", return_value=process),
            ):
                with self.assertRaisesRegex(RuntimeError, "许可证"):
                    probe_origin_environment(executable, timeout_seconds=1)

            process.kill.assert_called_once()

    def test_origin_process_match_ignores_helper_with_origin_prefix(self) -> None:
        tasklist_output = (
            '"Origin批量排版助手.exe","38660","Console","1","149,948 K"\n'
            '"explorer.exe","12640","Console","1","100,000 K"'
        )

        self.assertEqual([], _matching_processes(tasklist_output, "Origin64.exe"))

    def test_origin_process_match_finds_selected_executable(self) -> None:
        tasklist_output = (
            '"Origin批量排版助手.exe","38660","Console","1","149,948 K"\n'
            '"Origin64.exe","41234","Console","1","200,000 K"'
        )

        self.assertEqual(
            [(41234, "Origin64.exe")],
            _matching_processes(tasklist_output, "Origin64.exe"),
        )

    def test_labtalk_uses_safe_copy_and_project_export(self) -> None:
        script = build_labtalk_script(
            Path(r"C:\Temp\source_0001.opju"),
            Path(r"C:\Temp\export_0001"),
            3000,
        )
        self.assertIn('doc -o "C:\\Temp\\source_0001.opju";', script)
        self.assertIn("export:=project", script)
        self.assertIn("tr1.width:=3000", script)
        self.assertIn("ORIGIN_BRIDGE_STAGE_OPENED", script)
        self.assertIn("ORIGIN_BRIDGE_STAGE_EXPORTED", script)
        self.assertTrue(script.endswith('ORIGIN_BRIDGE_STAGE_DONE";exit;'))
        command = build_origin_command_line(
            Path(r"D:\Origin\Origin64.exe"), script, Path(r"C:\Temp\origin.log")
        )
        self.assertTrue(
            command.startswith(
                '"D:\\Origin\\Origin64.exe" -HS -SLOG "C:\\Temp\\origin.log" -RS doc -o'
            )
        )

    def test_creates_valid_v2_figgrid_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.png"
            second = root / "second.png"
            make_png(first, (640, 480), "red")
            make_png(second, (800, 600), "blue")
            graphs = [
                ExportedGraph(
                    id="g1",
                    source_path="one.opju",
                    source_name="one.opju",
                    graph_index=1,
                    graph_name="Graph01",
                    image_path=str(first),
                    width=640,
                    height=480,
                    size=first.stat().st_size,
                ),
                ExportedGraph(
                    id="g2",
                    source_path="two.opju",
                    source_name="two.opju",
                    graph_index=1,
                    graph_name="Graph01",
                    image_path=str(second),
                    width=800,
                    height=600,
                    size=second.stat().st_size,
                ),
            ]
            output = create_figgrid(graphs, root / "selected.figgrid", "测试工程")

            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(2, manifest["schemaVersion"])
                self.assertEqual("测试工程", manifest["title"])
                self.assertEqual(2, len(manifest["assets"]))
                self.assertEqual(manifest["panelOrder"], list(manifest["panels"]))
                for asset in manifest["assets"]:
                    payload = archive.read(asset["path"])
                    self.assertEqual(asset["size"], len(payload))
                    self.assertEqual(
                        asset["sha256"], hashlib.sha256(payload).hexdigest()
                    )

    def test_figgrid_rejects_invalid_panel_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "2–12"):
                create_figgrid([], Path(directory) / "empty.figgrid", "empty")

    def test_figgrid_rejects_ineligible_image(self) -> None:
        graph = ExportedGraph(
            id="g1",
            source_path="one.opju",
            source_name="one.opju",
            graph_index=1,
            graph_name="Graph01",
            image_path="missing.png",
            width=1,
            height=1,
            size=1,
            eligible=False,
            error="too large",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "不符合"):
                create_figgrid(
                    [graph, graph],
                    Path(directory) / "invalid.figgrid",
                    "invalid",
                    limits=BridgeLimits(),
                )

    def test_build_import_url_uses_hash_route(self) -> None:
        url = build_import_url("https://layout-assistant.xyz/", 54321, "a b")
        self.assertEqual(
            "https://layout-assistant.xyz/#/bridge-import?port=54321&token=a+b",
            url,
        )


if __name__ == "__main__":
    unittest.main()
