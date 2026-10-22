#pragma once
#include <mutex>
#include "common/CommonStructs.h"

// ---- Scenario S2: value changed by another thread (singleton field) ----
class CSettings {
public:
    static CSettings* Instance();
    int m_mode = 1;
    int m_sessionLimit = 8;
};

class CReconfigService {
public:
    void applyProfile(int round);
};

class CCommandHandler {
public:
    bool execute(int session);
};

// ---- Scenario S3: lock on the writer only (partial locking) ----
class CAccountLedger {
public:
    std::mutex m_mutex;
    long m_debit = 0;
    long m_credit = 0;
};

class CLedgerWriter {
public:
    void post(CAccountLedger& ledger, long amount);
};

class CLedgerAuditor {
public:
    void audit(CAccountLedger& ledger);
};
