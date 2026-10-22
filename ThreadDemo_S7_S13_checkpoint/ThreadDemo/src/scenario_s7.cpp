#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include <string>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
struct S7State {
    std::mutex mutex;
    std::condition_variable cv;
    std::unique_ptr<Resource> current{new Resource{701, "shared-entry", 7}};
    bool released = false;
    bool auditReady = false;
    int auditValue = 0;
    int readyCount = 0;
};

class CS7ResourceManager {
public:
    void releaseCurrent(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7ResourceManager::releaseCurrent" FUNCTION_START, DEBUG_LOG);
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            state.cv.wait(lock, [&state] { return state.readyCount == 7; });
            state.current.reset();
            state.released = true;
        }
        state.cv.notify_all();
        CLoggerParser::Instance()->Writelog("CS7ResourceManager::releaseCurrent Resource released", INFO_LOG);
        CLoggerParser::Instance()->Writelog("CS7ResourceManager::releaseCurrent" FUNCTION_END, DEBUG_LOG);
    }
};

class CS7Transport {
public:
    void forward(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Transport::forward" FUNCTION_START, DEBUG_LOG);
        manager.releaseCurrent(state);
        CLoggerParser::Instance()->Writelog("CS7Transport::forward" FUNCTION_END, DEBUG_LOG);
    }
private:
    CS7ResourceManager manager;
};

class CS7Workers {
public:
    void runT1(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT1" FUNCTION_START, DEBUG_LOG);
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            state.auditValue = 41;
            state.auditReady = true;
        }
        state.cv.notify_all();
        observe(state, "CS7Workers::runT1");
    }
    void runT2(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT2" FUNCTION_START, DEBUG_LOG);
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            state.cv.wait(lock, [&state] { return state.auditReady; });
            if (state.auditValue == 41) {
                CLoggerParser::Instance()->Writelog("CS7Workers::runT2 Audit marker observed", INFO_LOG);
            }
        }
        observe(state, "CS7Workers::runT2");
    }
    void runT3(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT3" FUNCTION_START, DEBUG_LOG);
        observe(state, "CS7Workers::runT3");
    }
    void runT4(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT4" FUNCTION_START, DEBUG_LOG);
        observe(state, "CS7Workers::runT4");
    }
    void runT5(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT5" FUNCTION_START, DEBUG_LOG);
        observe(state, "CS7Workers::runT5");
    }
    void runT6(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT6" FUNCTION_START, DEBUG_LOG);
        transport.forward(state);
        CLoggerParser::Instance()->Writelog("CS7Workers::runT6" FUNCTION_END, DEBUG_LOG);
    }
    void runT7(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT7" FUNCTION_START, DEBUG_LOG);
        observe(state, "CS7Workers::runT7");
    }
    void runT8(S7State& state) {
        CLoggerParser::Instance()->Writelog("CS7Workers::runT8" FUNCTION_START, DEBUG_LOG);
        observe(state, "CS7Workers::runT8");
    }
private:
    static void observe(S7State& state, const std::string& function) {
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            ++state.readyCount;
            state.cv.notify_all();
            state.cv.wait(lock, [&state] { return state.released; });
            if (state.current) {
                CLoggerParser::Instance()->Writelog(function + " Shared entry available", INFO_LOG);
            } else {
                CLoggerParser::Instance()->Writelog(function + " Shared resource unavailable after phase transition", ERROR_LOG);
            }
        }
        CLoggerParser::Instance()->Writelog(function + FUNCTION_END, DEBUG_LOG);
    }
    CS7Transport transport;
};
}

void runS7() {
    CLoggerParser::Instance()->Writelog("CS7Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS7Scenario::run Scenario S7 start", INFO_LOG);
    S7State state;
    CS7Workers workers;
    std::thread t1(&CS7Workers::runT1, &workers, std::ref(state));
    std::thread t2(&CS7Workers::runT2, &workers, std::ref(state));
    std::thread t3(&CS7Workers::runT3, &workers, std::ref(state));
    std::thread t4(&CS7Workers::runT4, &workers, std::ref(state));
    std::thread t5(&CS7Workers::runT5, &workers, std::ref(state));
    std::thread t6(&CS7Workers::runT6, &workers, std::ref(state));
    std::thread t7(&CS7Workers::runT7, &workers, std::ref(state));
    std::thread t8(&CS7Workers::runT8, &workers, std::ref(state));
    t1.join(); t2.join(); t3.join(); t4.join();
    t5.join(); t6.join(); t7.join(); t8.join();
    CLoggerParser::Instance()->Writelog("CS7Scenario::run" FUNCTION_END, DEBUG_LOG);
}
