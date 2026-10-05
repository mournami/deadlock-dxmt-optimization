#include "dxmt_frame_report.hpp"
#include <algorithm>
#include <iostream>
#include <memory>
#include <vector>
#include <sys/resource.h>

// Controlled recorder-hook benchmark, NOT a renderer/FPS benchmark.
// Compile with -DLEGACY_RECORDER against adffbc8's two reporter headers to
// compare the same synthetic hooks. No Wine/Metal/Win32 property calls here.
int main(int argc, char **argv) {
  if (argc != 4) return 1;
  const std::filesystem::path root(argv[1]);
  const std::string mode(argv[2]);
  const auto frames = std::stoull(argv[3]);
  if (frames < 100 || frames > 100000) return 1;
  using namespace dxmt;
  std::unique_ptr<FrameReport<>> report;
  if (mode != "off") {
#ifdef LEGACY_RECORDER
    report = std::make_unique<FrameReport<>>(root / "bench.csv");
#else
    report = std::make_unique<FrameReport<>>(root / "bench.csv", reportMode(mode));
#endif
  }
  auto events = report ? report->eventRecorder() : nullptr;
  const bool full = mode == "full";
  FrameCounters counters;
  std::vector<uint64_t> costs;
  costs.reserve(frames);
  rusage before{}, after{};
  getrusage(RUSAGE_SELF, &before);
  const auto epoch = std::chrono::steady_clock::now();
  for (uint64_t frame = 0; frame < frames; ++frame) {
    const auto start = frameEventTimeNS();
    {
      FrameEventScope present(events, FrameEvent::Present, frame, 1, 0, 1);
      if (full) for (unsigned i = 0; i < 150; ++i) counters.add(FrameCounter::OMBlendCalls);
      for (auto kind : {FrameEvent::PresentMutex, FrameEvent::PrepareFlush, FrameEvent::SyncFrame,
                        FrameEvent::Commit, FrameEvent::PresentBoundary}) {
        FrameEventScope phase(events, kind, frame, 1, 0, 1);
      }
      if (full || frame % 23 == 0) {
        FrameEventScope window(events, FrameEvent::WindowState, frame, 1, 5, 1);
      }
      if (report) {
        FrameReportSample sample;
        sample.frame = frame;
        sample.cpu = counters.take();
        report->submit(sample);
        report->submitEncoder({frame, 0, 0, 0});
#ifndef LEGACY_RECORDER
        report->submitGPU({frame, frame + 1, (frame + 1) * 1000000, (frame + 2) * 1000000,
                           0, 0, 4, 32000000, uint64_t(frame % 60 == 0)});
#endif
      }
    }
    costs.push_back(frameEventTimeNS() - start);
    std::this_thread::sleep_until(epoch + std::chrono::nanoseconds((frame + 1) * 1000000000 / 90));
  }
  getrusage(RUSAGE_SELF, &after);
  report.reset();
  std::sort(costs.begin(), costs.end());
  auto cpu = [](const rusage &value) {
    return value.ru_utime.tv_sec + value.ru_stime.tv_sec +
        (value.ru_utime.tv_usec + value.ru_stime.tv_usec) / 1e6;
  };
  std::cout << "{\"mode\":\"" << mode << "\",\"frames\":" << frames
            << ",\"median_hook_us\":" << costs[costs.size() / 2] / 1000.0
            << ",\"p95_hook_us\":" << costs[size_t(costs.size() * .95)] / 1000.0
            << ",\"process_cpu_seconds\":" << cpu(after) - cpu(before)
            << ",\"scope\":\"Synthetic producer hooks plus background CSV at 90Hz; excludes Wine/Metal/window APIs and game FPS\"}\n";
}
