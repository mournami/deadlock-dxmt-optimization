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

enum class FrameCounter : size_t {
  OMBlendCalls, OMBlendRedundant, OMDepthCalls, OMDepthRedundant,
  OMBlendCommands, OMDepthCommands, LayerQueryNS, LayerWaitNS,
  PresentPipelineBuildNS, LayerUpdateNS, DisplayChanges, PresentPipelineBuilds, Count
};
constexpr size_t kFrameCounterCount = size_t(FrameCounter::Count);
inline constexpr const char *kFrameCounterCSV =
    "om_blend_calls,om_blend_redundant,om_depth_calls,om_depth_redundant,"
    "om_blend_commands,om_depth_commands,layer_query_ns,layer_wait_ns,"
    "present_pipeline_build_ns,layer_update_ns,display_changes,present_pipeline_builds";

// Atomics aggregate immediate/deferred recording calls between CPU boundaries.
// These are recording intervals, not GPU execution counts for the same frame.
class FrameCounters {
  std::array<std::atomic<uint64_t>, kFrameCounterCount> values_{};
public:
  void add(FrameCounter counter, uint64_t value = 1) {
    values_[size_t(counter)].fetch_add(value, std::memory_order_relaxed);
  }
  std::array<uint64_t, kFrameCounterCount> take() {
    std::array<uint64_t, kFrameCounterCount> result;
    for (size_t i = 0; i < result.size(); ++i)
      result[i] = values_[i].exchange(0, std::memory_order_relaxed);
    return result;
  }
};

struct EncoderReportSample {
  uint64_t frame = 0;
  uint64_t next_drawable_ns = 0;
  uint64_t present_encode_ns = 0;
};

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
  std::array<uint64_t, kFrameCounterCount> cpu{};
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
  FrameReportQueue<EncoderReportSample, 512> encoder_queue_;
  std::ofstream file_;
  std::ofstream encoder_file_;
  std::atomic<bool> stopping_{false};
  std::atomic<uint64_t> wake_{0};
  std::atomic<uint64_t> dropped_{0};
  std::atomic<uint64_t> encoder_dropped_{0};
  std::chrono::steady_clock::time_point previous_{};
  bool has_previous_ = false;
  bool enabled_ = false;
  bool encoder_enabled_ = false;
  Thread writer_;

  void write(const FrameReportSample &sample) {
    file_ << sample.frame << ',' << sample.boundary_interval_ns << ','
          << sample.command_queue_wait_ns << ',' << sample.resource_sync_wait_ns << ','
          << sample.frame_latency_wait_ns << ',' << sample.command_buffers << ','
          << sample.resource_syncs << ',' << sample.event_stalls;
    for (auto value : sample.cpu) file_ << ',' << value;
    file_ << '\n';
  }

  void drain(uint64_t &rows) {
    FrameReportSample sample;
    for (size_t i = 0; i < 512 && queue_.pop(sample); ++i) {
      write(sample);
      if (++rows % 256 == 0) file_.flush();
    }
    EncoderReportSample encoder;
    for (size_t i = 0; i < 512 && encoder_queue_.pop(encoder); ++i) {
      encoder_file_ << encoder.frame << ',' << encoder.next_drawable_ns << ',' << encoder.present_encode_ns << '\n';
      if (++rows % 256 == 0) encoder_file_.flush();
    }
  }

  void run() {
    uint64_t rows = 0;
    for (;;) {
      const auto wake = wake_.load(std::memory_order_acquire);
      drain(rows);
      if (stopping_.load(std::memory_order_acquire)) {
        // Both producers have stopped before stop(). Keep their queues separate
        // so the CPU and encoder never race for an SPSC producer slot.
        drain(rows);
        file_ << "# dropped_samples=" << dropped_.load(std::memory_order_relaxed) << '\n';
        file_.flush();
        if (encoder_file_) {
          encoder_file_ << "# dropped_samples=" << encoder_dropped_.load(std::memory_order_relaxed) << '\n';
          encoder_file_.flush();
        }
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
             "frame_latency_wait_ns,command_buffers,resource_syncs,event_stalls," << kFrameCounterCSV << '\n';
    encoder_file_.open(path.parent_path() / (path.stem().string() + ".encoder.csv"));
    if (encoder_file_) {
      encoder_file_.imbue(std::locale::classic());
      encoder_file_ << "frame,next_drawable_ns,present_encode_ns\n";
      encoder_enabled_ = true;
    }
    writer_ = Thread([this]() { run(); });
    enabled_ = true;
  }

  FrameReport(const FrameReport &) = delete;
  FrameReport &operator=(const FrameReport &) = delete;

  ~FrameReport() { stop(); }

  bool enabled() const { return enabled_; }

  // Called only by the encoding thread. No file IO or allocation here.
  void submitEncoder(EncoderReportSample sample) {
    if (!enabled_ || !encoder_enabled_) return;
    if (!encoder_queue_.push(sample)) {
      encoder_dropped_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    wake_.fetch_add(1, std::memory_order_release);
    wake_.notify_one();
  }

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
