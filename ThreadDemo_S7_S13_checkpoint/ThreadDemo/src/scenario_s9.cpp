#include <memory>
#include <thread>
#include "common/CommonStructs.h"
#include "common/Logger.h"

namespace {
class CS9Consumer {
public:
    void consume(std::unique_ptr<Resource> received) {
        CLoggerParser::Instance()->Writelog("CS9Consumer::consume" FUNCTION_START, DEBUG_LOG);
        Resource* transferred = received.release();
        std::unique_ptr<Resource> finalOwner(transferred);
        if (finalOwner) {
            CLoggerParser::Instance()->Writelog("CS9Consumer::consume Resource ownership received", INFO_LOG);
            finalOwner.reset();
        }
        CLoggerParser::Instance()->Writelog("CS9Consumer::consume" FUNCTION_END, DEBUG_LOG);
    }
};
}

void runS9() {
    CLoggerParser::Instance()->Writelog("CS9Scenario::run" FUNCTION_START, DEBUG_LOG);
    CLoggerParser::Instance()->Writelog("CS9Scenario::run Scenario S9 start", INFO_LOG);
    auto owner = std::make_unique<Resource>(Resource{901, "unique-control", 9});
    CS9Consumer consumer;
    std::thread worker(&CS9Consumer::consume, &consumer, std::move(owner));
    if (!owner) {
        CLoggerParser::Instance()->Writelog("CS9Scenario::run Original owner unavailable after transfer", ERROR_LOG);
    } else {
        CLoggerParser::Instance()->Writelog("CS9Scenario::run Original owner still holds resource", INFO_LOG);
    }
    worker.join();
    CLoggerParser::Instance()->Writelog("CS9Scenario::run" FUNCTION_END, DEBUG_LOG);
}
