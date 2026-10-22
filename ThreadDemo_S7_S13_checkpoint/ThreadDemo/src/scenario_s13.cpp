#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
struct S13State {
    std::atomic<int> available{1};
    std::mutex gateMutex;
    std::condition_variable gateCv;
    int checkedCount = 0;
};

class CS13Quota {
public:
    void reservePrimary(S13State& state) {
        CLoggerParser::Instance()->Writelog("CS13Quota::reservePrimary" FUNCTION_START, DEBUG_LOG);
        std::lock_guard<std::mutex> ownLock(m_primaryMutex);
        if (state.available.load() > 0) {
            waitForBothChecks(state);
            state.available.fetch_sub(1);
            CLoggerParser::Instance()->Writelog("CS13Quota::reservePrimary Permit reserved", INFO_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS13Quota::reservePrimary" FUNCTION_END, DEBUG_LOG);
    }

    void reserveFallback(S13State& state) {
        CLoggerParser::Instance()->Writelog("CS13Quota::reserveFallback" FUNCTION_START, DEBUG_LOG);
        std::lock_guard<std::mutex> ownLock(m_fallbackMutex);
        if (state.available.load() > 0) {
            waitForBothChecks(state);
            state.available.fetch_sub(1);
            CLoggerParser::Instance()->Writelog("CS13Quota::reserveFallback Permit reserved", INFO_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS13Quota::reserveFallback" FUNCTION_END, DEBUG_LOG);
    }
private:
    static void waitForBothChecks(S13State& state) {
        std::unique_lock<std::mutex> lock(state.gateMutex);
        ++state.checkedCount;
        state.gateCv.notify_all();
        state.gateCv.wait(lock, [&state] { return state.checkedCount == 2; });
    }
    std::mutex m_primaryMutex;
    std::mutex m_fallbackMutex;
};
}

void runS13() {
    CLoggerParser::Instance()->Writelog("CS13Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS13Scenario::run Scenario S13 start", INFO_LOG);
    S13State state;
    CS13Quota quota;
    std::thread first(&CS13Quota::reservePrimary, &quota, std::ref(state));
    std::thread second(&CS13Quota::reserveFallback, &quota, std::ref(state));
    first.join();
    second.join();
    const int remaining = state.available.load();
    if (remaining < 0) {
        CLoggerParser::Instance()->Writelog("CS13Scenario::run Capacity fell below zero: " + std::to_string(remaining), ERROR_LOG);
    } else {
        CLoggerParser::Instance()->Writelog("CS13Scenario::run Capacity remaining: " + std::to_string(remaining), INFO_LOG);
    }
    CLoggerParser::Instance()->Writelog("CS13Scenario::run" FUNCTION_END, DEBUG_LOG);
}
