#pragma once
#include <string>
#include "common/CommonStructs.h"

// ---- Scenario S1 (victim side): thread T1 runs q1 -> q2 ----
class CWorkerService {
public:
    void q1(Resource*& var);
    void q2(Resource*& var);
};

// ---- Scenario S6 (classic single-thread parameter bug) ----
class CServiceValidator {
public:
    bool validate(const std::string& name);
};

class CRequestBuilder {
public:
    bool build(const std::string& serviceName);
};

class CMenuController {
public:
    std::string selectService(int index);
    bool openMenu(int index);
};
