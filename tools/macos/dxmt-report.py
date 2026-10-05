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
SCHEDULER_FIELDS = ("shader_workers", "shader_workers_active", "shader_jobs_queued", "shader_worker_limit")
SHADER_CREATE_FIELDS = ("shader_create_calls", "shader_create_cache_hits", "shader_create_misses",
                        "shader_create_ns", "shader_bytecode_bytes")
GPU_FIELDS = ("frame", "chunk", "gpu_start_ns", "gpu_end_ns", "kernel_start_ns", "kernel_end_ns",
              "status", "allocated_bytes", "memory_sampled")
EVENT_FIELDS = ("event", "frame", "start_ns", "duration_ns", "thread_id", "object_id", "detail")
EVENT_KINDS = {"present", "present_mutex", "prepare_flush", "commit", "present_boundary", "sync_frame",
               "window_state", "resize_buffers", "resize_target", "fullscreen", "apply_layer",
               "wait_gpu_idle", "wait_cpu_fence"}


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
    if columns not in (FIELDS, FIELDS + DIAGNOSTIC_FIELDS, FIELDS + DIAGNOSTIC_FIELDS + SCHEDULER_FIELDS,
                       FIELDS + DIAGNOSTIC_FIELDS + SCHEDULER_FIELDS + SHADER_CREATE_FIELDS):
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
    mode = next((line.split("=", 1)[1] for line in lines if line.startswith("# report_mode=")), "legacy")
    if mode not in ("legacy", "full", "light"):
        raise ValueError("Unknown report mode")
    result["report_mode"] = mode
    worst = heapq.nlargest(10, rows, key=lambda row: row["boundary_interval_ns"])
    timing_fields = FIELDS[2:5] + tuple(key for key in DIAGNOSTIC_FIELDS + SHADER_CREATE_FIELDS
                                     if key.endswith("_ns") and key in columns and
                                     (key in SHADER_CREATE_FIELDS or mode != "light"))
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
    if columns != FIELDS and mode != "light":
        result["cpu_diagnostics"] = {
            "scope": "Immediate/deferred recording calls between CPU boundaries; not GPU execution counts",
            "counts": {key: sum(row[key] for row in rows) for key in DIAGNOSTIC_FIELDS if not key.endswith("_ns")},
            "mean_ms": {key.removesuffix("_ns"): round(statistics.mean(row[key] for row in rows) / 1e6, 4)
                        for key in DIAGNOSTIC_FIELDS if key.endswith("_ns")},
            "max_ms": {key.removesuffix("_ns"): round(max(row[key] for row in rows) / 1e6, 4)
                       for key in DIAGNOSTIC_FIELDS if key.endswith("_ns")},
        }
    if all(key in columns for key in SCHEDULER_FIELDS):
        result["shader_scheduler_snapshots"] = {
            "scope": "Atomic snapshots at CPU Present; not active worker CPU time or completed jobs",
            "mean": {key: round(statistics.mean(row[key] for row in rows), 3) for key in SCHEDULER_FIELDS},
            "max": {key: max(row[key] for row in rows) for key in SCHEDULER_FIELDS},
        }
        for interval in result["longest_intervals"]:
            row = next(row for row in worst if row["frame"] == interval["frame"])
            interval["shader_scheduler_snapshot"] = {key: row[key] for key in SCHEDULER_FIELDS}
    if all(key in columns for key in SHADER_CREATE_FIELDS):
        result["cpu_shader_creation"] = {
            "scope": "CPU shader bytecode hashing/parsing and in-process lookup; NOT persistent IR cache hits",
            "counts": {key: sum(row[key] for row in rows) for key in SHADER_CREATE_FIELDS if not key.endswith("_ns")},
            "total_ms": round(sum(row["shader_create_ns"] for row in rows) / 1e6, 4),
            "max_interval_ms": round(max(row["shader_create_ns"] for row in rows) / 1e6, 4)}
    encoder = path.with_name(path.stem + ".encoder.csv")
    if encoder.is_file():
        result["encoder_diagnostics"] = summarize_encoder(
            encoder, rows[0]["frame"], rows[-1]["frame"], allow_incomplete,
            selected_frames={row["frame"] for row in worst})
        selected = result["encoder_diagnostics"].pop("selected_frame_samples")
        for interval in result["longest_intervals"]:
            interval["encoder_same_frame"] = selected.get(str(interval["frame"]), [])
    events = path.with_name(path.stem + ".events.csv")
    if events.is_file():
        result["dxgi_events"] = summarize_events(
            events, rows[0]["frame"], rows[-1]["frame"], allow_incomplete,
            selected_frames={row["frame"] for row in worst})
        selected = result["dxgi_events"].pop("selected_frame_events")
        for interval in result["longest_intervals"]:
            interval["events_near_cpu_frame"] = selected.get(str(interval["frame"]), [])
    gpu = path.with_name(path.stem + ".gpu.csv")
    if gpu.is_file():
        result["gpu_diagnostics"] = summarize_gpu(gpu, rows[0]["frame"], rows[-1]["frame"], allow_incomplete)
    return result


