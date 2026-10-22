#include "file1_service.h"
#include "common/Logger.h"

// ===================== S1 victim chain: q1 -> q2 =====================
void CWorkerService::q1(Resource*& var) {
    CLoggerParser::Instance()->Writelog("CWorkerService::q1" FUNCTION_START, DEBUG_LOG);
    SleepMs(100);  // do some work first; T2 is faster
    q2(var);
    CLoggerParser::Instance()->Writelog("CWorkerService::q1" FUNCTION_END, DEBUG_LOG);
}

void CWorkerService::q2(Resource*& var) {
    CLoggerParser::Instance()->Writelog("CWorkerService::q2" FUNCTION_START, DEBUG_LOG);
    try {
        // @GT S1 victim CWorkerService::q2 READ var
        if (var == nullptr) {
            throw CDemoException(DemoError::VAR_NOT_AVAILABLE);
        }
        CLoggerParser::Instance()->Writelog("CWorkerService::q2 Using resource " + var->name, INFO_LOG);
    } catch (...) {
        CLoggerParser::Instance()->Writelog("CWorkerService::q2 Exception occured: Unknown exception", ERROR_LOG);
    }
}

// ===================== S6 classic parameter bug =====================
bool CServiceValidator::validate(const std::string& name) {
    CLoggerParser::Instance()->Writelog("CServiceValidator::validate" FUNCTION_START, DEBUG_LOG);
    // @GT S6 victim CServiceValidator::validate READ name
    if (name.empty()) {
        CLoggerParser::Instance()->Writelog("CServiceValidator::validate Service name is empty", ERROR_LOG);
        return false;
    }
    return true;
}

bool CRequestBuilder::build(const std::string& serviceName) {
    CLoggerParser::Instance()->Writelog("CRequestBuilder::build" FUNCTION_START, DEBUG_LOG);
    CServiceValidator validator;
    return validator.validate(serviceName);
}

std::string CMenuController::selectService(int index) {
    CLoggerParser::Instance()->Writelog("CMenuController::selectService" FUNCTION_START, DEBUG_LOG);
    if (index == 1) return "ReadVIN";
    if (index == 2) return "ReadDTC";
    // @GT S6 culprit CMenuController::selectService RETURN_EMPTY name
    return "";  // BUG: index 3 has no service configured
}

bool CMenuController::openMenu(int index) {
    CLoggerParser::Instance()->Writelog("CMenuController::openMenu" FUNCTION_START, DEBUG_LOG);
    std::string name = selectService(index);
    CRequestBuilder builder;
    return builder.build(name);
}
