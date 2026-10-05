#include "dxmt_tasks.hpp"
#include <array>
#include <chrono>
#include <cstdio>
#include <stdexcept>
#include <vector>

struct Task {
  std::atomic_bool done{false};
  std::atomic_uint calls{0};
  std::atomic_int priority{-999};
  std::atomic_bool *gate = nullptr;
  Task *dependency = nullptr;
};

namespace dxmt {
template <> struct task_trait<Task *> {
  Task *run_task(Task *task) {
    if (task->dependency && !task->dependency->done.load(std::memory_order_acquire))
      return task->dependency;
    while (task->gate && !task->gate->load(std::memory_order_acquire)) Sleep(1);
    task->priority = GetThreadPriority(GetCurrentThread());
    task->calls++;
    return task;
  }
  bool get_done(Task *task) { return task->done.load(std::memory_order_acquire); }
  void set_done(Task *task) { task->done.store(true, std::memory_order_release); }
};
}

template <typename F> void until(F predicate) {
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
  while (!predicate()) {
    if (std::chrono::steady_clock::now() > deadline) throw std::runtime_error("scheduler progress timed out");
    Sleep(1);
  }
}

void require(bool value, const char *message) { if (!value) throw std::runtime_error(message); }

void check_budget(unsigned limit) {
  std::array<Task, 24> tasks;
  std::atomic_bool gate{false};
  dxmt::task_scheduler<Task *> pool(limit);
  struct GateRelease { std::atomic_bool &gate; ~GateRelease() { gate = true; } } release{gate};
  const unsigned initial = unsigned(pool.get_worker_count());
  for (unsigned i=0; i<initial; ++i) { tasks[i].gate=&gate; pool.submit(&tasks[i]); }
  until([&](){ return pool.get_running_threads()==initial; });
  for (unsigned i=initial; i<limit; ++i) {
    tasks[i].gate=&gate; pool.submit(&tasks[i]);
    until([&](){ return pool.get_running_threads()==i+1; });
  }
  for (unsigned i=limit; i<tasks.size(); ++i) pool.submit(&tasks[i]);
  require(pool.get_worker_count()==limit,"worker cap exceeded");
  require(pool.get_queued_tasks()==tasks.size()-limit,"queued counter incorrect");
  gate=true;
  until([&](){ for (auto &t:tasks) if(!t.done.load()) return false; return true; });
  until([&](){ return pool.get_running_threads()==0 && pool.get_queued_tasks()==0; });
  for(auto &t:tasks) {
    require(t.calls==1,"task lost or duplicated");
    require(t.priority==THREAD_PRIORITY_NORMAL,"bounded worker did not use normal priority");
  }
  require(pool.get_worker_limit()==limit,"wrong worker limit");
}

void check_dependencies() {
  Task dependency, dependent;
  dependent.dependency=&dependency;
  dxmt::task_scheduler<Task *> pool(1);
  pool.submit(&dependent);
  until([&](){ return pool.get_running_threads()==0; });
  pool.submit(&dependency);
  until([&](){ return dependent.done.load(); });
  require(dependent.calls==1 && dependency.calls==1,"dependency continuation did not execute exactly once");
}

void check_parallel_submitters() {
  std::array<Task,256> tasks;
  dxmt::task_scheduler<Task *> pool(4);
  std::vector<dxmt::thread> submitters;
  for(unsigned lane=0;lane<4;++lane) submitters.emplace_back([&,lane](){
    for(unsigned i=lane;i<tasks.size();i+=4) pool.submit(&tasks[i]);
  });
  for(auto &t:submitters)t.join();
  until([&](){ for(auto &t:tasks) if(!t.done.load())return false;return true; });
  for(auto &t:tasks)require(t.calls==1,"parallel submit lost or duplicated task");
  require(pool.get_worker_count()<=4,"parallel submit exceeded cap");
}

int main() {
  try {
    check_budget(1); check_budget(4); check_dependencies(); check_parallel_submitters();
    Task legacy;
    dxmt::task_scheduler<Task *> pool;
    pool.submit(&legacy);
    until([&](){ return legacy.done.load(); });
    require(legacy.priority==THREAD_PRIORITY_TIME_CRITICAL,"legacy priority changed");
    require(pool.get_worker_limit()==std::max<unsigned>(2,dxmt::thread::hardware_concurrency()*2),"legacy limit changed");
    std::printf("shader_workers_passed: limits1/4, priority, queue counts, dependencies, parallel submit, legacy; CPU=%u legacy_limit=%llu\n",
      dxmt::thread::hardware_concurrency(),(unsigned long long)pool.get_worker_limit());
    return 0;
  } catch(const std::exception &error) { std::printf("shader_workers_failed: %s\n",error.what()); return 1; }
}
