#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"
#include "scenario_s11_macros.h"

namespace {
struct S11State {
    std::mutex mutex;
    std::condition_variable cv;
    std::unique_ptr<Resource> handle{new Resource{1101, "macro-control", 11}};
    bool cleared = false;
};

class CS11Registry {
public:
    void clear(S11State& state) {
        CLoggerParser::Instance()->Writelog("CS11Registry::clear" FUNCTION_START, DEBUG_LOG);
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            RELEASE_AND_CLEAR(state.handle);
            state.cleared = true;
        }
        state.cv.notify_all();
        CLoggerParser::Instance()->Writelog("CS11Registry::clear" FUNCTION_END, DEBUG_LOG);
    }
};

class CS11Observer {
public:
    void inspect(S11State& state) {
        CLoggerParser::Instance()->Writelog("CS11Observer::inspect" FUNCTION_START, DEBUG_LOG);
        std::unique_lock<std::mutex> lock(state.mutex);
        state.cv.wait(lock, [&state] { return state.cleared; });
        const bool missing = !state.handle;
        lock.unlock();
        if (missing) {
            CLoggerParser::Instance()->Writelog("CS11Observer::inspect Configured resource is unavailable", ERROR_LOG);
        } else {
            CLoggerParser::Instance()->Writelog("CS11Observer::inspect Configured resource is available", INFO_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS11Observer::inspect" FUNCTION_END, DEBUG_LOG);
    }
};
}

void runS11() {
    CLoggerParser::Instance()->Writelog("CS11Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS11Scenario::run Scenario S11 start", INFO_LOG);
    S11State state;
    CS11Registry registry;
    CS11Observer observer;
    std::thread clearer(&CS11Registry::clear, &registry, std::ref(state));
    std::thread reader(&CS11Observer::inspect, &observer, std::ref(state));
    clearer.join();
    reader.join();
    CLoggerParser::Instance()->Writelog("CS11Scenario::run" FUNCTION_END, DEBUG_LOG);
}
