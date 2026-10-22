#include "file2_settings.h"
#include <string>
#include "common/Logger.h"

CSettings* CSettings::Instance() {
    static CSettings instance;
    return &instance;
}

// ===================== S2 =====================
void CReconfigService::applyProfile(int round) {
    CLoggerParser::Instance()->Writelog("CReconfigService::applyProfile" FUNCTION_START, DEBUG_LOG);
    SleepMs(10);
    // @GT S2 culprit CReconfigService::applyProfile WRITE CSettings::m_mode
    CSettings::Instance()->m_mode = -(round + 1);
    CLoggerParser::Instance()->Writelog("CReconfigService::applyProfile Profile applied", INFO_LOG);
}

bool CCommandHandler::execute(int session) {
    CLoggerParser::Instance()->Writelog("CCommandHandler::execute" FUNCTION_START, DEBUG_LOG);
    SleepMs(50);
    // @GT S2 victim CCommandHandler::execute READ CSettings::m_mode
    int mode = CSettings::Instance()->m_mode;
    if (mode < 0) {
        CLoggerParser::Instance()->Writelog("CCommandHandler::execute Invalid mode " + std::to_string(mode) +
                                                " for session " + std::to_string(session),
                                            ERROR_LOG);
        return false;
    }
    CLoggerParser::Instance()->Writelog("CCommandHandler::execute Command executed", INFO_LOG);
    return true;
}

// ===================== S3 =====================
void CLedgerWriter::post(CAccountLedger& ledger, long amount) {
    CLoggerParser::Instance()->Writelog("CLedgerWriter::post" FUNCTION_START, DEBUG_LOG);
    // @GT S3 lock CLedgerWriter::post LOCK CAccountLedger::m_mutex
    std::lock_guard<std::mutex> guard(ledger.m_mutex);
    // @GT S3 culprit CLedgerWriter::post WRITE CAccountLedger::m_debit
    ledger.m_debit += amount;
    SleepMs(40);  // invariant debit == credit is temporarily broken here
    // @GT S3 culprit_aux CLedgerWriter::post WRITE CAccountLedger::m_credit
    ledger.m_credit += amount;
}

void CLedgerAuditor::audit(CAccountLedger& ledger) {
    CLoggerParser::Instance()->Writelog("CLedgerAuditor::audit" FUNCTION_START, DEBUG_LOG);
    SleepMs(20);
    // @GT S3 missing_lock CLedgerAuditor::audit NO_LOCK CAccountLedger::m_mutex
    // @GT S3 victim CLedgerAuditor::audit READ CAccountLedger::m_debit
    long debit = ledger.m_debit;
    long credit = ledger.m_credit;
    if (debit != credit) {
        CLoggerParser::Instance()->Writelog("CLedgerAuditor::audit Ledger invariant broken: debit " +
                                                std::to_string(debit) + " credit " + std::to_string(credit),
                                            ERROR_LOG);
    }
}
