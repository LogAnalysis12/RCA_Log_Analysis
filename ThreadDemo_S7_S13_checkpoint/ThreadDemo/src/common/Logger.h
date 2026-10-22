#pragma once
#include <cstdio>
#include <mutex>
#include <string>
#include "common/CommonStructs.h"

// Writes lines in the same format as the real DriverWrapper log:
// DD-MM-YYYY_HH:MM:SS.mmm | perf_timer | thread_id | LEVEL | <Class::Function> message
class CLoggerParser {
public:
    static CLoggerParser* Instance();
    bool Open(const std::string& path);
    void Close();
    void Writelog(const std::string& message, const std::string& level);

private:
    CLoggerParser() = default;
    std::mutex m_mutex;
    std::FILE* m_file = nullptr;
};
