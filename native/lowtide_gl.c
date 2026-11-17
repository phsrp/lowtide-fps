// lowtide_gl: frame timing for OpenGL games that Windows doesn't report to PresentMon.
//
// Lowtide loads this into an OpenGL game only when you turn on "Measure OpenGL games" in its
// settings. It hooks opengl32!wglSwapBuffers (gdi32!SwapBuffers ends up there too) and writes
// the time of every call into shared memory that Lowtide reads. Timing is taken when the game
// hands the frame over, before the driver call, so it's "time between presents" - the same
// thing RTSS and PresentMon's MsBetweenPresents measure. Nothing else in the game is touched.
//
// Build: x86_64-w64-mingw32-gcc -O2 -shared -s -o lowtide_gl64.dll lowtide_gl.c
//        minhook/src/*.c minhook/src/hde/hde64.c -Iminhook/include
#include <windows.h>
#include "MinHook.h"

#define LTGL_MAGIC 0x4C47544C  /* "LTGL" */
#define LTGL_VERSION 1
#define LTGL_RING 8192

// Must match the layout in lowtide.py (GLShared)
typedef struct {
    LONG magic;
    LONG version;
    LONGLONG qpc_freq;
    volatile LONGLONG count;      // frames written so far; slot = count % LTGL_RING
    LONGLONG qpc[LTGL_RING];      // QueryPerformanceCounter at each SwapBuffers call
} LtglShared;

typedef BOOL (WINAPI *SwapBuffersFn)(HDC);

static LtglShared *g_shared;
static HANDLE g_mapping;
static SwapBuffersFn g_original;

static BOOL WINAPI swap_hook(HDC hdc)
{
    if (g_shared) {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        LONGLONG n = g_shared->count;
        g_shared->qpc[n % LTGL_RING] = now.QuadPart;
        MemoryBarrier();          // the timestamp lands before the count that announces it
        g_shared->count = n + 1;
    }
    return g_original(hdc);
}

static DWORD WINAPI init(LPVOID unused)
{
    (void)unused;
    WCHAR name[64];
    wsprintfW(name, L"Local\\LowtideGL_%lu", GetCurrentProcessId());
    g_mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0,
                                   sizeof(LtglShared), name);
    if (!g_mapping)
        return 1;
    LtglShared *shared = (LtglShared *)MapViewOfFile(g_mapping, FILE_MAP_ALL_ACCESS, 0, 0,
                                                     sizeof(LtglShared));
    if (!shared)
        return 1;
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    shared->qpc_freq = freq.QuadPart;
    shared->version = LTGL_VERSION;
    shared->count = 0;

    HMODULE gl = GetModuleHandleW(L"opengl32.dll");
    void *target = gl ? (void *)GetProcAddress(gl, "wglSwapBuffers") : NULL;
    if (!target || MH_Initialize() != MH_OK)
        return 1;
    if (MH_CreateHook(target, (void *)swap_hook, (void **)&g_original) != MH_OK ||
        MH_EnableHook(target) != MH_OK)
        return 1;
    g_shared = shared;
    MemoryBarrier();
    shared->magic = LTGL_MAGIC;   // Lowtide starts reading once this is set
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE dll, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(dll);
        // hooking takes locks the loader holds during DllMain, so do it on its own thread
        HANDLE t = CreateThread(NULL, 0, init, NULL, 0, NULL);
        if (t)
            CloseHandle(t);
    }
    return TRUE;
}
