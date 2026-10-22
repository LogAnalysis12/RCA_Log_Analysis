#include <atomic>
#include <cstdlib>
#include <cstring>
#include <future>
#include <string>
#include <thread>

#include "common/Logger.h"
#include "common/NativeThread.h"
#include "file1_service.h"
#include "file2_settings.h"
#include "file3_owner.h"

// Background noise thread: its log lines interleave with every scenario.
class CHeartbeat {
public:
    void run(std::atomic<bool>& stop) {
        CLoggerParser::Instance()->Writelog("CHeartbeat::run" FUNCTION_START, DEBUG_LOG);
        int tick = 0;
        while (!stop.load()) {
            SleepMs(25);
            CLoggerParser::Instance()->Writelog("CHeartbeat::run Tick " + std::to_string(++tick), INFO_LOG);
        }
    }
};

// Entry function for the native OS thread used in S3.
struct AuditorContext {
    CLedgerAuditor* auditor;
    CAccountLedger* ledger;
};

static NATIVE_THREAD_PROC AuditorEntry(NATIVE_THREAD_ARG arg) {
    AuditorContext* ctx = static_cast<AuditorContext*>(arg);
    ctx->auditor->audit(*ctx->ledger);
    NATIVE_THREAD_RETURN;
}

class CScenarioRunner {
public:
    static void runS1();
    static void runS2(int rounds);
    static void runS3();
    static void runS4();
    static void runS5();
    static void runS6();
};

// S1: main-like function owns `var` and shares it with two threads.
void CScenarioRunner::runS1() {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS1 Scenario S1 start", INFO_LOG);
    // @GT S1 alloc CScenarioRunner::runS1 NEW var
    Resource* var = new Resource{1, "primary", 42};
    CWorkerService service;
    CResourceOwner owner;
    // @GT S1 spawn CScenarioRunner::runS1 SPAWN CWorkerService::q1 args=std::ref(var)
    std::thread t1(&CWorkerService::q1, &service, std::ref(var));
    // @GT S1 spawn CScenarioRunner::runS1 SPAWN CResourceOwner::q3 args=std::ref(var)
    std::thread t2(&CResourceOwner::q3, &owner, std::ref(var));
    t1.join();
    t2.join();
}

// S2: a field is changed by another thread; repeated several times.
void CScenarioRunner::runS2(int rounds) {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS2 Scenario S2 start", INFO_LOG);
    CReconfigService reconfig;
    CCommandHandler handler;
    for (int round = 0; round < rounds; ++round) {
        CSettings::Instance()->m_mode = 1;
        int session = 17 + round;
        // @GT S2 spawn CScenarioRunner::runS2 SPAWN lambda->CCommandHandler::execute args=session
        std::thread reader([&handler, session]() { handler.execute(session); });
        // @GT S2 spawn CScenarioRunner::runS2 SPAWN lambda->CReconfigService::applyProfile args=round
        auto writer = std::async(std::launch::async, [&reconfig, round]() { reconfig.applyProfile(round); });
        reader.join();
        writer.get();
    }
}

// S3: writer holds a lock, auditor (native thread) does not.
void CScenarioRunner::runS3() {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS3 Scenario S3 start", INFO_LOG);
    CAccountLedger ledger;
    CLedgerWriter writer;
    CLedgerAuditor auditor;
    // @GT S3 spawn CScenarioRunner::runS3 SPAWN CLedgerWriter::post args=std::ref(ledger)
    std::thread tw(&CLedgerWriter::post, &writer, std::ref(ledger), 100L);
    SleepMs(2);
    AuditorContext ctx{&auditor, &ledger};
    NativeThreadHandle handle;
    // @GT S3 spawn CScenarioRunner::runS3 SPAWN AuditorEntry via=StartNativeThread args=ctx
    StartNativeThread(&AuditorEntry, &ctx, handle);
    tw.join();
    JoinNativeThread(handle);
}

// S4: both sides lock the same mutex; error comes from ordering, not from a race.
void CScenarioRunner::runS4() {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS4 Scenario S4 start", INFO_LOG);
    CSafeCache cache;
    CCacheEvictor evictor;
    CCacheReader reader;
    // @GT S4 spawn CScenarioRunner::runS4 SPAWN CCacheEvictor::evict args=std::ref(cache)
    std::thread te(&CCacheEvictor::evict, &evictor, std::ref(cache));
    // @GT S4 spawn CScenarioRunner::runS4 SPAWN CCacheReader::read args=std::ref(cache)
    std::thread tr(&CCacheReader::read, &reader, std::ref(cache));
    te.join();
    tr.join();
}

// S5: two tasks run on a thread pool.
void CScenarioRunner::runS5() {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS5 Scenario S5 start", INFO_LOG);
    CCacheTasks tasks;
    {
        CThreadPool pool(2);
        // @GT S5 spawn CScenarioRunner::runS5 SPAWN lambda->CCacheTasks::consume via=CThreadPool::submit
        pool.submit([&tasks]() { tasks.consume(); });
        // @GT S5 spawn CScenarioRunner::runS5 SPAWN lambda->CCacheTasks::invalidate via=CThreadPool::submit
        pool.submit([&tasks]() { tasks.invalidate(); });
    }  // pool destructor drains the queue and joins the workers
}

// S6: classic single-thread bug: a caller passes an empty name.
void CScenarioRunner::runS6() {
    CLoggerParser::Instance()->Writelog("CScenarioRunner::runS6 Scenario S6 start", INFO_LOG);
    CMenuController menu;
    menu.openMenu(1);
    menu.openMenu(3);
}

int main(int argc, char** argv) {
    int rounds = 5;
    int repeat = 1;
    std::string logPath = "logs/demo_run.log";
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--rounds") && i + 1 < argc) rounds = std::atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--repeat") && i + 1 < argc) repeat = std::atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--log") && i + 1 < argc) logPath = argv[++i];
    }
    if (!CLoggerParser::Instance()->Open(logPath)) {
        std::fprintf(stderr, "Cannot open log file %s\n", logPath.c_str());
        return 1;
    }

    std::atomic<bool> stop(false);
    CHeartbeat heartbeat;
    std::thread noise(&CHeartbeat::run, &heartbeat, std::ref(stop));

    for (int r = 0; r < repeat; ++r) {
        CScenarioRunner::runS1();
        SleepMs(30);
        CScenarioRunner::runS2(rounds);
        SleepMs(30);
        CScenarioRunner::runS3();
        SleepMs(30);
        CScenarioRunner::runS4();
        SleepMs(30);
        CScenarioRunner::runS5();
        SleepMs(30);
        CScenarioRunner::runS6();
        SleepMs(30);
    }

    stop.store(true);
    noise.join();
    CLoggerParser::Instance()->Close();
    std::printf("Done. Log written to %s\n", logPath.c_str());
    return 0;
}
