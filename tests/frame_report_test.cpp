#include "dxmt_frame_report.hpp"
#include <cassert>
#include <fstream>
#include <iostream>
#include <string>
#include <unordered_set>

using namespace dxmt;

int main(int argc, char **argv) {
  assert(argc == 2);
  FrameReportQueue<uint64_t, 3> tiny;
  uint64_t value = 0;
  assert(!tiny.pop(value));
  for (uint64_t i = 0; i < 3; ++i)
    assert(tiny.push(i));
  assert(!tiny.push(99));
  assert(tiny.pop(value) && value == 0);
  assert(tiny.push(3));
  for (uint64_t i = 1; i <= 3; ++i)
    assert(tiny.pop(value) && value == i);
  assert(!tiny.pop(value));

  // Stress wrap-around and publication ordering with independent threads.
  FrameReportQueue<uint64_t, 127> queue;
  constexpr uint64_t count = 300000;
  std::thread producer([&]() {
    for (uint64_t i = 0; i < count; ++i)
      while (!queue.push(i)) std::this_thread::yield();
  });
  for (uint64_t i = 0; i < count; ++i) {
    while (!queue.pop(value)) std::this_thread::yield();
    assert(value == i);
  }
  producer.join();

  FrameEventQueue<2> event_tiny;
  FrameEventSample event;
  assert(!event_tiny.pop(event));
  assert(event_tiny.push({FrameEvent::SyncFrame, 1}));
  assert(event_tiny.push({FrameEvent::Present, 2}));
  assert(!event_tiny.push({FrameEvent::Present, 3}));
  assert(event_tiny.pop(event) && event.frame == 1);
  assert(event_tiny.push({FrameEvent::Present, 3}));
  assert(event_tiny.pop(event) && event.frame == 2);
  assert(event_tiny.pop(event) && event.frame == 3);
  assert(!event_tiny.pop(event));

  // Concurrent callers may drop on contention, but accepted records must be
  // intact, unique and FIFO per producer. No caller waits for queue space.
  FrameEventRecorder events;
  std::atomic<unsigned> finished{0};
  std::array<std::thread, 4> callers;
  constexpr uint64_t events_per_caller = 40000;
  for (size_t caller = 0; caller < callers.size(); ++caller)
    callers[caller] = std::thread([&, caller] {
      for (uint64_t i = 0; i < events_per_caller; ++i)
        events.submit({FrameEvent::WaitCPUFence, i, i + 100, i + 200, caller, caller + 10, i});
      finished.fetch_add(1, std::memory_order_release);
    });
  std::unordered_set<uint64_t> unique;
  std::array<uint64_t, 4> last{};
  std::array<bool, 4> seen{};
  auto check_event = [&](const FrameEventSample &sample) {
    assert(sample.event == FrameEvent::WaitCPUFence && sample.thread_id < callers.size());
    assert(sample.start_ns == sample.frame + 100 && sample.duration_ns == sample.frame + 200);
    assert(sample.object_id == sample.thread_id + 10 && sample.detail == sample.frame);
    assert(unique.insert((sample.thread_id << 32) | sample.frame).second);
    if (seen[sample.thread_id]) assert(sample.frame > last[sample.thread_id]);
    seen[sample.thread_id] = true;
    last[sample.thread_id] = sample.frame;
  };
  while (finished.load(std::memory_order_acquire) != callers.size()) {
    if (events.pop(event)) check_event(event);
    else std::this_thread::yield();
  }
  for (auto &caller : callers) caller.join();
  while (events.pop(event)) check_event(event);
  assert(unique.size() + events.dropped() == callers.size() * events_per_caller);
  {
    FrameEventScope disabled_scope(nullptr, FrameEvent::Present, 7);
    FrameEventScope scope(&events, FrameEvent::Present, 8, 9, 10, 11);
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
    scope.finish();
    scope.finish();
  }
  assert(events.pop(event) && event.event == FrameEvent::Present && event.frame == 8);
  assert(event.duration_ns >= 1000000 && event.thread_id == 9 && event.detail == 10 && event.object_id == 11);
  assert(!events.pop(event));

  const std::filesystem::path root(argv[1]);
  const auto csv = root / "frame-report-test.csv";
  {
    FrameReport<> report(csv);
    assert(report.enabled());
    FrameReportSample sample;
    sample.command_buffers = 4;
    sample.command_queue_wait_ns = 500000;
    sample.resource_sync_wait_ns = 100000;
    sample.frame_latency_wait_ns = 200000;
    sample.frame = 0;
    report.eventRecorder()->submit({FrameEvent::Present, 0, 100, 200, 3, 4, 5});
    sample.cpu[size_t(FrameCounter::OMBlendCalls)] = 7;
    report.submit(sample);
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
    sample.frame = 1;
    report.submit(sample);
    report.stop();
    report.stop();
  }
  std::ifstream file(csv);
  std::string header, first, second, footer;
  std::getline(file, header);
  std::getline(file, first);
  std::getline(file, second);
  std::getline(file, footer);
  assert(header.starts_with("frame,boundary_interval_ns,"));
  assert(first == "0,0,500000,100000,200000,4,0,0,7,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0");
  assert(second.starts_with("1,"));
  assert(std::stoull(second.substr(2)) >= 1000000);
  assert(footer == "# dropped_samples=0");
  std::ifstream event_file(root / "frame-report-test.events.csv");
  std::getline(event_file, header);
  std::getline(event_file, first);
  std::getline(event_file, footer);
  assert(header == "event,frame,start_ns,duration_ns,thread_id,object_id,detail");
  assert(first == "present,0,100,200,3,4,5");
  assert(footer == "# dropped_samples=0");

  // An unwritable destination disables telemetry instead of affecting frames.
  FrameReport<> disabled(root / "missing" / "report.csv");
  assert(!disabled.enabled());
  disabled.submit({});
  disabled.stop();

  // A saturated diagnostic queue must remain bounded and terminate cleanly.
  const auto stress = root / "frame-report-stress.csv";
  {
    FrameReport<> report(stress);
    std::thread encoder([&]() {
      for (uint64_t i = 0; i < 100000; ++i)
        report.submitEncoder({i, i + 10, i + 20, i + 30});
    });
    std::thread event_caller([&]() {
      for (uint64_t i = 0; i < 100000; ++i)
        report.eventRecorder()->submit({FrameEvent::Present, i, i + 100, i + 200, 1, 2, i});
    });
    for (uint64_t i = 0; i < 100000; ++i) {
      FrameReportSample sample;
      sample.frame = i;
      report.submit(sample);
    }
    encoder.join();
    event_caller.join();
  }
  std::ifstream stress_file(stress);
  std::string line;
  uint64_t previous = 0, rows = 0, dropped = 0;
  while (std::getline(stress_file, line)) {
    if (line.starts_with("# dropped_samples="))
      dropped = std::stoull(line.substr(18));
    else if (!line.starts_with("frame,")) {
      const auto frame = std::stoull(line);
      if (rows) assert(frame > previous);
      previous = frame;
      ++rows;
    }
  }
  assert(rows + dropped == 100000);
  std::ifstream stress_events(root / "frame-report-stress.events.csv");
  previous = rows = dropped = 0;
  while (std::getline(stress_events, line)) {
    if (line.starts_with("# dropped_samples=")) dropped = std::stoull(line.substr(18));
    else if (!line.starts_with("event,")) {
      const auto frame = std::stoull(line.substr(line.find(',') + 1));
      if (rows) assert(frame > previous);
      assert(line == "present," + std::to_string(frame) + "," + std::to_string(frame + 100) + "," +
                     std::to_string(frame + 200) + ",1,2," + std::to_string(frame));
      previous = frame;
      ++rows;
    }
  }
  assert(rows + dropped == 100000);
  std::ifstream encoder_file(root / "frame-report-stress.encoder.csv");
  previous = rows = dropped = 0;
  while (std::getline(encoder_file, line)) {
    if (line.starts_with("# dropped_samples=")) dropped = std::stoull(line.substr(18));
    else if (!line.starts_with("frame,")) {
      const auto frame = std::stoull(line);
      if (rows) assert(frame > previous);
      assert(line == std::to_string(frame) + "," + std::to_string(frame + 10) + "," + std::to_string(frame + 20) + "," + std::to_string(frame + 30));
      previous = frame;
      ++rows;
    }
  }
  assert(rows + dropped == 100000);

  FrameCounters counters;
  std::thread a([&]() { for (size_t i = 0; i < 100000; ++i) counters.add(FrameCounter::OMBlendCalls); });
  std::thread b([&]() { for (size_t i = 0; i < 100000; ++i) counters.add(FrameCounter::OMBlendCalls); });
  uint64_t total = 0;
  for (size_t i = 0; i < 1000; ++i) total += counters.take()[size_t(FrameCounter::OMBlendCalls)];
  a.join(); b.join();
  total += counters.take()[size_t(FrameCounter::OMBlendCalls)];
  assert(total == 200000);
  std::cout << "CPU/encoder/event queues, CSV drain, overflow, concurrent counters/events: passed\n";
}
