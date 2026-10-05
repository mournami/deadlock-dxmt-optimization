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
        self.events = self.root / "game.events.csv"

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

    def write_events(self, rows, footer=True):
        with self.events.open("w") as out:
            writer = csv.writer(out)
            writer.writerow(report.EVENT_FIELDS)
            writer.writerows(rows)
            if footer:
                out.write("# dropped_samples=0\n")

    def test_event_timeline_separates_nested_calls_from_inter_call_gaps(self):
        self.write_cpu(pause=True)
        # Return order differs from entry order. The 2s mutex is nested inside
        # Present and must not be added again to Present's duration.
        self.write_events([
            ("present_mutex", 139, 1100000000, 2000000000, 1, 10, 0),
            ("window_state", 139, 1000000000, 10, 1, 10, 5),
            ("present", 139, 1000000000, 2100000000, 1, 10, 0),
            ("present", 139, 4000000000, 1, 2, 20, 0),
            ("present", 140, 9000000000, 1000000, 1, 10, 0),
            ("window_state", 140, 9000000001, 10, 1, 10, 4),
        ])
        result = report.summarize(self.cpu, skip_seconds=0)
        dxgi = result["dxgi_events"]
        self.assertEqual(dxgi["calls"]["present_mutex"]["max_ms"], 2000)
        self.assertEqual(dxgi["longest_calls"][0]["duration_ms"], 2100)
        gap = dxgi["longest_present_gaps"][0]
        self.assertEqual(gap["gap_ms"], 5900)
        self.assertEqual((gap["thread_id"], gap["object_id"]), (1, 10))
        self.assertEqual(len(dxgi["longest_present_gaps"]), 1)
        transitions = dxgi["sampled_window_transitions"]
        self.assertTrue(transitions[0]["initial_sample"])
        self.assertTrue(transitions[0]["foreground"])
        self.assertFalse(transitions[1]["foreground"])
        self.assertFalse(transitions[1]["initial_sample"])
        near = result["longest_intervals"][0]["events_near_cpu_frame"]
        self.assertEqual(len(near), 6)
        self.assertEqual(near[0]["start_seconds"], 0)
        self.assertNotIn("cause", dxgi)

    def test_event_partial_tail_losses_and_bad_rows(self):
        self.write_events([("present", 1, 100, 200, 3, 4, 0)], footer=False)
        with self.events.open("a") as out:
            out.write("present,2,10")
        with self.assertRaises(ValueError):
            report.summarize_events(self.events, 0, 10, False)
        result = report.summarize_events(self.events, 0, 10, True)
        self.assertEqual(result["samples"], 1)
        self.assertTrue(result["ignored_unterminated_tail"])
        self.assertFalse(result["clean_shutdown"])
        self.write_events([("present", 1, 100, -2, 3, 4, 0)])
        with self.assertRaisesRegex(ValueError, "Negative event"):
            report.summarize_events(self.events, 0, 10, True)
        self.write_events([("unknown_event", 1, 100, 2, 3, 4, 0)])
        with self.assertRaisesRegex(ValueError, "Unknown event"):
            report.summarize_events(self.events, 0, 10, True)
        self.write_events([("present", 1, 100)], footer=False)
        with self.assertRaisesRegex(ValueError, "Malformed event"):
            report.summarize_events(self.events, 0, 10, True)

    def test_test_presents_and_unrelated_swapchains_do_not_create_gaps(self):
        self.write_events([
            ("present", 1, 100, 10, 1, 10, 0),
            ("present", 1, 200, 10, 1, 10, 1),
            ("present", 2, 300, 10, 1, 20, 0),
            ("present", 3, 400, 10, 1, 10, 0),
        ])
        result = report.summarize_events(self.events, 0, 10, False)
        self.assertEqual(len(result["longest_present_gaps"]), 1)
        self.assertEqual(result["longest_present_gaps"][0]["previous_frame"], 1)
        self.assertEqual(result["longest_present_gaps"][0]["frame"], 3)
        self.assertEqual(result["longest_present_gaps"][0]["gap_ms"], .0003)

if __name__ == "__main__": unittest.main()
