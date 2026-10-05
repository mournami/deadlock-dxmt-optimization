# Deadlock DXMT optimization

This public fork starts from upstream DXMT v0.72 (`3d8d2aa15564401cbc978014950b14aa87c686c0`). Preserve upstream history, copyright notices and the MIT license.

Use `main` as the primary branch. The owner requested no README yet. Keep changes small, measurable and correct for D3D11 workloads; do not silently drop synchronization or add executable-name hacks.

The owner authorized committing and pushing useful validated project changes to this public repository. Commit coherent changes with relevant validation; never publish private bottles, registry/account data, CrossOver runtime files, credentials, personal conversation notes or raw gameplay telemetry. `.gitignore` excludes generated assets and the private lab.

Current optimizations: remove release-only costs of disabled debug HUD formatting/aggregation/encoding timers, and reject full repeats of OM blend/depth-stencil bindings. Optional asynchronous CPU/encoder CSV records boundary cadence, presentation waits and binding counts, not displayed FPS or input latency. User feedback is encouraging, but a controlled FPS gain has not been established and support across Apple Silicon models has not been validated.

Native reporter tests: compile `tests/frame_report_test.cpp` with C++20, threads and `-I src/dxmt`; pass a temporary output directory. Python tooling tests: `python3 -m unittest discover -s tests -p 'test_tools.py'`. Staging fixture tests require an installed CrossOver library; other tests are independent of Steam/game execution.

`tools/macos` contains source versions of the prepared-lab helpers. They default to a private `.dxmt-lab` inside this checkout. `DXMT_LAB_DIR` can select an already-prepared isolated lab, `DXMT_STEAM_BOTTLE` selects the source bottle name, and `DXMT_GAME_EXE` selects the installed game executable. These scripts require prepared local runtime/toolchain data; they do not constitute a one-click installer for a fresh machine. Keep public helper changes in sync with any local machine adapters and their tests.

The canonical launch path is now `dxmt-crossover.py` → `dxmt-test.py` with a validated ready pointer. New builds should use `dxmt-build.py build --variant <baseline|experiment> --publish` to run private DLL/device/OM-state readback checks before updating the menu. Preserve older ready snapshots on failure. The service-only CrossOver DXMT bottle holds raw menu entries; the real game stays in the isolated Steam test prefix. Use its custom stop entry, not the control bottle's ordinary Quit All Applications action. `DXMT_DEVELOPER_DIR` supports Xcode at another location. See `docs/deadlock-experiment.md` for counter semantics, the rollback switch and benchmark limits.

Measure presentation/layer waits and PSO rebuilds, redundant state invalidation, dynamic uploads and encoder transitions before choosing the next patch. Publish encoder-owned/GPU measurements through a safe completion channel instead of racing reads from the CPU PresentBoundary thread. Keep diagnostic overhead and enablement consistent between comparison builds.

The full upstream build workflow remains manually dispatched until its toolchain is maintained. The fast test workflow validates standalone telemetry/tooling components, not game rendering or FPS.
