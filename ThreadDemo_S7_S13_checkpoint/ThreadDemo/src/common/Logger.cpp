#include "common/Logger.h"

#include <chrono>
#include <ctime>

#ifdef _WIN32
#include <windows.h>
#elif defined(__linux__)
#include <sys/syscall.h>
#include <unistd.h>
#else
#include <functional>
#endif

static unsigned long CurrentThreadNumber() {
#ifdef _WIN32
    return static_cast<unsigned long>(GetCurrentThreadId());
#elif defined(__linux__)
    return static_cast<unsigned long>(syscall(SYS_gettid));
#else
    return static_cast<unsigned long>(std::hash<std::thread::id>{}(std::this_thread::get_id()) % 100000);
#endif
}

CLoggerParser* CLoggerParser::Instance() {
    static CLoggerParser instance;
    return &instance;
}

bool CLoggerParser::Open(const std::string& path) {
    std::lock_guard<std::mutex> guard(m_mutex);
    m_file = std::fopen(path.c_str(), "w");
    return m_file != nullptr;
}

void CLoggerParser::Close() {
    std::lock_guard<std::mutex> guard(m_mutex);
    if (m_file) {
        std::fclose(m_file);
        m_file = nullptr;
    }
}

void CLoggerParser::Writelog(const std::string& message, const std::string& level) {
    using namespace std::chrono;
    auto now = system_clock::now();
    int millis = static_cast<int>(duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000);
    std::time_t t = system_clock::to_time_t(now);
    std::tm tmv{};
#ifdef _WIN32
    localtime_s(&tmv, &t);
#else
    localtime_r(&t, &tmv);
#endif
    char ts[80];
    std::snprintf(ts, sizeof(ts), "%02d-%02d-%04d_%02d:%02d:%02d.%03d", tmv.tm_mday, tmv.tm_mon + 1,
                  tmv.tm_year + 1900, tmv.tm_hour, tmv.tm_min, tmv.tm_sec, millis);
    double perf = duration<double>(steady_clock::now().time_since_epoch()).count();
    char perfText[32];
    std::snprintf(perfText, sizeof(perfText), "%.6f", perf);

    char head[200];
    std::snprintf(head, sizeof(head), "%s | %-20s | %-8lu | %-11s | ", ts, perfText, CurrentThreadNumber(),
                  level.c_str());

    std::lock_guard<std::mutex> guard(m_mutex);
    std::FILE* out = m_file ? m_file : stderr;
    std::fputs(head, out);
    std::fputs(message.c_str(), out);
    std::fputc('\n', out);
    std::fflush(out);
}
