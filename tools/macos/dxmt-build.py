#!/usr/bin/env python3
"""Build the pinned DXMT experiment without launching Wine, CrossOver or a game."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[2]
LAB = Path(os.environ.get("DXMT_LAB_DIR", str(REPO_ROOT / ".dxmt-lab"))).expanduser().resolve()
SOURCE = REPO_ROOT
TOOLS = LAB / "build-tools/bin"
CHAIN = SOURCE / "toolchains"
XCODE = Path("/Applications/Xcode.app/Contents/Developer")
CLT = Path("/Library/Developer/CommandLineTools")
ASSETS = (
    ("wine", "https://github.com/3Shain/wine/releases/download/v8.16-3shain/wine.tar.gz", 230434672),
    ("mingw", "https://github.com/mstorsjo/llvm-mingw/releases/download/20231017/llvm-mingw-20231017-ucrt-macos-universal.tar.xz", 87862440),
    ("llvm", "https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.7/llvm-15.0.7.src.tar.xz", 52935892),
    ("cmake", "https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.7/cmake-15.0.7.src.tar.xz", 6972),
)


def run(args: list[str], env: dict[str, str], cwd: Path = LAB) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def environment(developer: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DEVELOPER_DIR"] = str(developer)
    env["PATH"] = str(TOOLS) + os.pathsep + env.get("PATH", "")
    return env


def dependencies() -> None:
    run(["git", "submodule", "update", "--init", "--depth", "1", "include/native/directx"], os.environ.copy(), SOURCE)
    CHAIN.mkdir(exist_ok=True)
    downloads = LAB / "downloads"
    downloads.mkdir(exist_ok=True)

    def fetch(asset: tuple[str, str, int]) -> dict[str, str]:
        name, url, size = asset
        archive = downloads / url.rsplit("/", 1)[1]
        if not archive.is_file() or archive.stat().st_size != size:
            pending = archive.with_suffix(archive.suffix + ".partial")
            print(f"Downloading {name}: {size // 1048576} MiB", flush=True)
            with urllib.request.urlopen(url, timeout=90) as response, pending.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            if pending.stat().st_size != size:
                raise RuntimeError(f"Unexpected release asset size: {name}")
            pending.replace(archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        marker = CHAIN / f".{name}-archive-sha256"
        if marker.exists():
            if marker.read_text().strip() != digest:
                raise RuntimeError(f"Dependency changed: {name}; review before replacing it")
            return {"name": name, "url": url, "sha256": digest}
        with tempfile.TemporaryDirectory(prefix=f"extract-{name}-", dir=LAB) as tmp:
            staging = Path(tmp)
            with tarfile.open(archive) as packed:
                packed.extractall(staging, filter="data")
            if name == "wine":
                target = CHAIN / "wine"
                if target.exists():
                    raise RuntimeError("Untracked Wine toolchain exists; refusing to overwrite it")
                # The release archive is an install tree, not a single source folder.
                shutil.move(str(staging), target)
            else:
                entries = list(staging.iterdir())
                if len(entries) != 1 or not entries[0].is_dir():
                    raise RuntimeError(f"Unexpected archive layout: {name}")
                target = CHAIN / ("cmake" if name == "cmake" else entries[0].name)
                if target.exists():
                    raise RuntimeError(f"Untracked dependency exists: {target}")
                shutil.move(str(entries[0]), target)
        marker.write_text(digest + "\n")
        print(f"Ready: {name}", flush=True)
        return {"name": name, "url": url, "sha256": digest}

    with ThreadPoolExecutor(max_workers=2) as pool:
        manifest = list(pool.map(fetch, ASSETS))
    (LAB / "dependency-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def llvm(jobs: int) -> None:
    env = environment(CLT)
    build = CHAIN / "llvm-darwin-build"
    sdk = Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], env=env, text=True).strip())
    run([
        str(TOOLS / "cmake"), "-B", str(build), "-S", str(CHAIN / "llvm-15.0.7.src"),
        "-G", "Ninja", f"-DCMAKE_INSTALL_PREFIX={CHAIN / 'llvm-darwin'}",
        "-DCMAKE_OSX_ARCHITECTURES=x86_64", "-DLLVM_HOST_TRIPLE=x86_64-apple-darwin",
        f"-DCMAKE_OSX_SYSROOT={sdk}", "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0",
        "-DCMAKE_C_COMPILER=/Library/Developer/CommandLineTools/usr/bin/clang",
        "-DCMAKE_CXX_COMPILER=/Library/Developer/CommandLineTools/usr/bin/clang++",
        "-DLLVM_ENABLE_ASSERTIONS=On", "-DLLVM_ENABLE_ZSTD=Off", "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_TARGETS_TO_BUILD=", "-DLLVM_BUILD_TOOLS=Off", "-DLLVM_INCLUDE_TESTS=Off",
        "-DLLVM_INCLUDE_BENCHMARKS=Off", "-DLLVM_INCLUDE_EXAMPLES=Off",
        "-DPACKAGE_VENDOR=DXMT", "-DLLVM_VERSION_PRINTER_SHOW_HOST_TARGET_INFO=Off",
    ], env)
    run([str(TOOLS / "cmake"), "--build", str(build), "--parallel", str(jobs)], env)
    run([str(TOOLS / "cmake"), "--install", str(build)], env)


def meson_array(items: list[str]) -> str:
    # Meson strings, not shell quoting. Paths on this Mac contain spaces.
    return "[" + ", ".join("'" + item.replace("\\", "\\\\").replace("'", "\\'") + "'" for item in items) + "]"


def build(variant: str, jobs: int) -> None:
    env = environment(XCODE)
    run(["xcodebuild", "-checkFirstLaunchStatus"], env)
    run(["xcrun", "-sdk", "macosx", "metal", "--version"], env)
    pin = json.loads(Path(__file__).with_name("upstream-pin.json").read_text())
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", pin["commit"], current], cwd=SOURCE)
    if ancestry.returncode:
        raise RuntimeError("Source HEAD is not based on the recorded upstream baseline")
    source = SOURCE
    if variant == "baseline":
        source = LAB / "baseline-source"
        if not source.exists():
            source.mkdir()
            archive = subprocess.check_output(["git", "archive", pin["commit"]], cwd=SOURCE)
            import io
            with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
                packed.extractall(source, filter="data")
        # Apply the identical recorder to both builds. Keep the upstream HUD
        # formatting and aggregation in the baseline so those are the only
        # performance differences being tested.
        for relative in ("src/dxmt/dxmt_frame_report.hpp", "src/dxmt/dxmt_command_queue.cpp",
                         "src/airconv/shaders/air_tessellation.metal"):
            shutil.copy2(SOURCE / relative, source / relative)
        shutil.copytree(SOURCE / "include/native/directx", source / "include/native/directx",
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        # git archive has no .git directory; keep the embedded version equal
        # to the experiment rather than Meson's v0.1 fallback.
        (source / "version.h.in").write_text((SOURCE / "version.h.in").read_text().replace("@VCS_TAG@", pin["public_source_ref"]))
        queue = (SOURCE / "src/dxmt/dxmt_command_queue.hpp").read_text()
        guard = "#ifdef DXMT_DEBUG\n    // Aggregates are only consumed by the debug HUD, not the frame report.\n    statistics.compute(frame_count);\n#endif"
        if queue.count(guard) != 1:
            raise RuntimeError("Baseline instrumentation needs review after source changes")
        original_queue = subprocess.check_output(["git", "show", pin["commit"] + ":src/dxmt/dxmt_command_queue.hpp"], cwd=SOURCE, text=True)
        original_queue = original_queue.replace('#include "dxmt_context.hpp"', '#include "dxmt_context.hpp"\n#include "dxmt_frame_report.hpp"')
        original_queue = original_queue.replace('  CaptureState capture_state;', '  CaptureState capture_state;\n  std::unique_ptr<FrameReport<dxmt::thread>> frame_report_;')
        start, end = "  void\n  PresentBoundary()", "  uint32_t GetMaxLatency()"
        boundary = queue[queue.index(start):queue.index(end)].replace(guard, "    statistics.compute(frame_count);")
        original_queue = original_queue[:original_queue.index(start)] + boundary + original_queue[original_queue.index(end):]
        (source / "src/dxmt/dxmt_command_queue.hpp").write_text(original_queue)
    compiler = CHAIN / "llvm-mingw-20231017-ucrt-macos-universal/bin"
    cross = LAB / f"cross-win64-{variant}.ini"
    binaries = {"c": "gcc", "cpp": "g++", "ar": "ar", "strip": "strip", "windres": "windres"}
    cross.write_text("[binaries]\n" + "\n".join(
        f"{key} = {meson_array([str(compiler / ('x86_64-w64-mingw32-' + value))])}" for key, value in binaries.items()
    ) + "\n[properties]\nneeds_exe_wrapper = true\n[host_machine]\nsystem = 'windows'\ncpu_family = 'x86_64'\ncpu = 'x86_64'\nendian = 'little'\n")
    native = LAB / f"native-x86_64-{variant}.ini"
    flags = ["-arch", "x86_64", "-mmacosx-version-min=14.0"]
    native.write_text("[binaries]\nc = 'clang'\ncpp = 'clang++'\n[built-in options]\n" + "\n".join(
        f"{key} = {meson_array(flags)}" for key in ("c_args", "cpp_args", "c_link_args", "cpp_link_args")
    ) + "\n")
    destination = LAB / f"build-{variant}"
    install = LAB / f"install-{variant}"
    llvm_option = "toolchains/llvm-darwin" if source == SOURCE else str(CHAIN / "llvm-darwin")
    if not (destination / "build.ninja").exists():
        run([
            str(TOOLS / "meson"), "setup", str(destination), str(source),
            "--cross-file", str(cross), "--native-file", str(native), "--buildtype=release",
            "--prefix", str(install), "-Dwine_builtin_dll=true", "-Ddxmt_debug=false",
            f"-Dnative_llvm_path={llvm_option}", f"-Dwine_install_path={CHAIN / 'wine'}",
        ], env)
    run([str(TOOLS / "meson"), "compile", "-C", str(destination), "-j", str(jobs)], env)
    run([str(TOOLS / "meson"), "install", "-C", str(destination), "--no-rebuild"], env)
    # Only record a completed build after install succeeds.
    (LAB / f"build-{variant}-manifest.json").write_text(json.dumps({
        "variant": variant, "source_commit": current, "upstream_baseline_commit": pin["commit"], "install": str(install),
        "dependency_manifest": str(LAB / "dependency-manifest.json"),
        "directx_headers_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE / "include/native/directx", text=True).strip(),
        "source_files_sha256": {
            relative: hashlib.sha256((source / relative).read_bytes()).hexdigest()
            for relative in ("src/dxmt/dxmt_frame_report.hpp", "src/dxmt/dxmt_command_queue.cpp",
                             "src/dxmt/dxmt_command_queue.hpp", "src/d3d11/d3d11_swapchain.cpp",
                             "src/airconv/shaders/air_tessellation.metal")
        },
        "baseline_includes_identical_recorder": True,
        "binary_sha256": {relative: hashlib.sha256((install / relative).read_bytes()).hexdigest()
                          for relative in ("x86_64-windows/d3d11.dll", "x86_64-windows/dxgi.dll",
                                           "x86_64-windows/winemetal.dll", "x86_64-unix/winemetal.so")},
        "runtime_tested": False,
    }, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dependencies", "llvm", "build"))
    parser.add_argument("--variant", choices=("baseline", "experiment"), default="experiment")
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 6:
        parser.error("Use 1 to 6 build jobs on this 16 GB Mac")
    if args.command == "dependencies": dependencies()
    elif args.command == "llvm": llvm(args.jobs)
    else: build(args.variant, args.jobs)


if __name__ == "__main__":
    main()
