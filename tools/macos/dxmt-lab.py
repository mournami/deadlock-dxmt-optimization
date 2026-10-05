#!/usr/bin/env python3
"""Manage the isolated DXMT experiment prepared for this Mac."""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
LAB = Path(os.environ.get("DXMT_LAB_DIR", str(REPO_ROOT / ".dxmt-lab"))).expanduser().resolve()
RUNTIME = LAB / "runtime"
ORIGINAL = Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver")
BOTTLES = LAB / "bottles"
BOTTLE = "DXMT-Probe"
FILES = (
    "x86_64-windows/d3d11.dll",
    "x86_64-windows/dxgi.dll",
    "x86_64-windows/winemetal.dll",
    "x86_64-unix/winemetal.so",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private_runtime() -> None:
    if not RUNTIME.is_dir() or not RUNTIME.resolve().is_relative_to(LAB.resolve()):
        raise RuntimeError("The private runtime is missing or points outside the lab.")
    if RUNTIME.resolve() == ORIGINAL.resolve():
        raise RuntimeError("Refusing to operate on the installed CrossOver runtime.")


def environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "CX_ROOT", "CX_BOTTLE", "CX_INITIALIZED", "WINEPREFIX",
        "WINESERVER", "WINELOADER", "WINEDLLPATH",
    ):
        env.pop(key, None)
    env["CX_BOTTLE_PATH"] = str(BOTTLES)
    return env


def check_original() -> bool:
    manifest = json.loads((LAB / "original-dxmt-hashes.json").read_text())
    return all((ORIGINAL / rel).is_file() and digest(ORIGINAL / rel) == value
               for rel, value in manifest.items())


def private_prefix(bottle: str) -> Path:
    private_runtime()
    if bottle not in (BOTTLE, "Steam-DXMT-Test"):
        raise RuntimeError("Only the private probe/test bottles are allowed.")
    prefix = BOTTLES / bottle
    config = prefix / "cxbottle.conf"
    if (not prefix.resolve().is_relative_to(LAB.resolve()) or
            not config.is_file() or not config.resolve().is_relative_to(prefix.resolve())):
        raise RuntimeError("The private bottle/configuration is missing or escapes the lab.")
    return prefix


def configure(path: Path, updates: dict[str, dict[str, str]]) -> None:
    text = path.read_text()
    for section, values in updates.items():
        match = re.search(r"(?m)^\[" + re.escape(section) + r"\][ \t]*$", text)
        if not match:
            text += "\n[" + section + "]\n"
            match = re.search(r"(?m)^\[" + re.escape(section) + r"\][ \t]*$", text)
        start = match.end()
        next_section = re.search(r"(?m)^\[", text[start:])
        end = start + next_section.start() if next_section else len(text)
        body = text[start:end]
        for key, value in values.items():
            if any(char in value for char in ('"', '\n', '\r', '\\')):
                raise RuntimeError("Unsupported configuration value")
            line = '"' + key + '" = "' + value + '"'
            pattern = r'(?m)^[ \t]*"' + re.escape(key) + r'"[ \t]*=.*$'
            if re.search(pattern, body):
                body = re.sub(pattern, lambda _: line, body)
            else:
                body += "\n" + line + "\n"
        text = text[:start] + body + text[end:]
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        stream.write(text)
    try:
        pending.chmod(path.stat().st_mode & 0o777)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def configure_dxmt(bottle: str) -> None:
    prefix = private_prefix(bottle)
    sync_prefix_dxmt(prefix)
    path = prefix / "cxbottle.conf"
    backup = prefix / "cxbottle.conf.before-dll-routing"
    if not backup.exists():
        with backup.open("xb") as stream:
            stream.write(path.read_bytes())
        backup.chmod(0o600)
    # CrossOver's wrapper overrides WINEDLLPATH with Wine/DllPath. Keep both
    # PE architectures and Wine roots so the loader can still find ntdll.so.
    dll_path = ":".join("${CX_ROOT}/lib/" + rel for rel in (
        "dxmt/x86_64-windows", "wine/x86_64-windows", "wine/i386-windows",
        "dxmt", "wine",
    ))
    configure(path, {
        "Wine": {"DllPath": dll_path},
        "EnvironmentVariables": {"CX_GRAPHICS_BACKEND": "dxmt", "WINEMSYNC": "1"},
    })


