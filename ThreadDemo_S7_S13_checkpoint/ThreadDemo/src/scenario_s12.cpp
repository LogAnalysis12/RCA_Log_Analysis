#include <atomic>
#include <functional>
#include <memory>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
struct S12LifetimeState {
    std::atomic<bool> alive{true};
};

struct S12LifetimeGuard {
    std::shared_ptr<S12LifetimeState> state;
    ~S12LifetimeGuard() { state->alive.store(false, std::memory_order_release); }
};

class CS12Queue {
public:
    void registerCallback() {
        CLoggerParser::Instance()->Writelog("CS12Queue::registerCallback" FUNCTION_START, DEBUG_LOG);
        int localValue = 12;
        auto lifetime = std::make_shared<S12LifetimeState>();
        S12LifetimeGuard guard{lifetime};
        m_callback = [&localValue, lifetime]() {
            if (lifetime->alive.load(std::memory_order_acquire)) {
                CLoggerParser::Instance()->Writelog("CS12Queue::callback Value observed " + std::to_string(localValue), INFO_LOG);
                return true;
            }
            return false;
        };
        CLoggerParser::Instance()->Writelog("CS12Queue::registerCallback" FUNCTION_END, DEBUG_LOG);
    }

    void invokeDeferred() {
        CLoggerParser::Instance()->Writelog("CS12Queue::invokeDeferred" FUNCTION_START, DEBUG_LOG);
        if (!m_callback || !m_callback()) {
            CLoggerParser::Instance()->Writelog("CS12Queue::invokeDeferred Deferred callback outlived its captured stack state", ERROR_LOG);
        }
        CLoggerParser::Instance()->Writelog("CS12Queue::invokeDeferred" FUNCTION_END, DEBUG_LOG);
    }
private:
    std::function<bool()> m_callback;
};
}

void runS12() {
    CLoggerParser::Instance()->Writelog("CS12Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS12Scenario::run Scenario S12 start", INFO_LOG);
    CS12Queue queue;
    queue.registerCallback();
    std::thread worker(&CS12Queue::invokeDeferred, &queue);
    worker.join();
    CLoggerParser::Instance()->Writelog("CS12Scenario::run" FUNCTION_END, DEBUG_LOG);
}
