#include "file3_owner.h"
#include "common/Logger.h"

// ===================== S1 culprit =====================
void CResourceOwner::q3(Resource*& var) {
    CLoggerParser::Instance()->Writelog("CResourceOwner::q3" FUNCTION_START, DEBUG_LOG);
    SleepMs(20);
    // @GT S1 culprit CResourceOwner::q3 DELETE var
    delete var;
    var = nullptr;
    CLoggerParser::Instance()->Writelog("CResourceOwner::q3 Resource released", INFO_LOG);
}

// ===================== S4 (both locked) =====================
void CCacheEvictor::evict(CSafeCache& cache) {
    CLoggerParser::Instance()->Writelog("CCacheEvictor::evict" FUNCTION_START, DEBUG_LOG);
    SleepMs(5);
    {
        // @GT S4 lock CCacheEvictor::evict LOCK CSafeCache::m_mutex
        std::lock_guard<std::mutex> guard(cache.m_mutex);
        // @GT S4 culprit CCacheEvictor::evict DELETE CSafeCache::m_entry
        cache.m_entry.reset();
    }
    CLoggerParser::Instance()->Writelog("CCacheEvictor::evict Entry evicted", INFO_LOG);
}

void CCacheReader::read(CSafeCache& cache) {
    CLoggerParser::Instance()->Writelog("CCacheReader::read" FUNCTION_START, DEBUG_LOG);
    SleepMs(30);
    // @GT S4 lock CCacheReader::read LOCK CSafeCache::m_mutex
    std::lock_guard<std::mutex> guard(cache.m_mutex);
    // @GT S4 victim CCacheReader::read READ CSafeCache::m_entry
    if (!cache.m_entry) {
        CLoggerParser::Instance()->Writelog("CCacheReader::read Cache entry missing after eviction", ERROR_LOG);
    }
}

// ===================== S5 thread pool =====================
CThreadPool::CThreadPool(int workers) {
    for (int i = 0; i < workers; ++i) {
        // @GT S5 spawn CThreadPool::CThreadPool SPAWN CThreadPool::workerLoop args=this
        m_workers.push_back(std::thread(&CThreadPool::workerLoop, this));
    }
}

CThreadPool::~CThreadPool() {
    {
        std::lock_guard<std::mutex> guard(m_mutex);
        m_stop = true;
    }
    m_cv.notify_all();
    for (auto& worker : m_workers) {
        worker.join();
    }
}

void CThreadPool::submit(std::function<void()> task) {
    {
        std::lock_guard<std::mutex> guard(m_mutex);
        m_tasks.push(std::move(task));
    }
    m_cv.notify_one();
}

void CThreadPool::workerLoop() {
    while (true) {
        std::function<void()> task;
        {
            std::unique_lock<std::mutex> lock(m_mutex);
            m_cv.wait(lock, [this] { return m_stop || !m_tasks.empty(); });
            if (m_stop && m_tasks.empty()) {
                return;
            }
            task = std::move(m_tasks.front());
            m_tasks.pop();
        }
        task();  // indirect call: target is not visible in the static call graph
    }
}

void CCacheTasks::invalidate() {
    CLoggerParser::Instance()->Writelog("CCacheTasks::invalidate" FUNCTION_START, DEBUG_LOG);
    SleepMs(10);
    // @GT S5 culprit CCacheTasks::invalidate CLEAR CCacheTasks::m_payload
    m_payload.clear();
}

void CCacheTasks::consume() {
    CLoggerParser::Instance()->Writelog("CCacheTasks::consume" FUNCTION_START, DEBUG_LOG);
    SleepMs(60);
    try {
        // @GT S5 victim CCacheTasks::consume READ CCacheTasks::m_payload
        std::string item = m_payload.at(0);
        CLoggerParser::Instance()->Writelog("CCacheTasks::consume Consumed " + item, INFO_LOG);
    } catch (...) {
        CLoggerParser::Instance()->Writelog("CCacheTasks::consume Exception occured: Unknown exception", ERROR_LOG);
    }
}
