import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("pause_report", Path(__file__).resolve().parents[1] / "tools/macos/dxmt-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class PauseAnalysis(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cpu = self.root / "game.csv"
        self.encoder = self.root / "game.encoder.csv"

    def tearDown(self):
        self.temp.cleanup()

    def write_cpu(self, extended=True, footer=True, pause=False):
        fields = report.FIELDS + (report.DIAGNOSTIC_FIELDS if extended else ())
        with self.cpu.open("w") as out:
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            for frame in range(200):
                row = dict.fromkeys(fields, 0)
                row.update(frame=frame, boundary_interval_ns=1000000)
                if pause and frame == 140:
                    row["boundary_interval_ns"] = 4419871000
                    row["frame_latency_wait_ns"] = 5000000
                    if extended:
                        row["present_pipeline_build_ns"] = 3000000000
                writer.writerow(row)
            if footer:
                out.write("# dropped_samples=0\n")

    def test_worst_interval_has_exact_frame_join_and_separate_timings(self):
        self.write_cpu(pause=True)
        self.encoder.write_text("frame,next_drawable_ns,present_encode_ns\n"
                                "139,9000000000,9100000000\n"
                                "140,800000,1200000\n140,1000,5000\n# dropped_samples=0\n")
        result = report.summarize(self.cpu, skip_seconds=0)
        worst = result["longest_intervals"][0]
        self.assertEqual(worst["frame"], 140)
        self.assertEqual(worst["boundary_interval_ms"], 4419.871)
        self.assertEqual(worst["cpu_interval_timings_ms"]["present_pipeline_build"], 3000)
        self.assertEqual(worst["encoder_same_frame"], [
            {"next_drawable": .8, "present_encode": 1.2},
            {"next_drawable": .001, "present_encode": .005},
        ])
        self.assertEqual(len(result["longest_intervals"]), 10)
        self.assertNotIn("cause", worst)

    def test_legacy_report_can_show_pauses_without_encoder_file(self):
        self.write_cpu(extended=False, pause=True)
        result = report.summarize(self.cpu, skip_seconds=0)
        self.assertEqual(result["longest_intervals"][0]["frame"], 140)
        self.assertEqual(result["longest_intervals"][0]["encoder_same_frame"], [])
        self.assertNotIn("cpu_diagnostics", result)

    def test_partial_cpu_and_encoder_tails_are_explicitly_omitted(self):
        self.write_cpu(footer=False)
        with self.cpu.open("a") as out:
            out.write("200,4419871000,0")
        self.encoder.write_text("frame,next_drawable_ns,present_encode_ns\n0,1000,2000\n1,999")
        with self.assertRaises(ValueError):
            report.summarize(self.cpu, skip_seconds=0)
        result = report.summarize(self.cpu, skip_seconds=0, allow_incomplete=True)
        self.assertEqual(result["samples"], 200)
        self.assertTrue(result["ignored_unterminated_tail"])
        self.assertTrue(result["encoder_diagnostics"]["ignored_unterminated_tail"])
        self.assertEqual(result["encoder_diagnostics"]["samples"], 1)
        self.assertFalse(result["clean_shutdown"])

    def test_complete_malformed_rows_are_not_hidden_by_partial_mode(self):
        self.write_cpu(footer=False)
        with self.cpu.open("a") as out:
            out.write("200,4419871000,0\n")
        with self.assertRaisesRegex(ValueError, "Malformed frame"):
            report.summarize(self.cpu, skip_seconds=0, allow_incomplete=True)
        self.encoder.write_text("frame,next_drawable_ns,present_encode_ns\n1,999\n")
        with self.assertRaisesRegex(ValueError, "Malformed encoder"):
            report.summarize_encoder(self.encoder, 0, 200, True)

    def test_unterminated_but_parseable_integer_is_not_trusted(self):
        self.write_cpu(footer=False)
        with self.cpu.open("a") as out:
            out.write("200,1000000," + ",".join("0" for _ in range(18)))
        result = report.summarize(self.cpu, skip_seconds=0, allow_incomplete=True)
        self.assertEqual(result["samples"], 200)
        self.assertTrue(result["ignored_unterminated_tail"])

    def test_scheduler_snapshots_and_pipeline_wait_schema(self):
        fields=report.FIELDS+report.DIAGNOSTIC_FIELDS+report.SCHEDULER_FIELDS
        with self.cpu.open("w") as out:
            writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
            for frame in range(200):
                row=dict.fromkeys(fields,0)
                row.update(frame=frame,boundary_interval_ns=1000000 if frame!=140 else 4000000000,
                           shader_workers=4,shader_workers_active=3,shader_jobs_queued=8,shader_worker_limit=4)
                writer.writerow(row)
            out.write("# dropped_samples=0\n")
        self.encoder.write_text("frame,next_drawable_ns,present_encode_ns,pipeline_wait_ns\n"
                                "140,1000,2000,3000000000\n# dropped_samples=0\n")
        result=report.summarize(self.cpu,skip_seconds=0)
        self.assertEqual(result["shader_scheduler_snapshots"]["max"]["shader_worker_limit"],4)
        self.assertNotIn("shader_worker_limit",result["cpu_diagnostics"]["counts"])
        worst=result["longest_intervals"][0]
        self.assertEqual(worst["shader_scheduler_snapshot"]["shader_jobs_queued"],8)
        self.assertEqual(worst["encoder_same_frame"][0]["pipeline_wait"],3000)

if __name__ == "__main__": unittest.main()
