#pragma once

#include "dxmt_report_policy.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>

namespace dxmt {

enum class FrameEvent : uint32_t {
  Present, PresentMutex, PrepareFlush, Commit, PresentBoundary, SyncFrame,
  WindowState, ResizeBuffers, ResizeTarget, Fullscreen, ApplyLayer,
  WaitGPUIdle, WaitCPUFence, Count
};
inline constexpr std::array<const char *, size_t(FrameEvent::Count)> kFrameEventNames = {
  "present", "present_mutex", "prepare_flush", "commit", "present_boundary", "sync_frame",
  "window_state", "resize_buffers", "resize_target", "fullscreen", "apply_layer",
  "wait_gpu_idle", "wait_cpu_fence"
};

struct FrameEventSample {
  FrameEvent event = FrameEvent::Present;
  uint64_t frame = 0;
  uint64_t start_ns = 0;
  uint64_t duration_ns = 0;
  uint64_t thread_id = 0;
  uint64_t object_id = 0;
  uint64_t detail = 0;
};

// Multiple callers may resize/wait while another thread presents. A single
// try-lock protects the fixed queue; contention/full capacity drops telemetry.
// Neither producer nor consumer spins, waits, allocates or touches the disk.
template <size_t Capacity> class FrameEventQueue {
  static_assert(Capacity > 0);
  std::array<FrameEventSample, Capacity> samples_{};
  std::atomic_flag busy_ = ATOMIC_FLAG_INIT;
  size_t read_ = 0, write_ = 0, count_ = 0;
public:
  bool push(const FrameEventSample &sample) {
    if (busy_.test_and_set(std::memory_order_acquire)) return false;
    const bool available = count_ < Capacity;
    if (available) {
      samples_[write_] = sample;
      write_ = (write_ + 1) % Capacity;
      ++count_;
    }
    busy_.clear(std::memory_order_release);
    return available;
  }
  bool pop(FrameEventSample &sample) {
    if (busy_.test_and_set(std::memory_order_acquire)) return false;
    const bool available = count_ != 0;
    if (available) {
      sample = samples_[read_];
      read_ = (read_ + 1) % Capacity;
      --count_;
    }
    busy_.clear(std::memory_order_release);
    return available;
  }
};

class FrameEventRecorder {
  FrameEventQueue<512> queue_;
  std::atomic<uint64_t> dropped_{0};
  ReportMode mode_;
public:
  explicit FrameEventRecorder(ReportMode mode = ReportMode::Full) : mode_(mode) {}
  bool allows(FrameEvent event) const {
    if (mode_ == ReportMode::Off) return false;
    if (mode_ == ReportMode::Full) return true;
    switch (event) {
    case FrameEvent::PresentMutex: case FrameEvent::PrepareFlush:
    case FrameEvent::Commit: case FrameEvent::PresentBoundary: case FrameEvent::SyncFrame:
      return false;
    default: return true;
    }
  }
  void submit(const FrameEventSample &sample) {
    if (!queue_.push(sample)) {
      dropped_.fetch_add(1, std::memory_order_relaxed);
    }
    // The writer polls in batches. No producer notification or OS wakeup.
  }
  bool pop(FrameEventSample &sample) { return queue_.pop(sample); }
  uint64_t dropped() const { return dropped_.load(std::memory_order_relaxed); }
};

inline uint64_t frameEventTimeNS() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}

// A null recorder performs no clock read and submits nothing. Finish explicitly
// for nested phases, or let the destructor cover all returns from a call.
class FrameEventScope {
  FrameEventRecorder *recorder_;
  FrameEventSample sample_;
public:
  FrameEventScope(FrameEventRecorder *recorder, FrameEvent event, uint64_t frame,
                  uint64_t thread_id = 0, uint64_t detail = 0, uint64_t object_id = 0)
      : recorder_(recorder && recorder->allows(event) ? recorder : nullptr),
        sample_{event, frame, recorder_ ? frameEventTimeNS() : 0, 0,
                                    thread_id, object_id, detail} {}
  FrameEventScope(const FrameEventScope &) = delete;
  FrameEventScope &operator=(const FrameEventScope &) = delete;
  ~FrameEventScope() { finish(); }
  uint64_t startNS() const { return sample_.start_ns; }
  void setDetail(uint64_t detail) { sample_.detail = detail; }
  void finish() {
    if (!recorder_) return;
    sample_.duration_ns = frameEventTimeNS() - sample_.start_ns;
    recorder_->submit(sample_);
    recorder_ = nullptr;
  }
};

} // namespace dxmt