def prefix_copy(source: Path, destination: Path, prefix: Path) -> None:
    if not destination.parent.resolve().is_relative_to(prefix.resolve()):
        raise RuntimeError("DLL destination escapes the private prefix.")
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        pending = Path(stream.name)
    try:
        shutil.copy2(source, pending)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def sync_prefix_dxmt(prefix: Path) -> None:
    system32 = prefix / "drive_c/windows/system32"
    if not system32.resolve().is_relative_to(prefix.resolve()) or not system32.is_dir():
        raise RuntimeError("The private system32 directory is missing or escapes the prefix.")
    backup = prefix / "dxmt-system32-stock"
    backup.mkdir(exist_ok=True, mode=0o700)
    if not backup.resolve().is_relative_to(prefix.resolve()):
        raise RuntimeError("The system32 backup escapes the private prefix.")
    names = tuple(Path(rel).name for rel in FILES[:-1])
    for name in names:
        path = system32 / name
        if not (backup / name).exists():
            backup_copy(path, backup / name)
    manifest = backup / "sha256.json"
    if not manifest.exists():
        with manifest.open("x") as stream:
            json.dump({name: digest(backup / name) for name in names}, stream, indent=2)
    expected = json.loads(manifest.read_text())
    if set(expected) != set(names) or any(digest(backup / name) != expected[name] for name in names):
        raise RuntimeError("The system32 backup has changed.")
    for name in names:
        selected = RUNTIME / "lib/dxmt/x86_64-windows" / name
        if digest(system32 / name) != digest(selected):
            prefix_copy(selected, system32 / name, prefix)


def restore_prefix_dxmt() -> None:
    for bottle in (BOTTLE, "Steam-DXMT-Test"):
        prefix = BOTTLES / bottle
        backup = prefix / "dxmt-system32-stock"
        manifest = backup / "sha256.json"
        if not manifest.exists():
            continue
        private_prefix(bottle)
        names = tuple(Path(rel).name for rel in FILES[:-1])
        expected = json.loads(manifest.read_text())
        if set(expected) != set(names) or any(digest(backup / name) != expected[name] for name in names):
            raise RuntimeError("The system32 restore backup has changed.")
        for name in names:
            prefix_copy(backup / name, prefix / "drive_c/windows/system32" / name, prefix)


def route_runtime_dxmt() -> None:
    """Wine searches its builtin directory before extra DLL paths.

    Route all four paired components in the private runtime through lib/dxmt;
    stage switches then update a single source of truth. Keep stock Wine files
    separately, because they are different from CrossOver's stock DXMT files.
    """
    private_runtime()
    assert_probe_stopped()
    backup = LAB / "stock-wine-dxmt"
    manifest_path = LAB / "stock-wine-dxmt-hashes.json"
    expected = {rel: digest(ORIGINAL / "lib/wine" / rel) if (ORIGINAL / "lib/wine" / rel).is_file() else None for rel in FILES}
    for rel in FILES:
        path = RUNTIME / "lib/wine" / rel
        target = RUNTIME / "lib/dxmt" / rel
        if not path.parent.resolve().is_relative_to(RUNTIME.resolve()):
            raise RuntimeError("Builtin Wine destination escapes the private runtime.")
        if path.is_symlink() and path.resolve() == target.resolve():
            if expected[rel] is not None and (not (backup / rel).is_file() or digest(backup / rel) != expected[rel]):
                raise RuntimeError("The private Wine routing backup is missing or changed.")
        else:
            if expected[rel] is None:
                if path.exists() or path.is_symlink():
                    raise RuntimeError("An unexpected private Wine component exists.")
                continue
            if path.is_symlink() or digest(path) != expected[rel]:
                raise RuntimeError("Private Wine builtin files differ from their installed baseline.")
            if not (backup / rel).exists():
                backup_copy(path, backup / rel)
            if digest(backup / rel) != expected[rel]:
                raise RuntimeError("The private Wine routing backup hash is invalid.")
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != expected:
            raise RuntimeError("Installed Wine changed since the private routing backup.")
    else:
        with manifest_path.open("x") as stream:
            json.dump(expected, stream, indent=2)
    changed = []
    try:
        for rel in FILES:
            path = RUNTIME / "lib/wine" / rel
            target = RUNTIME / "lib/dxmt" / rel
            if path.is_symlink() and path.resolve() == target.resolve():
                continue
            pending = path.with_name(path.name + ".dxmt-routing")
            if pending.exists() or pending.is_symlink():
                raise RuntimeError("A pending Wine routing operation needs review.")
            try:
                pending.symlink_to(os.path.relpath(target, path.parent))
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
            changed.append(rel)
    except Exception:
        for rel in changed:
            if expected[rel] is None:
                (RUNTIME / "lib/wine" / rel).unlink()
            else:
                atomic_copy(backup / rel, RUNTIME / "lib/wine" / rel)
        raise


