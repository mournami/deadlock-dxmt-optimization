#pragma once

#include "dxmt_frame_events.hpp"

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
  PresentPipelineBuildNS, LayerUpdateNS, DisplayChanges, PresentPipelineBuilds,
  ShaderWorkers, ShaderWorkersActive, ShaderJobsQueued, ShaderWorkerLimit,
  ShaderCreateCalls, ShaderCreateCacheHits, ShaderCreateMisses, ShaderCreateNS, ShaderBytecodeBytes, Count
};
constexpr size_t kFrameCounterCount = size_t(FrameCounter::Count);
inline constexpr const char *kFrameCounterCSV =
    "om_blend_calls,om_blend_redundant,om_depth_calls,om_depth_redundant,"
    "om_blend_commands,om_depth_commands,layer_query_ns,layer_wait_ns,"
    "present_pipeline_build_ns,layer_update_ns,display_changes,present_pipeline_builds,"
    "shader_workers,shader_workers_active,shader_jobs_queued,shader_worker_limit,"
    "shader_create_calls,shader_create_cache_hits,shader_create_misses,shader_create_ns,shader_bytecode_bytes";

// Atomics aggregate immediate/deferred recording calls between CPU boundaries.
// These are recording intervals, not GPU execution counts for the same frame.
class FrameCounters {
  std::array<std::atomic<uint64_t>, kFrameCounterCount> values_{};
public:
  void add(FrameCounter counter, uint64_t value = 1) {
    values_[size_t(counter)].fetch_add(value, std::memory_order_relaxed);
  }
  void set(FrameCounter counter, uint64_t value) {
    values_[size_t(counter)].store(value, std::memory_order_relaxed);
  }
  std::array<uint64_t, kFrameCounterCount> take() {
    std::array<uint64_t, kFrameCounterCount> result;
    for (size_t i = 0; i < result.size(); ++i)
      result[i] = values_[i].exchange(0, std::memory_order_relaxed);
    return result;
  }
};

struct GPUReportSample {
  uint64_t frame = 0, chunk = 0;
  uint64_t gpu_start_ns = 0, gpu_end_ns = 0;
  uint64_t kernel_start_ns = 0, kernel_end_ns = 0;
  uint64_t status = 0, allocated_bytes = 0, memory_sampled = 0;
};

