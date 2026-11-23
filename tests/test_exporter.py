import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from origin_bridge.core import (
    ExportedGraph,
    OriginBatchExporter,
    ProjectExportResult,
)


class FakeExporter(OriginBatchExporter):
    def __init__(self, origin_executable: Path, *, cancel_first: bool = False) -> None:
        super().__init__(origin_executable)
        self.cancel_first = cancel_first

    def _export_one(
        self,
        source: Path,
        project_index: int,
        attempt: int,
        temp_root: Path,
        job,
        progress,
    ) -> ProjectExportResult:
        if self.cancel_first and project_index == 1 and attempt == 1:
            self.cancel_event.set()
            return ProjectExportResult(
                project_index=project_index,
                source_path=str(source),
                status="cancelled",
                attempt=attempt,
                error="cancelled",
                error_code="CANCELLED",
            )
        if not self.cancel_first and project_index == 1 and attempt == 1:
            partial = job.images_directory / "0001_partial.png"
            Image.new("RGB", (10, 10), "black").save(partial)
            return ProjectExportResult(
                project_index=project_index,
                source_path=str(source),
                status="timeout",
                attempt=attempt,
                error="timeout",
                error_code="ORIGIN_TIMEOUT",
            )

        image_path = job.images_directory / f"{project_index:04d}_{source.stem}_Graph01.png"
        Image.new("RGB", (100, 80), "blue").save(image_path)
        graph = ExportedGraph(
            id=f"{project_index}-{attempt}",
            source_path=str(source),
            source_name=source.name,
            graph_index=1,
            graph_name="Graph01",
            image_path=str(image_path),
            width=100,
            height=80,
            size=image_path.stat().st_size,
        )
        return ProjectExportResult(
            project_index=project_index,
            source_path=str(source),
            status="success",
            attempt=attempt,
            graphs=[graph],
            last_stage="complete",
        )


class ExporterTests(unittest.TestCase):
    def _files(self, root: Path) -> tuple[Path, list[Path]]:
        origin = root / "Origin64.exe"
        origin.write_bytes(b"stub")
        projects = [root / "one.opj", root / "two.opj"]
        for project in projects:
            project.write_bytes(project.name.encode())
        return origin, projects

    def test_retry_replaces_only_failed_project_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, projects = self._files(root)
            exporter = FakeExporter(origin)
            with patch("origin_bridge.core.running_origin_processes", return_value=[]):
                job = exporter.export(projects, root / "out")
                second_image = Path(job.results[1].graphs[0].image_path)
                second_payload = second_image.read_bytes()
                self.assertEqual("timeout", job.results[0].status)
                self.assertEqual("success", job.results[1].status)
                self.assertTrue((job.images_directory / "0001_partial.png").exists())

                exporter.retry_failed(job)

            self.assertEqual("success", job.status)
            self.assertEqual(2, job.results[0].attempt)
            self.assertEqual(1, job.results[1].attempt)
            self.assertTrue((job.images_directory / "0001_partial.png").exists())
            self.assertEqual(second_payload, second_image.read_bytes())
            self.assertEqual(root / "out", job.images_directory)
            self.assertEqual(
                {"0001_partial.png", "0001_one_Graph01.png", "0002_two_Graph01.png"},
                {entry.name for entry in job.output_directory.iterdir()},
            )

    def test_cancelled_job_can_continue_in_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, projects = self._files(root)
            exporter = FakeExporter(origin, cancel_first=True)
            with patch("origin_bridge.core.running_origin_processes", return_value=[]):
                job = exporter.export(projects, root / "out")
                self.assertEqual("cancelled", job.status)
                self.assertEqual("cancelled", job.results[0].status)
                self.assertEqual("pending", job.results[1].status)

                exporter.resume(job)

            self.assertEqual("success", job.status)
            self.assertEqual(["success", "success"], [r.status for r in job.results])
            self.assertEqual([2, 1], [r.attempt for r in job.results])


if __name__ == "__main__":
    unittest.main()
