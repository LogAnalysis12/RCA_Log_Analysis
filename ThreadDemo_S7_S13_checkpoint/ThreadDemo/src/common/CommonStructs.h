#pragma once
#include <string>
#include <thread>
#include <chrono>

// Log level constants (global consts, like the real project's Common_Structs.h)
const std::string DEBUG_LOG = "DEBUG";
const std::string INFO_LOG  = "INFO";
const std::string WARN_LOG  = "WARN";
const std::string ERROR_LOG = "ERROR";

#define FUNCTION_START " FUNCTION_START"
#define FUNCTION_END   " FUNCTION_END"

struct Resource {
    int id;
    std::string name;
    int value;
};

enum class DemoError { VAR_NOT_AVAILABLE = 1 };

struct CDemoException {
    explicit CDemoException(DemoError c) : code(c) {}
    DemoError code;
};

inline void SleepMs(int ms) {
    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
}