struct EncoderReportSample {
  uint64_t frame = 0;
  uint64_t next_drawable_ns = 0;
  uint64_t present_encode_ns = 0;
  uint64_t pipeline_wait_ns = 0;
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
  FrameReportQueue<GPUReportSample, 512> gpu_queue_;
  std::ofstream file_;
  std::ofstream encoder_file_;
  std::ofstream event_file_;
  std::ofstream gpu_file_;
  std::atomic<bool> stopping_{false};
  FrameEventRecorder events_;
  std::atomic<uint64_t> dropped_{0};
  std::atomic<uint64_t> encoder_dropped_{0};
  std::atomic<uint64_t> gpu_dropped_{0};
  std::chrono::steady_clock::time_point previous_{};
  bool has_previous_ = false;
  bool enabled_ = false;
  bool encoder_enabled_ = false;
  ReportMode mode_;
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
      ++rows;
    }
    EncoderReportSample encoder;
    for (size_t i = 0; i < 512 && encoder_queue_.pop(encoder); ++i) {
      encoder_file_ << encoder.frame << ',' << encoder.next_drawable_ns << ',' << encoder.present_encode_ns << ',' << encoder.pipeline_wait_ns << '\n';
      ++rows;
    }
    FrameEventSample event;
    for (size_t i = 0; i < 512 && events_.pop(event); ++i) {
      event_file_ << kFrameEventNames[size_t(event.event)] << ',' << event.frame << ','
                  << event.start_ns << ',' << event.duration_ns << ','
                  << event.thread_id << ',' << event.object_id << ',' << event.detail << '\n';
      ++rows;
    }
    GPUReportSample gpu;
    for (size_t i = 0; i < 512 && gpu_queue_.pop(gpu); ++i) {
      gpu_file_ << gpu.frame << ',' << gpu.chunk << ',' << gpu.gpu_start_ns << ',' << gpu.gpu_end_ns << ','
                << gpu.kernel_start_ns << ',' << gpu.kernel_end_ns << ',' << gpu.status << ','
                << gpu.allocated_bytes << ',' << gpu.memory_sampled << '\n';
      ++rows;
    }
    // Timed batch flush keeps a forcibly stopped capture's tail bounded.
    file_.flush();
    if (encoder_file_) encoder_file_.flush();
    if (event_file_) event_file_.flush();
    if (gpu_file_) gpu_file_.flush();
  }

  void run() {
    uint64_t rows = 0;
    for (;;) {
      drain(rows);
      if (stopping_.load(std::memory_order_acquire)) {
        // All producers have stopped before stop(). Keep their queues separate
        // so the CPU and encoder never race for an SPSC producer slot.
        drain(rows);
        file_ << "# dropped_samples=" << dropped_.load(std::memory_order_relaxed) << '\n';
        file_.flush();
        if (encoder_file_) {
          encoder_file_ << "# dropped_samples=" << encoder_dropped_.load(std::memory_order_relaxed) << '\n';
          encoder_file_.flush();
        }
        if (event_file_) {
          event_file_ << "# dropped_samples=" << events_.dropped() << '\n';
          event_file_.flush();
        }
        if (gpu_file_) {
          gpu_file_ << "# dropped_samples=" << gpu_dropped_.load(std::memory_order_relaxed) << '\n';
          gpu_file_.flush();
        }
        return;
      }
      // Producers never wake this thread. At most 10 normal drains per second;
      // a slow disk/contended writer loses telemetry, not rendering work.
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }

public:
  explicit FrameReport(const std::filesystem::path &path, ReportMode mode = ReportMode::Full)
      : events_(mode), mode_(mode) {
    if (mode == ReportMode::Off) return;
    file_.open(path);
    if (!file_)
      return;
    file_.imbue(std::locale::classic());
    file_ << "frame,boundary_interval_ns,command_queue_wait_ns,resource_sync_wait_ns,"
             "frame_latency_wait_ns,command_buffers,resource_syncs,event_stalls," << kFrameCounterCSV << '\n';
    file_ << "# report_mode=" << (full() ? "full" : "light") << '\n';
    encoder_file_.open(path.parent_path() / (path.stem().string() + ".encoder.csv"));
    if (encoder_file_) {
      encoder_file_.imbue(std::locale::classic());
      encoder_file_ << "frame,next_drawable_ns,present_encode_ns,pipeline_wait_ns\n";
      encoder_enabled_ = true;
    }
    event_file_.open(path.parent_path() / (path.stem().string() + ".events.csv"));
    if (event_file_) {
      event_file_.imbue(std::locale::classic());
      event_file_ << "event,frame,start_ns,duration_ns,thread_id,object_id,detail\n";
    }
    gpu_file_.open(path.parent_path() / (path.stem().string() + ".gpu.csv"));
    if (gpu_file_) {
      gpu_file_.imbue(std::locale::classic());
      gpu_file_ << "frame,chunk,gpu_start_ns,gpu_end_ns,kernel_start_ns,kernel_end_ns,status,allocated_bytes,memory_sampled\n";
    }
    writer_ = Thread([this]() { run(); });
    enabled_ = true;
  }

  FrameReport(const FrameReport &) = delete;
  FrameReport &operator=(const FrameReport &) = delete;

  ~FrameReport() { stop(); }

  bool enabled() const { return enabled_; }
  bool full() const { return mode_ == ReportMode::Full; }
  template <typename Priority> void setWriterPriority(Priority priority) { writer_.set_priority(priority); }
  FrameEventRecorder *eventRecorder() { return event_file_.is_open() ? &events_ : nullptr; }

  // Called only by the encoding thread. No file IO or allocation here.
  void submitEncoder(EncoderReportSample sample) {
    if (!enabled_ || !encoder_enabled_) return;
    if (!encoder_queue_.push(sample)) {
      encoder_dropped_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
  }

  // Completion-thread producer; the command buffer has already completed.
  void submitGPU(GPUReportSample sample) {
    if (!enabled_ || !gpu_file_.is_open()) return;
    if (!gpu_queue_.push(sample)) gpu_dropped_.fetch_add(1, std::memory_order_relaxed);
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
  }

  // The owner must stop submitting before calling stop().
  void stop() {
    if (!enabled_)
      return;
    stopping_.store(true, std::memory_order_release);
    writer_.join();
    enabled_ = false;
  }
};

} // namespace dxmt
