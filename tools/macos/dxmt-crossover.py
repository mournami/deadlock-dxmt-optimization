#!/usr/bin/env python3
"""CrossOver menu integration and atomic promotion of tested private builds."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

def load_runner():
    spec = importlib.util.spec_from_file_location("dxmt_runner", Path(__file__).with_name("dxmt-test.py"))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner

runner = load_runner()
lab = runner.lab
CONTROL_NAME = "DXMT"
CONTROL = Path.home() / "Library/Application Support/CrossOver/Bottles" / CONTROL_NAME

def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)

def promote(variant: str) -> None:
    # Compile/install may happen separately. Publication happens only while the
    # private runtime is stopped, after a real DLL + Metal readback check.
    with lab.operation_lock():
        manifest = json.loads((lab.LAB / ("build-" + variant + "-manifest.json")).read_text())
        source = Path(manifest["install"]).resolve()
        if not source.is_relative_to(lab.LAB.resolve()) or manifest["variant"] != variant:
            raise RuntimeError("Build manifest is outside the lab or has the wrong variant")
        hashes = manifest["binary_sha256"]
        if set(hashes) != set(lab.FILES) or any(lab.digest(source / rel) != hashes[rel] for rel in lab.FILES):
            raise RuntimeError("Build files differ from the completed build manifest")
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
        snapshot = lab.LAB / "ready-builds" / (variant + "-" + identity)
        if not snapshot.resolve().is_relative_to(lab.LAB.resolve()):
            raise RuntimeError("Ready snapshot escapes the private lab")
        snapshot.mkdir(parents=True, exist_ok=True, mode=0o700)
        for rel in lab.FILES:
            target = snapshot / rel
            if not target.resolve().is_relative_to(snapshot.resolve()):
                raise RuntimeError("Ready component escapes its snapshot")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if lab.digest(target) != hashes[rel]:
                    raise RuntimeError("Existing ready snapshot changed; retained for inspection")
            else:
                shutil.copy2(source / rel, target)
        lab.stage(snapshot)
        lab.probe()
        lab.device_probe()
        state_tested = "om_state_dedup_default" in manifest
        if state_tested:
            lab.device_probe("om-state-probe.exe", "om_state_readback_passed")
        worker_tested = "shader_worker_default" in manifest
        if worker_tested:
            lab.shader_workers_probe()
        presentation_tested = bool(manifest.get("dxgi_events"))
        if presentation_tested:
            lab.presentation_probe()
        # The previous ready pointer survives every failure above.
        atomic_json(lab.LAB / ("ready-" + variant + ".json"), {
            "variant": variant, "install": str(snapshot), "sha256": hashes,
            "source_commit": manifest["source_commit"],
            "source_files_sha256": manifest["source_files_sha256"],
            "validated_at": datetime.now().isoformat(), "validation": "dll-and-gpu-readback",
            "om_state_tested": state_tested,
            "shader_worker_tested": worker_tested,
            "presentation_tested": presentation_tested,
        })
        print("Ready build published:", variant, identity)

def entry(action: str) -> None:
    if action in ("baseline", "experiment"):
        runner.run(action, require_ready=True)
    elif action == "stop":
        runner.stop()
        print("Тестовая бутылка остановлена.")
    elif action == "reports":
        reports = lab.LAB / "reports"
        reports.mkdir(exist_ok=True, mode=0o700)
        subprocess.run(["/usr/bin/open", str(reports)], check=True)
    elif action == "check":
        lab.private_runtime()
        runner.check_prefix()
        for variant in ("baseline", "experiment"):
            runner.selected_build(variant, require_ready=True)
        print("Baseline и experiment готовы. Можно запускать из раздела DXMT.")

def install() -> None:
    lab.private_runtime()
    runner.check_prefix()
    for variant in ("baseline", "experiment"):
        runner.selected_build(variant, require_ready=True)
    marker = CONTROL / ".dxmt-menu.json"
    if CONTROL.exists() and not marker.is_file():
        raise RuntimeError("A bottle named DXMT already exists and is not owned by this menu installer")
    env = lab.environment()
    env.pop("CX_BOTTLE_PATH", None)
    if not CONTROL.exists():
        subprocess.run([str(lab.ORIGINAL / "bin/cxbottle"), "--bottle", CONTROL_NAME,
                        "--create", "--template", "win10_64"], env=env, check=True, timeout=120)
        atomic_json(marker, {"kind": "dxmt-launch-menu", "lab": str(lab.LAB)})
    elif json.loads(marker.read_text()).get("lab") != str(lab.LAB):
        raise RuntimeError("The DXMT menu belongs to a different lab")
    gui = lab.LAB / "crossover-menu"
    gui.mkdir(exist_ok=True, mode=0o700)
    executable = gui / "DXMT Launcher"
    developer = os.environ.get("DXMT_DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    build_env = os.environ.copy()
    build_env["DEVELOPER_DIR"] = developer
    subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", str(Path(__file__).with_name("DXMTLauncher.swift")),
                    "-o", str(executable)], env=build_env, check=True)
    config = gui / "launch.json"
    atomic_json(config, {
        "python": sys.executable, "script": str(Path(__file__).resolve()), "lab": str(lab.LAB),
        "environment": {"DXMT_LAB_DIR": str(lab.LAB), "DXMT_GAME_EXE": str(runner.GAME),
                        "DXMT_STEAM_BOTTLE": runner.MAIN.name},
    })
    lab.configure(CONTROL / "cxbottle.conf", {
        "Bottle": {"MenuMode": "install", "AssocMode": "ignore",
                   "Description": "Baseline / experiment: private Steam-DXMT-Test; use Stop test to exit"},
        "EnvironmentVariables": {"CX_GRAPHICS_BACKEND": "dxmt", "WINEMSYNC": "1"},
    })
    items = {"baseline": "Deadlock baseline", "experiment": "Deadlock experiment",
             "stop": "Остановить тест", "check": "Проверить сборки", "reports": "Отчёты DXMT"}
    for action, label in items.items():
        command = shlex.join([str(executable), str(config), action])
        subprocess.run([str(lab.ORIGINAL / "bin/cxmenu"), "--bottle", CONTROL_NAME,
                        "--create", "StartMenu/" + label, "--type", "raw", "--command", command,
                        "--mode", "install"], env=env, check=True)
    subprocess.run([str(lab.ORIGINAL / "bin/cxmenu"), "--bottle", CONTROL_NAME, "--install"], env=env, check=True)
    print("CrossOver DXMT menu installed. Game was not launched.")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    publish = sub.add_parser("publish")
    publish.add_argument("--variant", choices=("baseline", "experiment"), required=True)
    run = sub.add_parser("entry")
    run.add_argument("action", choices=("baseline", "experiment", "stop", "check", "reports"))
    args = parser.parse_args()
    if args.command == "install": install()
    elif args.command == "publish": promote(args.variant)
    else: entry(args.action)

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
