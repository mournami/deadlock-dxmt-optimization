# CrossOver test entries and the OM-state experiment

The macOS tools require the existing isolated lab, runtime and toolchains. They are not a fresh-machine installer. They never replace the installed CrossOver runtime. Keep Steam account data, raw reports and runtime binaries local.

## CrossOver launch entries

`tools/macos/dxmt-crossover.py install` creates a separate empty `DXMT` bottle with native raw launch entries. They delegate to the prepared private Steam test bottle through the same safety checks and OS lock as the command-line launcher. The control bottle does not contain another Steam installation.

The entries are baseline, experiment, stop test, check builds and reports. Use **stop test** or exit the private Windows Steam before switching variants. CrossOver's ordinary Quit All Applications action on the empty control bottle does not own the private test session.

For every new local build, run the build helper with `--publish`. It checks DLL routing, creates a real D3D11/Metal device, reads back GPU pixels and runs the offscreen OM-state test. Only then does it atomically replace the corresponding ready pointer. The menu reads that pointer on every launch. Failed validation leaves the previous ready build available. Snapshots and their hashes are checked again at launch.

```sh
export DXMT_LAB_DIR=/path/to/prepared/lab
export DXMT_GAME_EXE=/path/to/Deadlock/game/bin/win64/deadlock.exe
# Optional when Xcode is installed elsewhere:
export DXMT_DEVELOPER_DIR=/path/to/Xcode.app/Contents/Developer
python3 tools/macos/dxmt-build.py build --variant baseline --publish
python3 tools/macos/dxmt-build.py build --variant experiment --publish
python3 tools/macos/dxmt-crossover.py install
```

Compile `tests/device_probe.c` and `tests/om_state_probe.c` with the prepared x86_64 LLVM-MinGW compiler as `device-probe.exe` and `om-state-probe.exe` in the lab. The menu uses the last validated **local** build; it does not silently merge upstream, download arbitrary binaries or run scheduled game sessions.

## Exact change

`OMSetBlendState` used to set `BlendFactorAndStencilRef` dirty even when the bound object, four factor bits and sample mask were identical. `OMSetDepthStencilState` used to set depth/stencil dirty when both the object and reference were identical. A subsequent draw could therefore record the same render-state command again.

Experiment returns early only for a full repeat. It preserves existing dirty flags, reference ownership, changed values and the state resets required by a new encoder, ClearState or command list. It does not remove a fence, wait or rendering command with a changed state. There is no executable-name check.

Factors use bitwise equality so signed zero and NaN payloads exposed through GetBlendState are preserved. NULL factors still select the documented defaults; NULL state objects still select the default states. See [OMSetBlendState](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-omsetblendstate) and [OMSetDepthStencilState](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-omsetdepthstencilstate).

`DXMT_OM_STATE_DEDUP=0` disables the change, `=1` enables it. The default is on in experiment and off in the instrumented baseline. The launch entries select the matching setting explicitly.

Expected effect is less CPU recording/translation work if repeated bindings are frequent. A GPU frame-time or FPS improvement has not been measured. The frequency in Deadlock remains to be established. This does not establish a fix for input slowdown or focus freezes.

## Diagnostics

`DXMT_FRAME_REPORT_DIR` enables the optional reporter in both variants. Without it there is no reporter thread, file IO, counter updates or extra presentation timing clock reads.

CPU CSV now adds OM call/repeat/recorded-command counts, display-setting changes and timings for display query, the existing layer fence wait, two presentation PSO creation attempts and layer updates. These counters aggregate immediate/deferred recording calls between CPU boundaries; they are not GPU execution counts for exactly that frame.

The companion `*.encoder.csv` records frame ID, time inside `nextDrawable` and the complete presentation encode call. These are encoding-thread wall times, not GPU completion times, display FPS or input latency. CPU and encoder use separate bounded SPSC queues feeding one writer; full queues drop diagnostics instead of waiting for disk. The owner joins both producers before reporter shutdown. The analysis helper supports old/new CPU schemas and joins encoder measurements by frame ID.

The analyzer also lists the ten longest CPU intervals with their CPU timings and any encoder samples bearing the same frame ID. Those two kinds of timing are displayed separately; they cannot safely be added as a causal breakdown. It does not infer an Alt+Tab event or classify loading as gameplay. Recorded elapsed time omits any lost intervals. For a forcibly stopped capture, `--allow-incomplete` discards an unterminated final line and reports that omission; malformed complete rows still fail validation.

## Validation and comparison

The offscreen GPU test verifies repeated state followed by draws, changed blend factors, sample masks and stencil references, NULL/default objects, exact state getter bits, new encoders after readback, ClearState and deferred command-list execution with both restore modes. It compares actual output pixels, not only successful API return codes. This is a correctness check, not a game performance benchmark.

Use identical graphics settings, resolution, scaling, FPS cap, Wine configuration and mods. Warm the same scene and record an identical 60–90 second route. Keep Explore New York and online gameplay as separate workloads. Note focus transitions and input slowdown times. Exclude loading/menu sections. Compare repeated runs rather than different scenes. Also compare with the reporter disabled before attributing a small gain to the optimization; recording counters itself has a cost.

## Next investigations

These are candidates, not measured top bottlenecks:

| Path | Evidence needed | Potential impact | Change risk |
| --- | --- | --- | --- |
| Presentation layer fence/PSO rebuild | Correlated layer invalidation and wait/build spikes | High during focus freezes | High if synchronization changes |
| nextDrawable | Encoding-thread waits at matched frame IDs | High during GPU backpressure | High if pacing changes |
| Repeated OM states | Repetition rate and reduction in recorded commands | CPU dependent | Low for exact repeat rejection |
| Graphics PSO cache/compilation | Miss/variant rate and time waiting for compiled PSOs | High during compilation stalls | Medium |
| Dynamic uploads/pass breaks | Map flags/bytes, allocation and encoder termination reasons | Workload dependent | Medium/high |

Do not remove the presentation fence or increase buffered frames to hide an input symptom. Evaluate effective presentation PSO keys if display versions repeatedly change without equivalent pipeline changes. Existing ring allocators and PSO caches should be investigated before adding replacements.