def restore_wine_routing() -> None:
    manifest_path = LAB / "stock-wine-dxmt-hashes.json"
    if not manifest_path.exists():
        return
    expected = json.loads(manifest_path.read_text())
    backup = LAB / "stock-wine-dxmt"
    if set(expected) != set(FILES) or any(value is not None and digest(backup / rel) != value for rel, value in expected.items()):
        raise RuntimeError("Verified Wine routing backups are missing or changed.")
    for rel in FILES:
        path = RUNTIME / "lib/wine" / rel
        if expected[rel] is None:
            if path.is_symlink() and path.resolve() == (RUNTIME / "lib/dxmt" / rel).resolve():
                path.unlink()
            elif path.exists() or path.is_symlink():
                raise RuntimeError("Refusing to remove an unknown private Wine component.")
        else:
            atomic_copy(backup / rel, path)


def server_command(bottle: str, argument: str) -> subprocess.CompletedProcess:
    private_prefix(bottle)
    return subprocess.run([
        str(RUNTIME / "bin/wine"), "--bottle", bottle, "--no-gui", "--no-update",
        "--ux-app", "wineserver", argument,
    ], env=environment(), capture_output=True, text=True, timeout=20)


def server_running(bottle: str) -> bool:
    result = server_command(bottle, "-k0")
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError(f"Cannot determine private server status ({result.returncode}): {result.stderr.strip()}")
    return result.returncode == 0


def prefix_pids(prefix: Path) -> set[int]:
    result = subprocess.run(["lsof", "-nP", "-Fpn"], capture_output=True,
                            text=True, timeout=30)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError("Cannot verify that the private bottle is stopped.")
    pids = set()
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            current = int(line[1:])
        elif current and (line == "n" + str(prefix) or line.startswith("n" + str(prefix) + "/")):
            pids.add(current)
    return pids


