#!/usr/bin/env python3
"""Manual test launcher. The run/stop commands launch private Wine executables."""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal

SPEC = importlib.util.spec_from_file_location("dxmt_lab", Path(__file__).with_name("dxmt-lab.py"))
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
TEST_BOTTLE = "Steam-DXMT-Test"
PREFIX = lab.BOTTLES / TEST_BOTTLE
MAIN = Path.home() / "Library/Application Support/CrossOver/Bottles" / os.environ.get("DXMT_STEAM_BOTTLE", "Steam")
GAME = Path(os.environ.get("DXMT_GAME_EXE", str(MAIN / "drive_c/Program Files (x86)/Steam/steamapps/common/Deadlock/game/bin/win64/deadlock.exe"))).expanduser()


def check_prefix() -> None:
    if not PREFIX.resolve().is_relative_to(lab.LAB.resolve()) or PREFIX.resolve() == MAIN.resolve():
        raise RuntimeError("Refusing to use a prefix outside the private lab")
    if not (PREFIX / ".dxmt-test-prefix.json").is_file():
        raise RuntimeError("The private Steam test prefix has not been prepared")


configure = lab.configure


def prepare() -> None:
    lab.private_runtime()
    if PREFIX.exists():
        check_prefix()
        return
    if not (MAIN / "cxbottle.conf").is_file():
        raise RuntimeError("The working Steam bottle is missing")
    # Inspect open paths without displaying account data. Copying a live Wine
    # registry/Steam update is deliberately refused.
    open_files = subprocess.run(["lsof", "-nP", "-Fpn"], capture_output=True, text=True, timeout=30)
    pids = set()
    current_pid = None
    for line in open_files.stdout.splitlines():
        if line.startswith("p"): current_pid = line[1:]
        elif (line == "n" + str(MAIN) or line.startswith("n" + str(MAIN) + "/")) and current_pid: pids.add(current_pid)
    if pids:
        raise RuntimeError("First close the working Steam bottle with Quit All Applications; open-file PIDs: " + ", ".join(sorted(pids)))
    if open_files.returncode not in (0, 1):
        raise RuntimeError("Could not check whether the working bottle is stopped")
    lab.BOTTLES.mkdir(exist_ok=True, mode=0o700)
    lab.BOTTLES.chmod(0o700)
    pending = lab.BOTTLES / (TEST_BOTTLE + "-copying")
    if pending.exists():
        raise RuntimeError("An incomplete private copy exists; review it before retrying")
    tracked = ("cxbottle.conf", "user.reg", "system.reg", "drive_c/Program Files (x86)/Steam/steam.exe")
    snapshot = {relative: lab.digest(MAIN / relative) for relative in tracked}
    subprocess.run(["/bin/cp", "-cR", str(MAIN), str(pending)], check=True)
    pending.chmod(0o700)
    if any(lab.digest(MAIN / relative) != digest or lab.digest(pending / relative) != digest
           for relative, digest in snapshot.items()):
        raise RuntimeError("The source bottle changed while being copied; the incomplete copy was retained for review")
    if not (pending / "drive_c").resolve().is_relative_to(pending.resolve()):
        raise RuntimeError("Private C: drive points outside the copied prefix")
    configure(pending / "cxbottle.conf", {
        "Bottle": {"MenuMode": "ignore", "AssocMode": "ignore"},
        "EnvironmentVariables": {"CX_GRAPHICS_BACKEND": "dxmt", "WINEMSYNC": "1"},
    })
    (pending / ".dxmt-test-prefix.json").write_text(json.dumps({
        "source": str(MAIN), "original_config_sha256": snapshot["cxbottle.conf"],
        "original_user_registry_sha256": snapshot["user.reg"],
        "note": "Steam account data is local in this private copy; installed game files are shared",
    }, indent=2))
    pending.rename(PREFIX)


def stop() -> None:
    check_prefix()
    lab.stop_bottle(TEST_BOTTLE)


