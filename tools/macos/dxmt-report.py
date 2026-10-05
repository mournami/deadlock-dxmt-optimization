#!/usr/bin/env python3
"""Summarize CPU frame-boundary cadence; these numbers are not display/GPU FPS."""
from __future__ import annotations

import argparse
import csv
import heapq
import io
import json
import math
from pathlib import Path
import statistics

FIELDS = ("frame", "boundary_interval_ns", "command_queue_wait_ns", "resource_sync_wait_ns",
          "frame_latency_wait_ns", "command_buffers", "resource_syncs", "event_stalls")
DIAGNOSTIC_FIELDS = ("om_blend_calls", "om_blend_redundant", "om_depth_calls", "om_depth_redundant",
    "om_blend_commands", "om_depth_commands", "layer_query_ns", "layer_wait_ns",
    "present_pipeline_build_ns", "layer_update_ns", "display_changes", "present_pipeline_builds")


def report_lines(path: Path, allow_incomplete: bool) -> tuple[list[str], bool]:
    raw = path.read_text()
    lines = raw.splitlines()
    # A killed writer can leave half a row (or a partially written integer).
    # Discard only the unterminated tail, only when partial analysis was chosen.
    ignored_tail = bool(raw and not raw.endswith(("\n", "\r")) and allow_incomplete)
    if ignored_tail:
        lines = lines[:-1]
    return lines, ignored_tail


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = fraction * (len(ordered) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summarize(path: Path, skip_seconds: float = 5, allow_incomplete: bool = False,
              duration_seconds: float | None = None) -> dict:
    lines, ignored_tail = report_lines(path, allow_incomplete)
    dropped = next((int(line.split("=", 1)[1]) for line in lines if line.startswith("# dropped_samples=")), None)
    if dropped is not None and dropped < 0:
        raise ValueError("Negative dropped sample count")
    reader = csv.DictReader(io.StringIO("\n".join(line for line in lines if not line.startswith("#"))))
    columns = tuple(reader.fieldnames or ())
    if columns not in (FIELDS, FIELDS + DIAGNOSTIC_FIELDS):
        raise ValueError("Unsupported frame report header")
    rows = []
    previous = None
    elapsed_ns = 0
    gaps = 0
    for row in reader:
        if None in row or any(row.get(key) is None for key in columns):
            raise ValueError("Malformed frame report row")
        sample = {key: int(row[key]) for key in columns}
        if any(value < 0 for value in sample.values()):
            raise ValueError("Negative report value")
        frame = sample["frame"]
        if previous is not None:
            if frame <= previous:
                raise ValueError("Frame numbers are not increasing")
            gaps += frame - previous - 1
        else:
            gaps += frame
        previous = frame
        interval = sample["boundary_interval_ns"]
        elapsed_ns += interval
        sample["recorded_elapsed_ns"] = elapsed_ns
        in_window = duration_seconds is None or elapsed_ns <= (skip_seconds + duration_seconds) * 1e9
        if interval > 0 and elapsed_ns > skip_seconds * 1e9 and in_window:
            rows.append(sample)
    if (dropped is None or dropped > 0 or gaps > 0) and not allow_incomplete:
        raise ValueError("Report was not closed cleanly or lost samples; use --allow-incomplete for partial observations")
    if len(rows) < 100:
        raise ValueError("Need at least 100 frame samples after warmup")
    intervals = [row["boundary_interval_ns"] / 1e6 for row in rows]
    slowest = sorted(intervals, reverse=True)[:max(1, math.ceil(len(intervals) * .01))]
    waits = {key.removesuffix("_ns"): round(statistics.mean(row[key] for row in rows) / 1e6, 4)
             for key in FIELDS[2:5]}
    result = {
        "file": str(path.resolve()), "samples": len(rows),
        "metric": "CPU PresentBoundary cadence; not measured display FPS or GPU time",
        "clean_shutdown": dropped is not None, "dropped_samples": dropped, "missing_frames": gaps,
        "ignored_unterminated_tail": ignored_tail,
        "warmup_seconds": skip_seconds, "observed_seconds": round(sum(intervals) / 1000, 3),
        "requested_duration_seconds": duration_seconds,
        "mean_interval_ms": round(statistics.mean(intervals), 4),
        "p50_interval_ms": round(percentile(intervals, .5), 4),
        "p95_interval_ms": round(percentile(intervals, .95), 4),
        "p99_interval_ms": round(percentile(intervals, .99), 4),
        "mean_boundary_rate_hz": round(1000 / statistics.mean(intervals), 2),
        "slowest_1pct_boundary_rate_hz": round(1000 / statistics.mean(slowest), 2),
        "mean_wait_ms": waits,
        "mean_command_buffers": round(statistics.mean(row["command_buffers"] for row in rows), 2),
        "resource_sync_events": sum(row["resource_syncs"] for row in rows),
        "event_stalls": sum(row["event_stalls"] for row in rows),
        "frames_over_33ms": sum(value > 33.333 for value in intervals),
        "frames_over_100ms": sum(value > 100 for value in intervals),
    }
    worst = heapq.nlargest(10, rows, key=lambda row: row["boundary_interval_ns"])
    timing_fields = FIELDS[2:5] + tuple(key for key in DIAGNOSTIC_FIELDS if key.endswith("_ns") and key in columns)
    result["longest_intervals"] = [{
        "frame": row["frame"],
        "recorded_elapsed_seconds": round(row["recorded_elapsed_ns"] / 1e9, 6),
        "boundary_interval_ms": round(row["boundary_interval_ns"] / 1e6, 4),
        "cpu_interval_timings_ms": {key.removesuffix("_ns"): round(row[key] / 1e6, 4) for key in timing_fields},
        "encoder_same_frame": [],
    } for row in worst]
    result["longest_intervals_note"] = (
        "CPU interval timings and encoder samples are shown separately, not added as a causal breakdown. "
        "Recorded elapsed time omits lost intervals; scene/focus events are not inferred."
    )
    if columns != FIELDS:
        result["cpu_diagnostics"] = {
            "scope": "Immediate/deferred recording calls between CPU boundaries; not GPU execution counts",
            "counts": {key: sum(row[key] for row in rows) for key in DIAGNOSTIC_FIELDS if not key.endswith("_ns")},
            "mean_ms": {key.removesuffix("_ns"): round(statistics.mean(row[key] for row in rows) / 1e6, 4)
                        for key in DIAGNOSTIC_FIELDS if key.endswith("_ns")},
            "max_ms": {key.removesuffix("_ns"): round(max(row[key] for row in rows) / 1e6, 4)
                       for key in DIAGNOSTIC_FIELDS if key.endswith("_ns")},
        }
    encoder = path.with_name(path.stem + ".encoder.csv")
    if encoder.is_file():
        result["encoder_diagnostics"] = summarize_encoder(
            encoder, rows[0]["frame"], rows[-1]["frame"], allow_incomplete,
            selected_frames={row["frame"] for row in worst})
        selected = result["encoder_diagnostics"].pop("selected_frame_samples")
        for interval in result["longest_intervals"]:
            interval["encoder_same_frame"] = selected.get(str(interval["frame"]), [])
    return result


def summarize_encoder(path: Path, first_frame: int, last_frame: int, allow_incomplete: bool,
                      selected_frames: set[int] | None = None) -> dict:
    lines, ignored_tail = report_lines(path, allow_incomplete)
    dropped = next((int(line.split("=", 1)[1]) for line in lines if line.startswith("# dropped_samples=")), None)
    if dropped is not None and dropped < 0:
        raise ValueError("Negative encoder dropped sample count")
    reader = csv.DictReader(io.StringIO("\n".join(line for line in lines if not line.startswith("#"))))
    columns = ("frame", "next_drawable_ns", "present_encode_ns")
    if tuple(reader.fieldnames or ()) != columns:
        raise ValueError("Unsupported encoder report header")
    samples = []
    selected = {}
    for row in reader:
        if None in row or any(row.get(key) is None for key in columns):
            raise ValueError("Malformed encoder report row")
        sample = {key: int(row[key]) for key in columns}
        if any(value < 0 for value in sample.values()):
            raise ValueError("Negative encoder report value")
        if first_frame <= sample["frame"] <= last_frame:
            samples.append(sample)
            if selected_frames and sample["frame"] in selected_frames:
                selected.setdefault(str(sample["frame"]), []).append({
                    key.removesuffix("_ns"): round(sample[key] / 1e6, 4) for key in columns[1:]})
    if (dropped is None or dropped > 0) and not allow_incomplete:
        raise ValueError("Encoder report is incomplete or lost samples")
    return {
        "scope": "Encoding-thread wall time, matched by frame ID; not GPU time or display FPS",
        "file": str(path), "samples": len(samples), "dropped_samples": dropped,
        "ignored_unterminated_tail": ignored_tail, "selected_frame_samples": selected,
        "mean_ms": {key.removesuffix("_ns"): round(statistics.mean(s[key] for s in samples) / 1e6, 4)
                    for key in columns[1:]} if samples else {},
        "p99_ms": {key.removesuffix("_ns"): round(percentile([s[key] / 1e6 for s in samples], .99), 4)
                   for key in columns[1:]} if samples else {},
        "max_ms": {key.removesuffix("_ns"): round(max(s[key] for s in samples) / 1e6, 4)
                   for key in columns[1:]} if samples else {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--compare", type=Path, help="Baseline report from the same scene and settings")
    parser.add_argument("--skip-seconds", type=float, default=5)
    parser.add_argument("--duration-seconds", type=float, help="Analyze a fixed window after warmup")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    if args.skip_seconds < 0:
        parser.error("Warmup cannot be negative")
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        parser.error("Duration must be positive")
    try:
        result = summarize(args.report, args.skip_seconds, args.allow_incomplete, args.duration_seconds)
        if args.compare:
            baseline = summarize(args.compare, args.skip_seconds, args.allow_incomplete, args.duration_seconds)
            result = {"experiment": result, "baseline": baseline,
                      "mean_interval_change_pct": round((result["mean_interval_ms"] / baseline["mean_interval_ms"] - 1) * 100, 2),
                      "p99_interval_change_pct": round((result["p99_interval_ms"] / baseline["p99_interval_ms"] - 1) * 100, 2),
                      "note": "Negative interval changes mean faster cadence; scene equivalence must be checked manually"}
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
