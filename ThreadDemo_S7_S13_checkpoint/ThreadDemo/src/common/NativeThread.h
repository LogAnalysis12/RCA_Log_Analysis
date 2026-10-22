#pragma once
// Thin wrapper over the native OS thread API (CreateThread / pthread_create).
// The real thread entry function is passed as an ARGUMENT to StartNativeThread,
// which is a realistic "hidden spawn" case for the extractor.
#ifdef _WIN32
#include <windows.h>
typedef HANDLE NativeThreadHandle;
#define NATIVE_THREAD_PROC DWORD WINAPI
#define NATIVE_THREAD_ARG LPVOID
#define NATIVE_THREAD_RETURN return 0
inline bool StartNativeThread(DWORD(WINAPI* proc)(LPVOID), void* arg, NativeThreadHandle& handle) {
    handle = CreateThread(nullptr, 0, proc, arg, 0, nullptr);
    return handle != nullptr;
}
inline void JoinNativeThread(NativeThreadHandle handle) {
    WaitForSingleObject(handle, INFINITE);
    CloseHandle(handle);
}
#else
#include <pthread.h>
typedef pthread_t NativeThreadHandle;
#define NATIVE_THREAD_PROC void*
#define NATIVE_THREAD_ARG void*
#define NATIVE_THREAD_RETURN return nullptr
inline bool StartNativeThread(void* (*proc)(void*), void* arg, NativeThreadHandle& handle) {
    return pthread_create(&handle, nullptr, proc, arg) == 0;
}
inline void JoinNativeThread(NativeThreadHandle handle) {
    pthread_join(handle, nullptr);
}
#endif