def summarize_encoder(path: Path, first_frame: int, last_frame: int, allow_incomplete: bool,
                      selected_frames: set[int] | None = None) -> dict:
    lines, ignored_tail = report_lines(path, allow_incomplete)
    dropped = next((int(line.split("=", 1)[1]) for line in lines if line.startswith("# dropped_samples=")), None)
    if dropped is not None and dropped < 0:
        raise ValueError("Negative encoder dropped sample count")
    reader = csv.DictReader(io.StringIO("\n".join(line for line in lines if not line.startswith("#"))))
    columns = tuple(reader.fieldnames or ())
    if columns not in (("frame", "next_drawable_ns", "present_encode_ns"),
                       ("frame", "next_drawable_ns", "present_encode_ns", "pipeline_wait_ns")):
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


def summarize_events(path: Path, first_frame: int, last_frame: int, allow_incomplete: bool,
                     selected_frames: set[int] | None = None) -> dict:
    lines, ignored_tail = report_lines(path, allow_incomplete)
    dropped = next((int(line.split("=", 1)[1]) for line in lines if line.startswith("# dropped_samples=")), None)
    if dropped is not None and dropped < 0:
        raise ValueError("Negative event dropped sample count")
    reader = csv.DictReader(io.StringIO("\n".join(line for line in lines if not line.startswith("#"))))
    if tuple(reader.fieldnames or ()) != EVENT_FIELDS:
        raise ValueError("Unsupported event report header")
    samples = []
    for row in reader:
        if None in row or any(row.get(key) is None for key in EVENT_FIELDS):
            raise ValueError("Malformed event report row")
        if row["event"] not in EVENT_KINDS:
            raise ValueError("Unknown event kind")
        sample = {"event": row["event"], **{key: int(row[key]) for key in EVENT_FIELDS[1:]}}
        if any(sample[key] < 0 for key in EVENT_FIELDS[1:]):
            raise ValueError("Negative event report value")
        if first_frame <= sample["frame"] <= last_frame:
            samples.append(sample)
    if (dropped is None or dropped > 0) and not allow_incomplete:
        raise ValueError("Event report is incomplete or lost samples")
    # Nested events are submitted on return; the file is not start-time ordered.
    samples.sort(key=lambda sample: sample["start_ns"])
    origin = samples[0]["start_ns"] if samples else 0

    def compact(sample: dict) -> dict:
        return {"event": sample["event"], "frame": sample["frame"],
                "start_seconds": round((sample["start_ns"] - origin) / 1e9, 6),
                "duration_ms": round(sample["duration_ns"] / 1e6, 4),
                "thread_id": sample["thread_id"], "object_id": sample["object_id"],
                "detail": sample["detail"]}

    selected = {}
    for frame in selected_frames or ():
        nearby = [sample for sample in samples if abs(sample["frame"] - frame) <= 1]
        selected[str(frame)] = [compact(sample) for sample in nearby[:80]]
    durations = {kind: [sample["duration_ns"] / 1e6 for sample in samples if sample["event"] == kind]
                 for kind in sorted(EVENT_KINDS)}
    # Gaps are per caller thread AND swapchain, never between unrelated objects.
    # They can include other DXMT calls/engine work/intentional pacing, and a
    # missing event can inflate a gap. Do not label them "time outside DXMT".
    previous = {}
    gaps = []
    for sample in samples:
        if sample["event"] != "present" or sample["detail"] & 1:  # DXGI_PRESENT_TEST
            continue
        key = (sample["thread_id"], sample["object_id"])
        prior = previous.get(key)
        if prior is not None:
            gap = sample["start_ns"] - (prior["start_ns"] + prior["duration_ns"])
            gaps.append({"previous_frame": prior["frame"], "frame": sample["frame"],
                         "thread_id": key[0], "object_id": key[1],
                         "gap_ms": round(max(0, gap) / 1e6, 4),
                         "overlap": gap < 0})
        previous[key] = sample

    window_states = {}
    transitions = []
    for sample in samples:
        if sample["event"] != "window_state":
            continue
        key = sample["object_id"]
        state = sample["detail"]
        if window_states.get(key) != state:
            transitions.append({**compact(sample), "initial_sample": key not in window_states,
                                "foreground": bool(state & 1), "minimized": bool(state & 2),
                                "visible": bool(state & 4)})
            window_states[key] = state
    return {
        "file": str(path), "samples": len(samples), "dropped_samples": dropped,
        "clean_shutdown": dropped is not None, "ignored_unterminated_tail": ignored_tail,
        "scope": "CPU call wall times; nested/overlapping durations must not be added. Frame IDs are nearby CPU interval labels.",
        "timeline_origin_ns": origin,
        "calls": {kind: {"count": len(values), "mean_ms": round(statistics.mean(values), 4),
                         "max_ms": round(max(values), 4)} for kind, values in durations.items() if values},
        "longest_calls": [compact(sample) for sample in heapq.nlargest(
            10, samples, key=lambda sample: sample["duration_ns"])],
        "longest_present_gaps": heapq.nlargest(10, gaps, key=lambda gap: gap["gap_ms"]),
        "present_gaps_note": "Between returned Present and next entry on the same thread/swapchain; may include other DXMT work or intentional pacing. Missing events inflate gaps.",
        "sampled_window_transitions": transitions[:100], "omitted_window_transitions": max(0, len(transitions) - 100),
        "window_note": "Foreground/minimized/visible sampled at Present only; a transition may be detected late or missed between calls. No exact Alt+Tab timestamp.",
        "selected_frame_events": selected,
    }


