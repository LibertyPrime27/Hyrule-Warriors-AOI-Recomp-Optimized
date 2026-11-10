// Native Windows entry point for the portable compiler. No game data is embedded.
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)show;
    wchar_t folder[32768], python[32768], command[32768];
    DWORD n = GetModuleFileNameW(NULL, folder, 32768);
    if (!n || n >= 32768) return 1;
    while (n && folder[n - 1] != L'\\') n--;
    if (!n) return 1;
    folder[n - 1] = 0;
    if (swprintf(python, 32768, L"%ls\\python\\pythonw.exe", folder) < 0 ||
        swprintf(command, 32768, L"\"%ls\" -E -s \"%ls\\source\\compiler\\gui.py\" %ls", python, folder, arguments) < 0)
        return 1;
    STARTUPINFOW startup = {.cb = sizeof startup};
    PROCESS_INFORMATION process;
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, folder, &startup, &process)) {
        MessageBoxW(NULL, L"Extract the complete compiler ZIP, then open the Hyrule Warriors AOI Recomp Optimized Compiler again.",
                    L"Hyrule Warriors AOI Recomp Optimized Compiler", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
