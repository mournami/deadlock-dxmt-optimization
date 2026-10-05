import importlib.util
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools/macos"
spec = importlib.util.spec_from_file_location("menu", TOOLS / "dxmt-crossover.py")
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)
lab, runner = menu.lab, menu.runner
report_spec = importlib.util.spec_from_file_location("report", TOOLS / "dxmt-report.py")
report = importlib.util.module_from_spec(report_spec)
report_spec.loader.exec_module(report)

class ReadyBuilds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(lab, "LAB", self.root)
        self.patch.start()
        self.install = self.root / "install-experiment"
        for rel in lab.FILES:
            target = self.install / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rel.encode())
        self.hashes = {rel: lab.digest(self.install / rel) for rel in lab.FILES}
        self.manifest = {"variant": "experiment", "install": str(self.install), "binary_sha256": self.hashes,
                         "source_commit": "example", "source_files_sha256": {}}
        (self.root / "build-experiment-manifest.json").write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def record(self):
        return {"variant": "experiment", "install": str(self.install), "sha256": self.hashes,
                "validation": "dll-and-gpu-readback"}

    def test_ready_hashes_must_still_match(self):
        menu.atomic_json(self.root / "ready-experiment.json", self.record())
        self.assertEqual(runner.selected_build("experiment", True), self.install)
        (self.install / lab.FILES[0]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "changed after validation"):
            runner.selected_build("experiment", True)

    def test_missing_and_outside_build_refused(self):
        with self.assertRaisesRegex(RuntimeError, "No validated"):
            runner.selected_build("experiment", True)
        record = self.record()
        record["install"] = str(self.root.parent)
        menu.atomic_json(self.root / "ready-experiment.json", record)
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            runner.selected_build("experiment", True)

    def test_failed_gpu_check_keeps_previous_ready_pointer(self):
        pointer = self.root / "ready-experiment.json"
        pointer.write_text('{"previous":"keep"}')
        with patch.object(lab, "stage"), patch.object(lab, "probe"), \
             patch.object(lab, "device_probe", side_effect=RuntimeError("GPU failed")):
            with self.assertRaisesRegex(RuntimeError, "GPU failed"):
                menu.promote("experiment")
        self.assertEqual(pointer.read_text(), '{"previous":"keep"}')

    def test_success_publishes_immutable_snapshot(self):
        with patch.object(lab, "stage"), patch.object(lab, "probe"), patch.object(lab, "device_probe"):
            menu.promote("experiment")
        selected = runner.selected_build("experiment", True)
        self.assertTrue(selected.is_relative_to(self.root / "ready-builds"))
        (self.install / lab.FILES[0]).write_bytes(b"next-build")
        self.assertEqual(runner.selected_build("experiment", True), selected)

    def test_presentation_failure_keeps_previous_ready_pointer(self):
        self.manifest["dxgi_events"] = True
        (self.root / "build-experiment-manifest.json").write_text(json.dumps(self.manifest))
        pointer = self.root / "ready-experiment.json"
        pointer.write_text('{"previous":"keep"}')
        with patch.object(lab, "stage"), patch.object(lab, "probe"), patch.object(lab, "device_probe"), \
             patch.object(lab, "presentation_probe", side_effect=RuntimeError("Present failed")) as present:
            with self.assertRaisesRegex(RuntimeError, "Present failed"):
                menu.promote("experiment")
            present.assert_called_once()
        self.assertEqual(pointer.read_text(), '{"previous":"keep"}')

    def test_check_entry_cannot_launch_wine_or_game(self):
        with patch.object(lab, "private_runtime"), patch.object(runner, "check_prefix"), \
             patch.object(runner, "selected_build"), patch.object(menu.subprocess, "run") as process, \
             patch.object(runner, "run") as launch:
            menu.entry("check")
            process.assert_not_called()
            launch.assert_not_called()

    def test_extended_cpu_and_encoder_reports_join_by_frame(self):
        cpu = self.root / "game.csv"
        with cpu.open("w") as out:
            writer = csv.DictWriter(out, fieldnames=report.FIELDS + report.DIAGNOSTIC_FIELDS)
            writer.writeheader()
            for i in range(120):
                row = dict.fromkeys(report.FIELDS + report.DIAGNOSTIC_FIELDS, 0)
                row.update(frame=i, boundary_interval_ns=1000000, om_blend_calls=8, om_blend_redundant=7)
                writer.writerow(row)
            out.write("# dropped_samples=0\n")
        encoder = self.root / "game.encoder.csv"
        encoder.write_text("frame,next_drawable_ns,present_encode_ns\n105,4000000000,4100000000\n119,9,12\n# dropped_samples=0\n")
        result = report.summarize(cpu, skip_seconds=0, duration_seconds=.11)
        self.assertEqual(result["cpu_diagnostics"]["counts"]["om_blend_calls"],880)
        self.assertEqual(result["encoder_diagnostics"]["samples"],1)
        self.assertEqual(result["encoder_diagnostics"]["max_ms"]["next_drawable"],4000)

    def test_incomplete_and_negative_encoder_diagnostics_refused(self):
        encoder = self.root / "game.encoder.csv"
        encoder.write_text("frame,next_drawable_ns,present_encode_ns\n1,2,3\n")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            report.summarize_encoder(encoder,0,10,False)
        self.assertEqual(report.summarize_encoder(encoder,0,10,True)["samples"],1)
        encoder.write_text("frame,next_drawable_ns,present_encode_ns\n1,-2,3\n# dropped_samples=0\n")
        with self.assertRaisesRegex(ValueError, "Negative encoder"):
            report.summarize_encoder(encoder,0,10,True)

if __name__ == "__main__": unittest.main()