def selected_build(variant: str, require_ready: bool = False) -> Path:
    ready = lab.LAB / ("ready-" + variant + ".json")
    if not ready.is_file():
        if require_ready:
            raise RuntimeError("No validated build is ready for " + variant)
        return lab.LAB / ("install-" + variant)
    record = json.loads(ready.read_text())
    source = Path(record["install"]).resolve()
    if not source.is_relative_to(lab.LAB.resolve()) or record.get("variant") != variant:
        raise RuntimeError("The ready build escapes the lab or has a wrong variant")
    if record.get("validation") != "dll-and-gpu-readback":
        raise RuntimeError("The ready build has not passed validation")
    if set(record["sha256"]) != set(lab.FILES):
        raise RuntimeError("The ready build is incomplete")
    if any(not (source / rel).resolve().is_relative_to(source) for rel in lab.FILES):
        raise RuntimeError("The ready component escapes its snapshot")
    if any(not (source / rel).is_file() or lab.digest(source / rel) != value
           for rel, value in record["sha256"].items()):
        raise RuntimeError("The ready build changed after validation")
    return source


def run(variant: str, require_ready: bool = False) -> None:
    # Hold the common lock through the launcher lifetime. `stop` intentionally
    # does not take it, so the user can end their own private test.
    with lab.operation_lock():
        prepare()
        check_prefix()
        if not GAME.is_file():
            raise RuntimeError("Deadlock is missing; set DXMT_GAME_EXE to your installed deadlock.exe")
        if not (PREFIX / "drive_c/Program Files (x86)/Steam/steam.exe").is_file():
            raise RuntimeError("Windows Steam is missing from the private copy")
        if lab.server_running(TEST_BOTTLE):
            raise RuntimeError("The previous test/Windows Steam is still running. Exit it or use DXMT stop test.command first.")
        # Closing a Terminal can kill the launcher while leaving Wine services.
        # With no server, stop() reaps only verified orphan services, never apps.
        stop()
        lab.stage(selected_build(variant, require_ready))
        # Loading must succeed before opening Steam. This is still not a
        # device/rendering test, but catches broken paired libraries early.
        lab.probe()
        lab.device_probe()
        lab.configure_dxmt(TEST_BOTTLE)
        report_dir = lab.LAB / "reports" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + variant)
        report_dir.mkdir(parents=True, mode=0o700)
        ready = lab.LAB / ("ready-" + variant + ".json")
        build_record = json.loads(ready.read_text()) if ready.is_file() else {}
        (report_dir / "session.json").write_text(json.dumps({
            "variant": variant, "source_commit": build_record.get("source_commit"),
            "source_files_sha256": build_record.get("source_files_sha256", {}),
            "loaded_dxmt_sha256": lab.staged_hashes(), "om_state_dedup": variant == "experiment",
            "metric": "CPU/encoder wall time; not display FPS or input latency",
        }, indent=2) + "\n")
        if (PREFIX / "dosdevices/z:").resolve() != Path("/"):
            raise RuntimeError("The test bottle has no Z: mapping to the Mac filesystem")
        wine_report = "Z:" + str(report_dir)
        dedup = "1" if variant == "experiment" else "0"
        configure(PREFIX / "cxbottle.conf", {"EnvironmentVariables": {
            "DXMT_FRAME_REPORT_DIR": wine_report, "DXMT_OM_STATE_DEDUP": dedup}})
        env = lab.environment()
        env["DXMT_FRAME_REPORT_DIR"] = wine_report
        env["DXMT_OM_STATE_DEDUP"] = dedup
        env["MTL_HUD_ENABLED"] = "1"
        env["WINEDEBUG"] = "-all"
        print("Starting private Windows Steam / Deadlock (DX11).", flush=True)
        print("Use the same training scene and settings for both variants. Exit the game, then Steam.", flush=True)
        print("Reports:", report_dir, flush=True)
        result = 1
        try:
            with (report_dir / "launcher.log").open("w") as log:
                result = subprocess.run([
                    str(lab.RUNTIME / "bin/wine"), "--bottle", TEST_BOTTLE, "--no-gui", "--wait-children",
                    "--no-update", "--dll", "dxgi,d3d11,winemetal=b",
                    r"C:\Program Files (x86)\Steam\steam.exe", "-applaunch", "1422450", "-dx11",
                ], env=env, stdout=log, stderr=subprocess.STDOUT).returncode
        finally:
            stop()
        print("Launcher exit code:", result)
        print("Reports:", report_dir)


def main() -> None:
    def terminate(_number, _frame):
        # Let run()'s finally and the lock context clean up on Terminal shutdown.
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("stop")
    launch = sub.add_parser("run")
    launch.add_argument("--variant", choices=("baseline", "experiment"), default="experiment")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            with lab.operation_lock(): prepare()
            print("Private Steam test prefix prepared")
        elif args.command == "stop": stop()
        else: run(args.variant)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__": main()