def stop_bottle(bottle: str) -> None:
    prefix = private_prefix(bottle)
    if server_running(bottle):
        result = server_command(bottle, "-k")
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise RuntimeError(f"Private server shutdown failed ({result.returncode}): {result.stderr.strip()}")
        wait = server_command(bottle, "-w")
        if wait.returncode or wait.stderr.strip():
            raise RuntimeError(f"Private server did not finish shutting down: {wait.stderr.strip()}")
    # A code 1 from -k/-k0 is benign only after verifying the server and its
    # clients are actually gone; do not hide a real shutdown failure.
    if server_running(bottle):
        raise RuntimeError("Private bottle processes remain after shutdown.")
    remaining = prefix_pids(prefix)
    if remaining:
        reap_orphan_helpers(bottle, prefix, remaining)
    if prefix_pids(prefix):
        raise RuntimeError("Private bottle processes remain after shutdown.")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def orphan_identity(pid: int, prefix: Path) -> tuple[str, str] | None:
    status = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,ppid=,lstart=,comm="],
                            capture_output=True, text=True, timeout=10)
    fields = status.stdout.split(None, 7)
    if len(fields) != 8 or fields[0] != str(os.getuid()) or fields[1] != "1":
        return None
    name = PureWindowsPath(fields[7].strip()).name.lower()
    if name not in {"winedevice.exe", "services.exe", "plugplay.exe", "rpcss.exe"}:
        return None
    files = subprocess.run(["lsof", "-nP", "-p", str(pid), "-Ffn"],
                           capture_output=True, text=True, timeout=10)
    if files.returncode or files.stderr.strip():
        return None
    fd = ""
    cwd = None
    private_ntdll = False
    for line in files.stdout.splitlines():
        if line.startswith("f"):
            fd = line[1:]
        elif line.startswith("n"):
            if fd == "cwd":
                cwd = Path(line[1:]).resolve()
            if line[1:] == str(RUNTIME / "lib/wine/x86_64-unix/ntdll.so"):
                private_ntdll = True
    if cwd is None or not cwd.is_relative_to(prefix.resolve()) or not private_ntdll:
        return None
    return " ".join(fields[2:7]), name