def summarize_gpu(path: Path, first_frame: int, last_frame: int, allow_incomplete: bool) -> dict:
    lines, ignored_tail = report_lines(path, allow_incomplete)
    dropped = next((int(line.split("=", 1)[1]) for line in lines if line.startswith("# dropped_samples=")), None)
    if dropped is not None and dropped < 0:
        raise ValueError("Negative GPU dropped sample count")
    reader = csv.DictReader(io.StringIO("\n".join(line for line in lines if not line.startswith("#"))))
    if tuple(reader.fieldnames or ()) != GPU_FIELDS:
        raise ValueError("Unsupported GPU report header")
    samples = []
    previous = None
    for row in reader:
        if None in row or any(row.get(key) is None for key in GPU_FIELDS):
            raise ValueError("Malformed GPU report row")
        sample = {key: int(row[key]) for key in GPU_FIELDS}
        if any(value < 0 for value in sample.values()):
            raise ValueError("Negative GPU report value")
        if previous is not None and sample["chunk"] <= previous:
            raise ValueError("GPU chunk IDs are not increasing")
        previous = sample["chunk"]
        for prefix in ("gpu", "kernel"):
            start, end = sample[prefix + "_start_ns"], sample[prefix + "_end_ns"]
            if (start and end and end < start):
                raise ValueError("Reversed GPU timing")
        if first_frame <= sample["frame"] <= last_frame:
            samples.append(sample)
    if (dropped is None or dropped > 0) and not allow_incomplete:
        raise ValueError("GPU report is incomplete or lost samples")
    valid = [s for s in samples if s["status"] == 4 and 0 < s["gpu_start_ns"] <= s["gpu_end_ns"]]
    intervals = [(s["gpu_end_ns"] - s["gpu_start_ns"]) / 1e6 for s in valid]
    memory = [s for s in samples if s["memory_sampled"]]
    return {
        "file": str(path), "samples": len(samples), "valid_timing_samples": len(valid),
        "unavailable_timing_samples": len(samples) - len(valid), "dropped_samples": dropped,
        "ignored_unterminated_tail": ignored_tail,
        "scope": "Completed command-buffer GPU execution span, not display FPS/GPU utilization. Overlapping spans must not be added.",
        "mean_gpu_ms": round(statistics.mean(intervals), 4) if intervals else None,
        "max_gpu_ms": round(max(intervals), 4) if intervals else None,
        "longest_buffers": [{"frame": s["frame"], "chunk": s["chunk"],
                             "gpu_ms": round((s["gpu_end_ns"] - s["gpu_start_ns"]) / 1e6, 4),
                             "kernel_ms": round((s["kernel_end_ns"] - s["kernel_start_ns"]) / 1e6, 4)
                                          if 0 < s["kernel_start_ns"] <= s["kernel_end_ns"] else None}
                            for s in heapq.nlargest(10, valid, key=lambda s: s["gpu_end_ns"] - s["gpu_start_ns"])],
        "metal_memory_samples": len(memory),
        "metal_allocated_max_bytes": max((s["allocated_bytes"] for s in memory), default=None),
        "memory_note": "MTLDevice resource allocations only; not whole process RSS, system pressure or evidence of a leak.",
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
