#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <locale>
#include <thread>

namespace dxmt {

// CPU-side frame data only. Reading the encoder-owned fields of
// FrameStatistics here would introduce a data race.
struct FrameReportSample {
  uint64_t frame = 0;
  uint64_t boundary_interval_ns = 0;
  uint64_t command_queue_wait_ns = 0;
  uint64_t resource_sync_wait_ns = 0;
  uint64_t frame_latency_wait_ns = 0;
  uint32_t command_buffers = 0;
  uint32_t resource_syncs = 0;
  uint32_t event_stalls = 0;
};

// One producer (PresentBoundary), one consumer (the file writer). A full
// buffer drops diagnostics rather than delaying the game.
template <typename T, size_t Capacity> class FrameReportQueue {
  static_assert(Capacity > 0);
  std::array<T, Capacity> samples_{};
  alignas(64) std::atomic<uint64_t> write_{0};
  alignas(64) std::atomic<uint64_t> read_{0};

public:
  bool push(const T &sample) {
    const auto write = write_.load(std::memory_order_relaxed);
    if (write - read_.load(std::memory_order_acquire) == Capacity)
      return false;
    samples_[write % Capacity] = sample;
    write_.store(write + 1, std::memory_order_release);
    return true;
  }

  bool pop(T &sample) {
    const auto read = read_.load(std::memory_order_relaxed);
    if (read == write_.load(std::memory_order_acquire))
      return false;
    sample = samples_[read % Capacity];
    read_.store(read + 1, std::memory_order_release);
    return true;
  }
};

// Thread is dxmt::thread in the Wine build; the default permits native tests
// without Metal or Wine. The producer never allocates or writes to disk.
template <typename Thread = std::thread> class FrameReport {
  FrameReportQueue<FrameReportSample, 512> queue_;
  std::ofstream file_;
  std::atomic<bool> stopping_{false};
  std::atomic<uint64_t> wake_{0};
  std::atomic<uint64_t> dropped_{0};
  std::chrono::steady_clock::time_point previous_{};
  bool has_previous_ = false;
  bool enabled_ = false;
  Thread writer_;

  void run() {
    uint64_t rows = 0;
    for (;;) {
      const auto wake = wake_.load(std::memory_order_acquire);
      FrameReportSample sample;
      while (queue_.pop(sample)) {
        file_ << sample.frame << ',' << sample.boundary_interval_ns << ','
              << sample.command_queue_wait_ns << ',' << sample.resource_sync_wait_ns << ','
              << sample.frame_latency_wait_ns << ',' << sample.command_buffers << ','
              << sample.resource_syncs << ',' << sample.event_stalls << '\n';
        if (++rows % 256 == 0)
          file_.flush();
      }
      if (stopping_.load(std::memory_order_acquire)) {
        // stop() is called after the sole producer stops submitting samples.
        // Re-drain on the next iteration if shutdown raced with the last push.
        if (queue_.pop(sample)) {
          file_ << sample.frame << ',' << sample.boundary_interval_ns << ','
                << sample.command_queue_wait_ns << ',' << sample.resource_sync_wait_ns << ','
                << sample.frame_latency_wait_ns << ',' << sample.command_buffers << ','
                << sample.resource_syncs << ',' << sample.event_stalls << '\n';
          continue;
        }
        file_ << "# dropped_samples=" << dropped_.load(std::memory_order_relaxed) << '\n';
        file_.flush();
        return;
      }
      wake_.wait(wake, std::memory_order_acquire);
    }
  }

public:
  explicit FrameReport(const std::filesystem::path &path) : file_(path) {
    if (!file_)
      return;
    file_.imbue(std::locale::classic());
    file_ << "frame,boundary_interval_ns,command_queue_wait_ns,resource_sync_wait_ns,"
             "frame_latency_wait_ns,command_buffers,resource_syncs,event_stalls\n";
    writer_ = Thread([this]() { run(); });
    enabled_ = true;
  }

  FrameReport(const FrameReport &) = delete;
  FrameReport &operator=(const FrameReport &) = delete;

  ~FrameReport() { stop(); }

  bool enabled() const { return enabled_; }

  void submit(FrameReportSample sample) {
    if (!enabled_)
      return;
    const auto now = std::chrono::steady_clock::now();
    if (has_previous_) {
      sample.boundary_interval_ns =
          std::chrono::duration_cast<std::chrono::nanoseconds>(now - previous_).count();
    }
    previous_ = now;
    has_previous_ = true;
    if (!queue_.push(sample)) {
      dropped_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    wake_.fetch_add(1, std::memory_order_release);
    wake_.notify_one();
  }

  // The owner must stop submitting before calling stop().
  void stop() {
    if (!enabled_)
      return;
    stopping_.store(true, std::memory_order_release);
    wake_.fetch_add(1, std::memory_order_release);
    wake_.notify_one();
    writer_.join();
    enabled_ = false;
  }
};

} // namespace dxmt
