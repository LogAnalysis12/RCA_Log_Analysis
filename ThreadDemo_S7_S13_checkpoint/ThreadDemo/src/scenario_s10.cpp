#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
struct S10State {
    std::mutex mutex;
    std::condition_variable cv;
    std::shared_ptr<Resource> owner;
    std::weak_ptr<Resource> observer;
    bool observerReady = false;
    bool ownerReleased = false;
};

class CS10Owner {
public:
    void releaseEarly(S10State& state) {
        CLoggerParser::Instance()->Writelog("CS10Owner::releaseEarly" FUNCTION_START, DEBUG_LOG);
        std::unique_lock<std::mutex> lock(state.mutex);
        state.cv.wait(lock, [&state] { return state.observerReady; });
        state.owner.reset();
        state.ownerReleased = true;
        lock.unlock();
        state.cv.notify_all();
        CLoggerParser::Instance()->Writelog("CS10Owner::releaseEarly Ownership released", INFO_LOG);
        CLoggerParser::Instance()->Writelog("CS10Owner::releaseEarly" FUNCTION_END, DEBUG_LOG);
    }
};

class CS10Observer {
public:
    void observe(S10State& state) {
        CLoggerParser::Instance()->Writelog("CS10Observer::observe" FUNCTION_START, DEBUG_LOG);
        std::unique_lock<std::mutex> lock(state.mutex);
        state.observerReady = true;
        state.cv.notify_all();
        state.cv.wait(lock, [&state] { return state.ownerReleased; });
        const bool expired = state.observer.lock() == nullptr;
        lock.unlock();
        if (expired) {
            CLoggerParser::Instance()->Writelog("CS10Observer::observe Weak reference expired before consumer completed", ERROR_LOG);
        } else {
            CLoggerParser::Instance()->Writelog("CS10Observer::observe Resource still available", INFO_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS10Observer::observe" FUNCTION_END, DEBUG_LOG);
    }
};
}

void runS10() {
    CLoggerParser::Instance()->Writelog("CS10Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS10Scenario::run Scenario S10 start", INFO_LOG);
    S10State state;
    state.owner = std::make_shared<Resource>(Resource{1001, "weak-control", 10});
    state.observer = state.owner;
    CS10Owner owner;
    CS10Observer observer;
    std::thread consumer(&CS10Observer::observe, &observer, std::ref(state));
    std::thread release(&CS10Owner::releaseEarly, &owner, std::ref(state));
    consumer.join();
    release.join();
    CLoggerParser::Instance()->Writelog("CS10Scenario::run" FUNCTION_END, DEBUG_LOG);
}
