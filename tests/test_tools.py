import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

OUTPUTS = Path(__file__).resolve().parents[1] / "tools/macos"


def load(name):
    spec = importlib.util.spec_from_file_location(name, OUTPUTS / (name.replace("_", "-") + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAB_HELPER = load("dxmt_lab")
REPORT = load("dxmt_report")
RUNNER = load("dxmt_test")


def pe(machine=0x8664):
    data = bytearray(96)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\x00\x00"
    struct.pack_into("<H", data, 68, machine)
    struct.pack_into("<H", data, 86, 0x2000)
    struct.pack_into("<H", data, 88, 0x20B)
    return data


@unittest.skipUnless(Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/lib/dxmt/x86_64-unix/winemetal.so").is_file(), "CrossOver fixture library is not installed")
class StagingSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.lab = root / "lab"
        self.runtime = self.lab / "runtime"
        self.original = root / "original"
        self.source = root / "paired"
        self.real_so = Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/lib/dxmt/x86_64-unix/winemetal.so")
        for tree in (self.runtime / "lib/dxmt", self.original / "lib/dxmt", self.source):
            for relative in LAB_HELPER.FILES:
                target = tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.suffix == ".dll": target.write_bytes(pe())
                else: shutil.copy2(self.real_so, target)
        # Candidate DLLs differ from stock; no fixture is ever loaded or run.
        for relative in LAB_HELPER.FILES[:-1]:
            target = self.source / relative
            target.write_bytes(target.read_bytes() + b"candidate")
        self.baseline = {"lib/dxmt/" + rel: LAB_HELPER.digest(self.original / "lib/dxmt" / rel)
                         for rel in LAB_HELPER.FILES}
        (self.lab / "original-dxmt-hashes.json").write_text(json.dumps(self.baseline))
        for tree in (self.runtime / "lib/wine", self.original / "lib/wine"):
            (tree / "x86_64-unix").mkdir(parents=True, exist_ok=True)
            for relative in LAB_HELPER.FILES[:-1]:
                target = tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(pe() + b"stock-wine")
        self.patches = [patch.object(LAB_HELPER, name, value) for name, value in (
            ("LAB", self.lab), ("RUNTIME", self.runtime), ("ORIGINAL", self.original),
            ("BOTTLES", self.lab / "bottles"), ("assert_probe_stopped", lambda: None))]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def assert_stock(self):
        for relative in LAB_HELPER.FILES:
            expected = self.baseline["lib/dxmt/" + relative]
            self.assertEqual(LAB_HELPER.digest(self.runtime / "lib/dxmt" / relative), expected)
            self.assertEqual(LAB_HELPER.digest(self.original / "lib/dxmt" / relative), expected)

    def test_stage_restore_original_untouched(self):
        with LAB_HELPER.operation_lock(): LAB_HELPER.stage(self.source)
        self.assertTrue((self.lab / "staged-build.json").is_file())
        for relative in LAB_HELPER.FILES:
            self.assertEqual(LAB_HELPER.digest(self.original / "lib/dxmt" / relative), self.baseline["lib/dxmt/" + relative])
        with LAB_HELPER.operation_lock(): LAB_HELPER.restore()
        self.assertFalse((self.lab / "staged-build.json").exists())
        self.assert_stock()

    def test_failed_copy_rolls_back_pair(self):
        copy = LAB_HELPER.atomic_copy
        calls = 0
        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError("injected copy failure")
            copy(source, destination)
        with patch.object(LAB_HELPER, "atomic_copy", fail_once):
            with self.assertRaises(OSError): LAB_HELPER.stage(self.source)
        self.assert_stock()
        self.assertFalse((self.lab / "staged-build.json").exists())

    def test_wrong_arch_does_not_mutate(self):
        (self.source / LAB_HELPER.FILES[0]).write_bytes(pe(0xAA64))
        with self.assertRaisesRegex(RuntimeError, "x86_64"): LAB_HELPER.stage(self.source)
        self.assert_stock()

    def test_restore_rejects_tampered_backup(self):
        LAB_HELPER.stage(self.source)
        (self.lab / "stock-dxmt" / LAB_HELPER.FILES[0]).write_bytes(b"bad backup")
        with self.assertRaisesRegex(RuntimeError, "verified stock"): LAB_HELPER.restore()
        self.assertEqual((self.runtime / "lib/dxmt" / LAB_HELPER.FILES[0]).read_bytes(),
                         (self.source / LAB_HELPER.FILES[0]).read_bytes())

    def test_common_lock(self):
        with LAB_HELPER.operation_lock():
            with self.assertRaisesRegex(RuntimeError, "Another lab operation"):
                with LAB_HELPER.operation_lock(): pass
        self.assertEqual((self.lab / ".dxmt-operation.lock").read_text(), "")

    def test_escape_refused(self):
        with patch.object(LAB_HELPER, "RUNTIME", self.original):
            with self.assertRaises(RuntimeError): LAB_HELPER.private_runtime()

    def test_backup_escape_refused(self):
        (self.lab / "pre-stage-dxmt").symlink_to(self.original / "lib/dxmt")
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            LAB_HELPER.stage(self.source)
        self.assert_stock()

    def test_builtin_routing_switch_and_restore(self):
        LAB_HELPER.route_runtime_dxmt()
        for relative in LAB_HELPER.FILES:
            self.assertEqual((self.runtime / "lib/wine" / relative).resolve(),
                             (self.runtime / "lib/dxmt" / relative).resolve())
        LAB_HELPER.stage(self.source)
        for relative in LAB_HELPER.FILES:
            self.assertEqual(LAB_HELPER.digest(self.runtime / "lib/wine" / relative),
                             LAB_HELPER.digest(self.source / relative))
        LAB_HELPER.route_runtime_dxmt()  # Idempotent when already routed.
        LAB_HELPER.restore()
        for relative in LAB_HELPER.FILES[:-1]:
            self.assertEqual((self.runtime / "lib/wine" / relative).read_bytes(), pe() + b"stock-wine")
            self.assertFalse((self.runtime / "lib/wine" / relative).is_symlink())
        self.assertFalse((self.runtime / "lib/wine" / LAB_HELPER.FILES[-1]).exists())
        self.assert_stock()

    def test_routing_rejects_tampered_stock(self):
        LAB_HELPER.route_runtime_dxmt()
        (self.lab / "stock-wine-dxmt" / LAB_HELPER.FILES[0]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "backup"):
            LAB_HELPER.route_runtime_dxmt()


class PrefixSafety(unittest.TestCase):
    def test_configuration_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "cxbottle.conf"
            text = '[Bottle]\n"MenuMode" = "install"\n[Wine]\n"Version" = "win10"\n[EnvironmentVariables]\n"WINEMSYNC" = "0"\n'
            path.write_text(text)
            RUNNER.configure(path, {"Bottle": {"MenuMode": "ignore", "AssocMode": "ignore"},
                                    "EnvironmentVariables": {"WINEMSYNC": "1", "DXMT_FRAME_REPORT_DIR": "Z:/test/reports"}})
            updated = path.read_text()
            self.assertIn('[Wine]\n"Version" = "win10"', updated)
            self.assertEqual(updated.count('"MenuMode" = "ignore"'), 1)
            self.assertEqual(updated.count('"WINEMSYNC" = "1"'), 1)
            self.assertIn('"DXMT_FRAME_REPORT_DIR" = "Z:/test/reports"', updated)

    def test_live_source_copy_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            main = root / "main"
            main.mkdir()
            (main / "cxbottle.conf").write_text("source")
            result = subprocess.CompletedProcess([], 0, "p123\nn" + str(main / "user.reg") + "\n", "")
            with patch.object(RUNNER, "MAIN", main), patch.object(RUNNER, "PREFIX", root / "private"), \
                 patch.object(RUNNER.lab, "private_runtime", lambda: None), \
                 patch.object(RUNNER.subprocess, "run", return_value=result) as call:
                with self.assertRaisesRegex(RuntimeError, "Quit All Applications"): RUNNER.prepare()
                self.assertEqual(call.call_count, 1)
            self.assertEqual((main / "cxbottle.conf").read_text(), "source")
            self.assertFalse((root / "private").exists())

    def test_original_prefix_cannot_be_stopped(self):
        with patch.object(RUNNER, "PREFIX", RUNNER.MAIN):
            with self.assertRaisesRegex(RuntimeError, "outside"): RUNNER.check_prefix()


class LoaderAndShutdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.lab = root / "lab"
        self.runtime = self.lab / "runtime"
        self.bottles = self.lab / "bottles"
        self.prefix = self.bottles / LAB_HELPER.BOTTLE
        (self.prefix / "dosdevices").mkdir(parents=True)
        (self.prefix / "dosdevices/z:").symlink_to(root, target_is_directory=True)
        (self.prefix / "cxbottle.conf").write_text('[Bottle]\n"WineArch" = "win64"\n[EnvironmentVariables]\n"WINEMSYNC" = "0"\n')
        system32 = self.prefix / "drive_c/windows/system32"
        system32.mkdir(parents=True)
        (self.prefix / "dosdevices/c:").symlink_to(self.prefix / "drive_c", target_is_directory=True)
        for name in ("dxgi.dll", "winemetal.dll", "d3d11.dll"):
            p = self.runtime / "lib/dxmt/x86_64-windows" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(pe())
            (system32 / name).write_bytes(pe() + b"stock-prefix")
        self.patches = [patch.object(LAB_HELPER, name, value) for name, value in (
            ("LAB", self.lab), ("RUNTIME", self.runtime), ("BOTTLES", self.bottles))]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def log(self, tree="dxmt"):
        return '\n'.join('Loaded L"' + str(Path("Z:") / "lab/runtime/lib" / tree /
            "x86_64-windows" / name).replace('/', '\\\\') + '" at 0000: builtin'
            for name in ("dxgi.dll", "winemetal.dll", "d3d11.dll"))

    def test_actual_custom_paths_required(self):
        paths = LAB_HELPER.verify_loaded_dxmt(self.log(), self.prefix, ("d3d11.dll", "dxgi.dll", "winemetal.dll"))
        self.assertEqual(len(paths), 3)
        with self.assertRaisesRegex(RuntimeError, "Wrong"):
            LAB_HELPER.verify_loaded_dxmt(self.log("wine"), self.prefix, ("d3d11.dll", "dxgi.dll", "winemetal.dll"))

    def test_missing_dependency_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "missing"):
            LAB_HELPER.verify_loaded_dxmt('Loaded L"Z:\\\\lab\\\\runtime\\\\lib\\\\dxmt\\\\x86_64-windows\\\\d3d11.dll"',
                                          self.prefix, ("d3d11.dll", "winemetal.dll"))

    def test_system32_must_match_selected_build(self):
        log = 'Loaded L"C:\\\\windows\\\\system32\\\\d3d11.dll" at 0000: builtin'
        with self.assertRaisesRegex(RuntimeError, "hash"):
            LAB_HELPER.verify_loaded_dxmt(log, self.prefix, ("d3d11.dll",))
        LAB_HELPER.sync_prefix_dxmt(self.prefix)
        paths = LAB_HELPER.verify_loaded_dxmt(log, self.prefix, ("d3d11.dll",))
        self.assertEqual(paths["d3d11.dll"], str((self.prefix / "drive_c/windows/system32/d3d11.dll").resolve()))
        LAB_HELPER.restore_prefix_dxmt()
        self.assertEqual((self.prefix / "drive_c/windows/system32/d3d11.dll").read_bytes(), pe() + b"stock-prefix")

    def test_configuration_idempotent_and_backup_preserved(self):
        before = (self.prefix / "cxbottle.conf").read_bytes()
        LAB_HELPER.configure_dxmt(LAB_HELPER.BOTTLE)
        first = (self.prefix / "cxbottle.conf").read_bytes()
        LAB_HELPER.configure_dxmt(LAB_HELPER.BOTTLE)
        self.assertEqual(first, (self.prefix / "cxbottle.conf").read_bytes())
        self.assertEqual(before, (self.prefix / "cxbottle.conf.before-dll-routing").read_bytes())
        self.assertIn(b'${CX_ROOT}/lib/wine', first)
        self.assertIn(b'${CX_ROOT}/lib/dxmt/x86_64-windows', first)

    def test_no_server_is_benign_only_without_clients(self):
        with patch.object(LAB_HELPER, "server_running", return_value=False), \
             patch.object(LAB_HELPER, "prefix_pids", return_value=set()), \
             patch.object(LAB_HELPER, "server_command") as command:
            LAB_HELPER.stop_bottle(LAB_HELPER.BOTTLE)
            command.assert_not_called()
        with patch.object(LAB_HELPER, "server_running", return_value=False), \
             patch.object(LAB_HELPER, "prefix_pids", return_value={123}):
            with self.assertRaisesRegex(RuntimeError, "remain"):
                LAB_HELPER.stop_bottle(LAB_HELPER.BOTTLE)

    def test_running_server_wait_and_postcheck(self):
        ok = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(LAB_HELPER, "server_running", side_effect=[True, False]), \
             patch.object(LAB_HELPER, "prefix_pids", return_value=set()), \
             patch.object(LAB_HELPER, "server_command", return_value=ok) as command:
            LAB_HELPER.stop_bottle(LAB_HELPER.BOTTLE)
            self.assertEqual([call.args[1] for call in command.call_args_list], ["-k", "-w"])

    def test_shutdown_errors_not_hidden(self):
        error = subprocess.CompletedProcess([], 2, '', 'failed')
        with patch.object(LAB_HELPER, "server_running", return_value=True), \
             patch.object(LAB_HELPER, "server_command", return_value=error):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                LAB_HELPER.stop_bottle(LAB_HELPER.BOTTLE)

    def test_cannot_address_working_bottle(self):
        with self.assertRaisesRegex(RuntimeError, "Only the private"):
            LAB_HELPER.server_command("Steam", "-k")

    def test_unverified_orphan_is_never_signalled(self):
        with patch.object(LAB_HELPER, "orphan_identity", return_value=None), \
             patch.object(LAB_HELPER.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                LAB_HELPER.reap_orphan_helpers(LAB_HELPER.BOTTLE, self.prefix, {123})
            kill.assert_not_called()

    def test_verified_orphan_term_then_kill_with_recheck(self):
        identity = ("fixed-start", "winedevice.exe")
        with patch.object(LAB_HELPER, "orphan_identity", return_value=identity), \
             patch.object(LAB_HELPER, "server_running", return_value=False), \
             patch.object(LAB_HELPER, "pid_alive", return_value=True), \
             patch.object(LAB_HELPER, "wait_pid_exit", side_effect=[False, True]), \
             patch.object(LAB_HELPER.os, "kill") as kill:
            LAB_HELPER.reap_orphan_helpers(LAB_HELPER.BOTTLE, self.prefix, {123})
            self.assertEqual([c.args for c in kill.call_args_list], [(123, signal.SIGTERM), (123, signal.SIGKILL)])

    def test_orphan_pid_reuse_prevents_forced_kill(self):
        identity = ("fixed-start", "winedevice.exe")
        with patch.object(LAB_HELPER, "orphan_identity", side_effect=[identity, identity, ("new-start", "winedevice.exe")]), \
             patch.object(LAB_HELPER, "server_running", return_value=False), \
             patch.object(LAB_HELPER, "pid_alive", return_value=True), \
             patch.object(LAB_HELPER, "wait_pid_exit", return_value=False), \
             patch.object(LAB_HELPER.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "changed"):
                LAB_HELPER.reap_orphan_helpers(LAB_HELPER.BOTTLE, self.prefix, {123})
            self.assertEqual([c.args for c in kill.call_args_list], [(123, signal.SIGTERM)])


class CrashSafeLock(unittest.TestCase):
    def test_process_death_releases_flock_and_dead_legacy_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = OUTPUTS / "dxmt-lab.py"
            code = """import importlib.util, pathlib, signal, sys
s = importlib.util.spec_from_file_location('lab', sys.argv[1])
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.LAB = pathlib.Path(sys.argv[2])
with m.operation_lock():
    print('ready', flush=True)
    signal.pause()
"""
            child = subprocess.Popen([sys.executable, "-u", "-c", code, str(source), str(root)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with patch.object(LAB_HELPER, "LAB", root):
                    with self.assertRaisesRegex(RuntimeError, "Another lab operation"):
                        with LAB_HELPER.operation_lock(): pass
                    child.kill(); child.wait(timeout=10)
                    with LAB_HELPER.operation_lock(): pass
                    (root / ".dxmt-operation.lock").write_text(str(child.pid))
                    with LAB_HELPER.operation_lock(): pass
                    self.assertEqual((root / ".dxmt-operation.lock").read_text(), "")
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=10)
                child.stdout.close()

    def test_live_legacy_pid_blocks_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
            try:
                (root / ".dxmt-operation.lock").write_text(str(child.pid))
                with patch.object(LAB_HELPER, "LAB", root):
                    with self.assertRaisesRegex(RuntimeError, "still active"):
                        with LAB_HELPER.operation_lock(): pass
                self.assertEqual((root / ".dxmt-operation.lock").read_text(), str(child.pid))
            finally:
                child.kill(); child.wait(timeout=10)


class ReportAnalysis(unittest.TestCase):
    def write_report(self, root, *, gap=False, footer=True):
        target = Path(root) / "synthetic.csv"
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(REPORT.FIELDS)
        for frame in range(200):
            writer.writerow((frame + (1 if gap and frame >= 100 else 0), 10000000, 1000000,
                             100000, 2000000, 4, 1, 0))
        if footer: stream.write("# dropped_samples=0\n")
        target.write_text(stream.getvalue())
        return target

    def test_units_and_rate(self):
        with tempfile.TemporaryDirectory() as root:
            result = REPORT.summarize(self.write_report(root), skip_seconds=0)
        self.assertEqual(result["mean_interval_ms"], 10)
        self.assertEqual(result["mean_boundary_rate_hz"], 100)
        self.assertEqual(result["slowest_1pct_boundary_rate_hz"], 100)
        self.assertEqual(result["mean_wait_ms"]["command_queue_wait"], 1)

    def test_loss_and_incomplete_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for options in ({"gap": True}, {"footer": False}):
                target = self.write_report(root, **options)
                with self.assertRaises(ValueError): REPORT.summarize(target, skip_seconds=0)
                REPORT.summarize(target, skip_seconds=0, allow_incomplete=True)

    def test_selected_window(self):
        with tempfile.TemporaryDirectory() as root:
            result = REPORT.summarize(self.write_report(root), skip_seconds=.5, duration_seconds=1)
        self.assertEqual(result["samples"], 100)
        self.assertEqual(result["mean_interval_ms"], 10)


if __name__ == "__main__": unittest.main()
