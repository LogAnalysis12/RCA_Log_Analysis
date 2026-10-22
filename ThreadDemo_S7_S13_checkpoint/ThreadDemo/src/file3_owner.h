#pragma once
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>
#include "common/CommonStructs.h"

// ---- Scenario S1 (culprit side): thread T2 runs q3 and deletes var ----
class CResourceOwner {
public:
    void q3(Resource*& var);
};

// ---- Scenario S4: both sides lock the same mutex (control, NOT a data race) ----
class CEntry {
public:
    int id = 7;
};

class CSafeCache {
public:
    std::mutex m_mutex;
    std::shared_ptr<CEntry> m_entry = std::make_shared<CEntry>();
};

class CCacheEvictor {
public:
    void evict(CSafeCache& cache);
};

class CCacheReader {
public:
    void read(CSafeCache& cache);
};

// ---- Scenario S5: tasks run on a thread pool (hidden spawn via std::function) ----
class CThreadPool {
public:
    explicit CThreadPool(int workers);
    ~CThreadPool();
    void submit(std::function<void()> task);

private:
    void workerLoop();
    std::vector<std::thread> m_workers;
    std::queue<std::function<void()>> m_tasks;
    std::mutex m_mutex;
    std::condition_variable m_cv;
    bool m_stop = false;
};

class CCacheTasks {
public:
    void invalidate();
    void consume();
    std::vector<std::string> m_payload{"cached-item"};
};
