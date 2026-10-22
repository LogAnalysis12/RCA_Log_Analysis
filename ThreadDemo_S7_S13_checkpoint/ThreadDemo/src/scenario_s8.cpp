#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
struct S8Gate {
    std::mutex mutex;
    std::condition_variable cv;
    bool checkComplete = false;
    bool observerHasOwner = false;
    bool updateComplete = false;
};

class CS8Mutator {
public:
    void run(std::shared_ptr<Resource> resource, S8Gate& gate) {
        CLoggerParser::Instance()->Writelog("CS8Mutator::run" FUNCTION_START, DEBUG_LOG);
        const bool believedExclusive = resource.use_count() == 1;
        CLoggerParser::Instance()->Writelog("CS8Mutator::run Ownership count inspected", INFO_LOG);
        std::unique_lock<std::mutex> lock(gate.mutex);
        gate.checkComplete = true;
        gate.cv.notify_all();
        gate.cv.wait(lock, [&gate] { return gate.observerHasOwner; });
        if (believedExclusive) {
            resource->value = 99;
            gate.updateComplete = true;
            gate.cv.notify_all();
        }
        CLoggerParser::Instance()->Writelog("CS8Mutator::run" FUNCTION_END, DEBUG_LOG);
    }
};

class CS8Observer {
public:
    void run(std::weak_ptr<Resource> weak, S8Gate& gate) {
        CLoggerParser::Instance()->Writelog("CS8Observer::run" FUNCTION_START, DEBUG_LOG);
        std::unique_lock<std::mutex> lock(gate.mutex);
        gate.cv.wait(lock, [&gate] { return gate.checkComplete; });
        std::shared_ptr<Resource> resource = weak.lock();
        if (!resource) {
            CLoggerParser::Instance()->Writelog("CS8Observer::run Owner expired before observation", ERROR_LOG);
            return;
        }
        gate.observerHasOwner = true;
        gate.cv.notify_all();
        gate.cv.wait(lock, [&gate] { return gate.updateComplete; });
        const int observed = resource->value;
        if (observed == 99) {
            CLoggerParser::Instance()->Writelog("CS8Observer::run Value changed after exclusive path was selected", ERROR_LOG);
        } else {
            CLoggerParser::Instance()->Writelog("CS8Observer::run Value remained unchanged", INFO_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS8Observer::run" FUNCTION_END, DEBUG_LOG);
    }
};
}

void runS8() {
    CLoggerParser::Instance()->Writelog("CS8Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS8Scenario::run Scenario S8 start", INFO_LOG);
    auto owner = std::make_shared<Resource>(Resource{801, "shared-control", 8});
    std::weak_ptr<Resource> weak = owner;
    S8Gate gate;
    CS8Mutator mutator;
    CS8Observer observer;
    std::thread writer(&CS8Mutator::run, &mutator, std::move(owner), std::ref(gate));
    std::thread reader(&CS8Observer::run, &observer, weak, std::ref(gate));
    writer.join();
    reader.join();
    CLoggerParser::Instance()->Writelog("CS8Scenario::run" FUNCTION_END, DEBUG_LOG);
}