def wait_pid_exit(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def reap_orphan_helpers(bottle: str, prefix: Path, pids: set[int]) -> None:
    # Only orphaned Wine services in this dedicated prefix may be reaped.
    # Never infer ownership from a name alone or kill a game/Steam client.
    identities = {pid: orphan_identity(pid, prefix) for pid in pids}
    if any(identity is None for identity in identities.values()):
        raise RuntimeError("Private bottle processes remain: an orphan helper could not be verified.")
    for pid, identity in identities.items():
        if server_running(bottle):
            raise RuntimeError("The private server restarted during orphan cleanup.")
        if not pid_alive(pid):
            continue
        if orphan_identity(pid, prefix) != identity:
            raise RuntimeError("Private orphan identity changed before shutdown.")
        os.kill(pid, signal.SIGTERM)
        if not wait_pid_exit(pid, 2):
            if server_running(bottle) or orphan_identity(pid, prefix) != identity:
                raise RuntimeError("Private orphan identity/server changed before forced shutdown.")
            os.kill(pid, signal.SIGKILL)
            if not wait_pid_exit(pid, 2):
                raise RuntimeError("A verified private orphan did not exit.")
        print(f"Stopped orphan Wine service PID {pid} in {bottle}.", flush=True)


def verify_loaded_dxmt(log: str, prefix: Path, modules: tuple[str, ...]) -> dict[str, str]:
    paths = {}
    for raw in re.findall(r'Loaded L"([^"\n]+)"', log):
        win = PureWindowsPath(raw.replace("\\\\", "\\"))
        name = win.name.lower()
        if name not in modules or not win.drive:
            continue
        drive = prefix / "dosdevices" / win.drive.lower()
        if not drive.is_symlink():
            raise RuntimeError(f"Cannot resolve the loaded DLL drive: {win.drive}")
        actual = drive.resolve().joinpath(*win.parts[1:]).resolve()
        expected = (RUNTIME / "lib/dxmt/x86_64-windows" / name).resolve()
        prefix_dll = (prefix / "drive_c/windows/system32" / name).resolve()
        if str(actual).casefold() not in (str(expected).casefold(), str(prefix_dll).casefold()):
            raise RuntimeError(f"Wrong {name} was loaded: {actual}")
        if not actual.is_file() or digest(actual) != digest(expected):
            raise RuntimeError(f"Loaded {name} hash does not match the selected DXMT.")
        paths[name] = str(actual)
    missing = set(modules) - paths.keys()
    if missing:
        raise RuntimeError("DXMT loader confirmation is missing: " + ", ".join(sorted(missing)))
    return paths


def staged_hashes() -> dict[str, str]:
    manifest = LAB / "staged-build.json"
    if manifest.is_file():
        expected = json.loads(manifest.read_text())["sha256"]
    else:
        stock = json.loads((LAB / "original-dxmt-hashes.json").read_text())
        expected = {rel: stock["lib/dxmt/" + rel] for rel in FILES}
    if set(expected) != set(FILES) or any(digest(RUNTIME / "lib/dxmt" / rel) != expected[rel] for rel in FILES):
        raise RuntimeError("The private DXMT files do not match the selected build manifest.")
    return expected


def status() -> None:
    private_runtime()
    pin = json.loads(Path(__file__).with_name("upstream-pin.json").read_text())
    env = os.environ.copy()
    developer = Path("/Applications/Xcode.app/Contents/Developer")
    if developer.is_dir():
        env["DEVELOPER_DIR"] = str(developer)
    result = subprocess.run(["xcrun", "-sdk", "macosx", "metal", "--version"], env=env, capture_output=True,
                            text=True, timeout=15)
    print(json.dumps({
        "runtime": str(RUNTIME),
        "bottle": str(BOTTLES / BOTTLE),
        "source": pin,
        "metal_compiler_available": result.returncode == 0,
        "metal_compiler": result.stdout.strip().splitlines()[0] if result.returncode == 0 else None,
        "original_dxmt_unchanged": check_original(),
        "custom_build_staged": (LAB / "staged-build.json").exists(),
    }, ensure_ascii=False, indent=2))


def atomic_copy(source: Path, destination: Path) -> None:
    if not destination.parent.resolve().is_relative_to(RUNTIME.resolve()):
        raise RuntimeError("Destination escapes the private runtime.")
    fd, tmp = tempfile.mkstemp(prefix="dxmt-stage-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def backup_copy(source: Path, destination: Path) -> None:
    if not destination.resolve().is_relative_to(LAB.resolve()):
        raise RuntimeError("Backup destination escapes the private lab.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def assert_probe_stopped() -> None:
    prefix = BOTTLES / BOTTLE
    # /usr/bin/sample is used separately on a game process; this helper never
    # starts, stops, or modifies Steam or Deadlock in the working bottle.
    lock = prefix / ".dxmt-probe-running"
    if lock.exists():
        raise RuntimeError("A probe is in progress; staging is refused.")
    processes = subprocess.run(["ps", "-axo", "pid,comm"], capture_output=True,
                               text=True, timeout=15, check=True)
    if any(str(RUNTIME) + "/" in line for line in processes.stdout.splitlines()):
        raise RuntimeError("Private runtime processes are running; close the test first.")
    # Wine may display a Windows executable name rather than its runtime path.
    modules = [RUNTIME / "lib/wine/x86_64-unix/ntdll.so", RUNTIME / "lib/dxmt" / FILES[-1]]
    loaded = subprocess.run(["lsof", "-t", *map(str, modules)], capture_output=True,
                            text=True, timeout=20)
    if loaded.stdout.strip():
        raise RuntimeError("Private Wine libraries are loaded; close the test first.")
    if loaded.returncode not in (0, 1) or loaded.stderr.strip():
        raise RuntimeError("Cannot establish that the private runtime is stopped.")


@contextmanager
def operation_lock():
    lock = LAB / ".dxmt-operation.lock"
    # Keep one stable inode: flock is released by the OS even on SIGKILL.
    # Unlinking a flock file would let callers lock different inodes at once.
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another lab operation is active; exit the previous test/Windows Steam first.") from None
        legacy = stream.read().strip()
        if legacy.isdigit() and int(legacy) != os.getpid() and pid_alive(int(legacy)):
            raise RuntimeError(f"Previous launcher PID {legacy} is still active; close its test first.")
        try:
            stream.seek(0)
            stream.truncate()
            json.dump({"locking": "flock", "pid": os.getpid()}, stream)
            stream.flush()
            yield
        finally:
            stream.seek(0)
            stream.truncate()
            stream.flush()
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_pe(path: Path) -> None:
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        raise RuntimeError(f"Not a PE DLL: {path}")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset + 26 > len(data) or data[offset:offset + 4] != b"PE\x00\x00":
        raise RuntimeError(f"Invalid PE header: {path}")
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    characteristics = struct.unpack_from("<H", data, offset + 22)[0]
    magic = struct.unpack_from("<H", data, offset + 24)[0]
    if machine != 0x8664 or magic != 0x20B or not characteristics & 0x2000:
        raise RuntimeError(f"Expected an x86_64 PE32+ DLL: {path}")


def stage(source: Path) -> None:
    private_runtime()
    assert_probe_stopped()
    if not check_original():
        raise RuntimeError("Installed CrossOver changed since setup; review the baseline first.")
    source = source.resolve()
    for rel in FILES:
        p = source / rel
        if not p.is_file():
            raise RuntimeError(f"Missing paired DXMT component: {p}")
        if p.stat().st_size == 0:
            raise RuntimeError(f"Empty component: {p}")
        if p.suffix == ".dll":
            validate_pe(p)
    arch = subprocess.run(["lipo", "-archs", str(source / FILES[-1])],
                          capture_output=True, text=True, timeout=15)
    if arch.returncode or "x86_64" not in arch.stdout.split():
        raise RuntimeError("winemetal.so must contain x86_64 for this CrossOver runtime.")
    stock = LAB / "stock-dxmt"
    baseline = json.loads((LAB / "original-dxmt-hashes.json").read_text())
    for rel in FILES:
        destination = RUNTIME / "lib/dxmt" / rel
        if not (stock / rel).exists():
            if digest(destination) != baseline["lib/dxmt/" + rel]:
                raise RuntimeError("Private stock DXMT differs from the recorded baseline.")
            backup_copy(destination, stock / rel)
    previous = LAB / "pre-stage-dxmt"
    for rel in FILES:
        backup_copy(RUNTIME / "lib/dxmt" / rel, previous / rel)
    try:
        manifest = {rel: digest(source / rel) for rel in FILES}
        for rel in FILES:
            atomic_copy(source / rel, RUNTIME / "lib/dxmt" / rel)
        if any(digest(RUNTIME / "lib/dxmt" / rel) != value for rel, value in manifest.items()):
            raise RuntimeError("Source changed while staging; reverting the paired components.")
        with tempfile.NamedTemporaryFile(mode="w", dir=LAB, delete=False) as stream:
            pending = Path(stream.name)
            json.dump({"source": str(source), "sha256": manifest}, stream, indent=2)
        try:
            pending.replace(LAB / "staged-build.json")
        finally:
            pending.unlink(missing_ok=True)
    except Exception:
        for rel in FILES:
            atomic_copy(previous / rel, RUNTIME / "lib/dxmt" / rel)
        raise
    print("The paired components were staged in the private runtime only.")


def restore() -> None:
    private_runtime()
    assert_probe_stopped()
    restore_wine_routing()
    restore_prefix_dxmt()
    stock = LAB / "stock-dxmt"
    baseline = json.loads((LAB / "original-dxmt-hashes.json").read_text())
    for rel in FILES:
        if not (stock / rel).is_file() or digest(stock / rel) != baseline["lib/dxmt/" + rel]:
            raise RuntimeError("A verified stock backup is missing; restoration is refused.")
    previous = LAB / "pre-restore-dxmt"
    for rel in FILES:
        backup_copy(RUNTIME / "lib/dxmt" / rel, previous / rel)
    try:
        for rel in FILES:
            atomic_copy(stock / rel, RUNTIME / "lib/dxmt" / rel)
        (LAB / "staged-build.json").unlink(missing_ok=True)
    except Exception:
        for rel in FILES:
            atomic_copy(previous / rel, RUNTIME / "lib/dxmt" / rel)
        raise
    print("Stock DXMT restored in the private runtime only.")


def probe() -> None:
    private_runtime()
    assert_probe_stopped()
    prefix = private_prefix(BOTTLE)
    if not (prefix / "cxbottle.conf").is_file():
        raise RuntimeError("The private probe bottle is not initialized.")
    expected_hashes = staged_hashes()
    route_runtime_dxmt()
    configure_dxmt(BOTTLE)
    logs = LAB / "probe-logs"
    logs.mkdir(exist_ok=True)
    lock = prefix / ".dxmt-probe-running"
    # The common operation lock excludes concurrent probe/stage/restore calls.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        for module in ("dxgi.dll", "d3d11.dll"):
            result = subprocess.run([
                str(RUNTIME / "bin/wine"), "--bottle", BOTTLE,
                "--no-gui", "--no-update", "--dll", "dxgi,d3d11,winemetal=b",
                "--debugmsg", "-all,+loaddll",
                "regsvr32.exe", "/s", "/n", "/i", module,
            ], env=environment(), capture_output=True, text=True, timeout=45)
            log = logs / f"{module}.log"
            log.write_text(result.stdout + result.stderr)
            required = ("dxgi.dll", "winemetal.dll") if module == "dxgi.dll" else ("d3d11.dll", "dxgi.dll", "winemetal.dll")
            paths = verify_loaded_dxmt(result.stdout + result.stderr, prefix, required)
            if staged_hashes() != expected_hashes:
                raise RuntimeError("Selected DXMT changed during the loader probe.")
            print(json.dumps({"module": module, "loaded": True, "loaded_paths": paths,
                              "exit_code": result.returncode, "log": str(log)}))
            # DXGI/D3D11 do not export DllInstall, so regsvr32 is expected to
            # report an error after loading the DLL. /n skips registration.
            # This checks loading only; it is not a device or FPS benchmark.
            if result.returncode != 4:
                raise RuntimeError(f"Unexpected DLL smoke exit code {result.returncode}; inspect {log}")
    finally:
        try:
            stop_bottle(BOTTLE)
        finally:
            lock.unlink(missing_ok=True)


def device_probe(executable_name: str = "device-probe.exe", success_marker: str = "gpu_readback") -> None:
    private_runtime()
    assert_probe_stopped()
    prefix = private_prefix(BOTTLE)
    expected = staged_hashes()
    route_runtime_dxmt()
    configure_dxmt(BOTTLE)
    if executable_name not in ("device-probe.exe", "om-state-probe.exe"):
        raise RuntimeError("Only the private GPU probes are allowed")
    executable = LAB / executable_name
    if not executable.is_file() or not executable.resolve().is_relative_to(LAB.resolve()):
        raise RuntimeError("The local device-probe.exe is missing.")
    if (prefix / "dosdevices/z:").resolve() != Path("/"):
        raise RuntimeError("The private probe bottle has no Z: mapping to the Mac filesystem.")
    log = LAB / ("probe-logs/device.log" if executable_name == "device-probe.exe" else "probe-logs/om-state.log")
    log.parent.mkdir(exist_ok=True)
    try:
        result = subprocess.run([
            str(RUNTIME / "bin/wine"), "--bottle", BOTTLE, "--no-gui", "--no-update",
            "--dll", "dxgi,d3d11,winemetal=b", "--debugmsg", "-all,+loaddll",
            "--cx-app", "Z:" + executable.as_posix(),
        ], env=environment(), capture_output=True, text=True, timeout=45)
        output = result.stdout + result.stderr
        log.write_text(output)
        paths = verify_loaded_dxmt(output, prefix, ("d3d11.dll", "dxgi.dll", "winemetal.dll"))
        if result.returncode or "device_created" not in output or success_marker not in output:
            raise RuntimeError(f"D3D11/Metal device/readback failed ({result.returncode}); inspect {log}")
        if staged_hashes() != expected:
            raise RuntimeError("Selected DXMT changed during the GPU probe.")
        print(json.dumps({"device_created": True, "gpu_readback_passed": True,
                          "loaded_paths": paths, "exit_code": result.returncode,
                          "test": executable_name, "log": str(log)}, ensure_ascii=False), flush=True)
    finally:
        stop_bottle(BOTTLE)


def shader_workers_probe() -> None:
    private_runtime()
    assert_probe_stopped()
    executable = LAB / "shader-workers-probe.exe"
    prefix = private_prefix(BOTTLE)
    if not executable.is_file() or not executable.resolve().is_relative_to(LAB.resolve()):
        raise RuntimeError("The private shader-workers-probe.exe is missing")
    log = LAB / "probe-logs/shader-workers.log"
    log.parent.mkdir(exist_ok=True)
    try:
        result = subprocess.run([
            str(RUNTIME / "bin/wine"), "--bottle", BOTTLE, "--no-gui", "--no-update",
            "--debugmsg", "-all", "--cx-app", "Z:" + executable.as_posix(),
        ], env=environment(), capture_output=True, text=True, timeout=45)
        output = result.stdout + result.stderr
        log.write_text(output)
        if result.returncode or "shader_workers_passed" not in output:
            raise RuntimeError(f"Shader worker scheduler check failed ({result.returncode}); inspect {log}")
        print("Shader worker scheduler check passed", flush=True)
    finally:
        stop_bottle(BOTTLE)


def presentation_probe() -> None:
    private_runtime()
    assert_probe_stopped()
    prefix = private_prefix(BOTTLE)
    expected = staged_hashes()
    route_runtime_dxmt()
    configure_dxmt(BOTTLE)
    executable = LAB / "presentation-probe.exe"
    if not executable.is_file() or not executable.resolve().is_relative_to(LAB.resolve()):
        raise RuntimeError("The private presentation-probe.exe is missing")
    if (prefix / "dosdevices/z:").resolve() != Path("/"):
        raise RuntimeError("The private probe bottle has no Z: mapping")
    report = LAB / "probe-logs" / ("presentation-" + str(time.time_ns()))
    report.mkdir(parents=True, mode=0o700)
    env = environment()
    env["DXMT_FRAME_REPORT_DIR"] = "Z:" + report.as_posix()
    try:
        result = subprocess.run([
            str(RUNTIME / "bin/wine"), "--bottle", BOTTLE, "--no-gui", "--no-update",
            "--dll", "dxgi,d3d11,winemetal=b", "--debugmsg", "-all,+loaddll",
            "--cx-app", "Z:" + executable.as_posix(),
        ], env=env, capture_output=True, text=True, timeout=45)
        output = result.stdout + result.stderr
        (report / "probe.log").write_text(output)
        verify_loaded_dxmt(output, prefix, ("d3d11.dll", "dxgi.dll", "winemetal.dll"))
        if result.returncode or "presentation_probe_passed" not in output:
            raise RuntimeError(f"Present/resize probe failed ({result.returncode}); inspect {report}")
        files = list(report.glob("*.events.csv"))
        if len(files) != 1:
            raise RuntimeError(f"Expected one event timeline; inspect {report}")
        lines = files[0].read_text().splitlines()
        # Queue contention may drop optional diagnostic records. Require actual
        # phases from the real DLL, not an exact count or timing threshold.
        rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
        required = {"present", "present_mutex", "present_boundary", "sync_frame",
                    "window_state", "resize_buffers", "wait_gpu_idle", "wait_cpu_fence"}
        if not required.issubset({row["event"] for row in rows}):
            raise RuntimeError(f"Present probe did not record required phases; inspect {report}")
        if not any(line.startswith("# dropped_samples=") for line in lines):
            raise RuntimeError(f"Presentation timeline did not close cleanly; inspect {report}")
        if staged_hashes() != expected:
            raise RuntimeError("Selected DXMT changed during presentation probe")
        print("Present/resize and event timeline check passed:", report, flush=True)
    finally:
        stop_bottle(BOTTLE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("probe")
    sub.add_parser("device-probe")
    sub.add_parser("restore")
    staging = sub.add_parser("stage")
    staging.add_argument("paired_install_tree", type=Path)
    args = parser.parse_args()
    if args.command == "status":
        status()
    else:
        with operation_lock():
            if args.command == "probe":
                probe()
            elif args.command == "device-probe":
                device_probe()
            elif args.command == "stage":
                stage(args.paired_install_tree)
            elif args.command == "restore":
                restore()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
