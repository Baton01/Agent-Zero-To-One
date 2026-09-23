/*
 * Agent Zero To One · 一键启动器（Windows 原生版）
 *
 * 双击之后做三件事：
 *   1. 找到一个可用的 Python 解释器
 *   2. 用它启动本地判题服务（05-算法面试/06-在线判题/judge_server.py）
 *   3. 打开浏览器到网页版的「在线判题」页
 *
 * ---------------------------------------------------------------------------
 * 为什么是 C，而不是用 PyInstaller 打包 Python
 * ---------------------------------------------------------------------------
 * 最早这版是用 PyInstaller 把 launcher.py 冻成 exe 的。结果在装了 360 安全卫士的
 * 机器上，exe 一被双击就被隔离：先提示"拒绝访问"，随后文件直接从磁盘消失。
 * 这不是个例 —— PyInstaller 的引导器（bootloader）是杀软启发式规则的常客，
 * 而未签名的 exe 对国产杀软尤其敏感。这个项目的读者大多在国内，等于一半人用不了。
 *
 * 对照实验：同样用 gcc 编出来的原生 exe，54 KB，运行正常、不被删。
 * 于是换成原生实现：几十 KB、没有引导器特征、也不依赖 Python 就能启动。
 * 顺带把体积从 13.9 MB 降到几十 KB —— 这个程序要做的只是"起个子进程 + 开个浏览器"。
 *
 * ---------------------------------------------------------------------------
 * 为什么还是离不开 Python
 * ---------------------------------------------------------------------------
 * 判题器执行用户提交的代码，靠的是起一个真实的 python 子进程跑 driver.py，
 * 这个依赖消不掉（详见 launcher.py 顶部的说明）。所以本程序只负责找到它、
 * 找不到时说清楚装哪个版本，并照样把网页版打开。
 *
 * 构建见 build_exe.py。
 */

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define _WIN32_WINNT 0x0601

/* winsock2.h 必须在 windows.h 之前，否则两边的定义会打架 */
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define JUDGE_DIR      L"05-算法面试\\06-在线判题"
#define SERVER_PY      L"judge_server.py"
#define WEB_HTML       L"web\\index.html"
#define DEFAULT_PORT   8900
#define MIN_PY_MAJOR   3
#define MIN_PY_MINOR   8
#define MAX_CANDIDATES 64

/* ------------------------------------------------------------------ 输出 */

/*
 * 控制台按 UTF-8 输出。源码本身是 UTF-8，g++ 的默认执行字符集也是 UTF-8，
 * 所以字符串里的中文就是 UTF-8 字节，SetConsoleOutputCP(CP_UTF8) 之后能正确显示。
 */
static void say(const char *utf8) {
    fputs(utf8, stdout);
    fflush(stdout);
}

/* 宽字符串（路径等）转 UTF-8 再打 */
static void sayw(const wchar_t *wide) {
    char buf[MAX_PATH * 4];
    int n = WideCharToMultiByte(CP_UTF8, 0, wide, -1, buf, (int)sizeof(buf) - 1, NULL, NULL);
    if (n > 0) {
        say(buf);
    }
}

static void sayf(const char *fmt, ...) {
    char buf[4096];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    say(buf);
}

/*
 * 双击启动时，本进程是这个控制台的唯一主人 —— 程序一退出窗口就没了，
 * 用户只看到"闪退"。这种情况要在退出前停一下，把原因留在屏幕上。
 * 从已有终端里运行时，控制台上还挂着别的进程，就不能停（会把人卡住）。
 */
static int console_would_vanish(void) {
    DWORD pids[4];
    DWORD n = GetConsoleProcessList(pids, 4);
    return n <= 1;
}

static void hold_console(void) {
    if (!console_would_vanish()) return;
    say("\n按回车键退出…\n");
    char buf[8];
    /* EOF（输出被重定向、没有输入）时 fgets 立刻返回，不会挂住 */
    if (fgets(buf, sizeof(buf), stdin) == NULL) return;
}

static void banner(void) {
    say("====================================================================\n");
    say("  Agent Zero To One · 一键启动\n");
    say("====================================================================\n");
}

/* ------------------------------------------------------------------ 路径 */

static int exe_dir(wchar_t *out, DWORD cap) {
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 0;
    wchar_t *slash = wcsrchr(path, L'\\');
    if (!slash) return 0;
    *slash = L'\0';
    wcsncpy(out, path, cap - 1);
    out[cap - 1] = L'\0';
    return 1;
}

static int file_exists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

static int dir_exists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

/*
 * 项目根：从 exe 所在目录向上找 web\index.html。
 *
 * 为什么向上找：开发布局里 exe 在 launcher\dist\AgentZeroToOne\，离项目根隔三层，
 * 用户很自然会去双击那里那个 exe。找不到的话也要在屏幕上说清楚。
 */
static int find_root(wchar_t *out, DWORD cap) {
    wchar_t dir[MAX_PATH];
    if (!exe_dir(dir, MAX_PATH)) return 0;

    for (int level = 0; level < 6; level++) {
        wchar_t probe[MAX_PATH * 2];
        _snwprintf(probe, MAX_PATH * 2, L"%s\\%s", dir, WEB_HTML);
        if (file_exists(probe)) {
            wcsncpy(out, dir, cap - 1);
            out[cap - 1] = L'\0';
            return 1;
        }
        wchar_t *slash = wcsrchr(dir, L'\\');
        if (!slash) break;
        *slash = L'\0';
        if (wcslen(dir) <= 2) break;        /* 到 "C:" 了 */
    }
    return 0;
}

/* ------------------------------------------------------------------ 进程 */

/*
 * 跑一个命令并抓输出（用来探测 Python 版本）。
 * 返回退出码，-1 表示起不来或超时；输出写进 out。
 */
static int run_capture(const wchar_t *cmdline, char *out, int cap, DWORD timeout_ms) {
    SECURITY_ATTRIBUTES sa;
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = NULL;
    sa.bInheritHandle = TRUE;

    HANDLE rd = NULL, wr = NULL;
    if (!CreatePipe(&rd, &wr, &sa, 0)) return -1;
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = wr;
    si.hStdError = wr;
    si.hStdInput = INVALID_HANDLE_VALUE;    /* 别继承 stdin：有些程序会等输入 */

    ZeroMemory(&pi, sizeof(pi));

    wchar_t mutable_cmd[4096];
    wcsncpy(mutable_cmd, cmdline, 4095);
    mutable_cmd[4095] = L'\0';

    if (!CreateProcessW(NULL, mutable_cmd, NULL, NULL, TRUE,
                        CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) {
        CloseHandle(rd);
        CloseHandle(wr);
        return -1;
    }
    CloseHandle(wr);    /* 父进程不写；关掉它，读端才会看到 EOF */

    DWORD waited = WaitForSingleObject(pi.hProcess, timeout_ms);
    int code = -1;
    if (waited == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
    } else {
        DWORD ec = 0;
        GetExitCodeProcess(pi.hProcess, &ec);
        code = (int)ec;
    }

    DWORD total = 0;
    for (;;) {
        DWORD got = 0;
        DWORD room = (DWORD)(cap - 1) - total;
        if (room == 0) break;
        if (!ReadFile(rd, out + total, room, &got, NULL) || got == 0) break;
        total += got;
    }
    out[total] = '\0';

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(rd);
    return code;
}

/*
 * 探测一个解释器：能打印出 "Python 3.x" 才算数。
 *
 * 为什么不能只看文件存在：Windows 的 %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe
 * 是个占位程序，执行它会弹微软应用商店。只有真能打印版本号的才算数。
 */
static int probe_python(const wchar_t *exe, int *major, int *minor) {
    wchar_t cmd[4096];
    _snwprintf(cmd, 4096, L"\"%s\" --version", exe);

    char out[512];
    out[0] = '\0';
    int code = run_capture(cmd, out, (int)sizeof(out), 12000);
    if (code != 0) return 0;
    if (_strnicmp(out, "python ", 7) != 0) return 0;

    int ma = 0, mi = 0;
    if (sscanf(out + 7, "%d.%d", &ma, &mi) != 2) return 0;
    if (ma < MIN_PY_MAJOR || (ma == MIN_PY_MAJOR && mi < MIN_PY_MINOR)) return 0;
    *major = ma;
    *minor = mi;
    return 1;
}

static int find_on_path(const wchar_t *name, wchar_t *out, DWORD cap) {
    wchar_t found[MAX_PATH];
    DWORD n = SearchPathW(NULL, name, L".exe", MAX_PATH, found, NULL);
    if (n == 0 || n >= MAX_PATH) return 0;
    wcsncpy(out, found, cap - 1);
    out[cap - 1] = L'\0';
    return 1;
}

/*
 * 找一个可用的 Python。
 *
 * 顺序：PATH 上的 python.exe → py.exe（多版本启动器，要带 -3）→ 常见安装目录。
 * 注意别用"名字以 py 开头"来判断启动器 —— python.exe 也以 py 开头。
 */
static int find_python(wchar_t *exe_out, DWORD cap, int *use_dash3,
                       char *label, int label_cap) {
    wchar_t cand[MAX_CANDIDATES][MAX_PATH];
    int is_launcher[MAX_CANDIDATES];
    int count = 0;
    wchar_t one[MAX_PATH];

    if (find_on_path(L"python", one, MAX_PATH) && count < MAX_CANDIDATES) {
        wcsncpy(cand[count], one, MAX_PATH - 1);
        cand[count][MAX_PATH - 1] = L'\0';
        is_launcher[count] = 0;
        count++;
    }
    if (find_on_path(L"py", one, MAX_PATH) && count < MAX_CANDIDATES) {
        wcsncpy(cand[count], one, MAX_PATH - 1);
        cand[count][MAX_PATH - 1] = L'\0';
        is_launcher[count] = 1;
        count++;
    }

    /* 常见安装位置：%LOCALAPPDATA%\Programs\Python\Python3xx\ 与 C:\、Program Files 下的 Python3xx\ */
    wchar_t bases[3][MAX_PATH];
    int nb = 0;
    wchar_t tmp[MAX_PATH];
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", tmp, MAX_PATH) > 0) {
        _snwprintf(bases[nb], MAX_PATH, L"%s\\Programs\\Python", tmp);
        nb++;
    }
    if (GetEnvironmentVariableW(L"ProgramFiles", tmp, MAX_PATH) > 0) {
        _snwprintf(bases[nb], MAX_PATH, L"%s", tmp);
        nb++;
    }
    _snwprintf(bases[nb], MAX_PATH, L"C:\\");
    nb++;

    for (int b = 0; b < nb; b++) {
        wchar_t pattern[MAX_PATH * 2];
        _snwprintf(pattern, MAX_PATH * 2, L"%s\\Python3*", bases[b]);
        WIN32_FIND_DATAW fd;
        HANDLE h = FindFirstFileW(pattern, &fd);
        if (h == INVALID_HANDLE_VALUE) continue;
        do {
            if (count >= MAX_CANDIDATES) break;
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            wchar_t full[MAX_PATH * 2];
            _snwprintf(full, MAX_PATH * 2, L"%s\\%s\\python.exe", bases[b], fd.cFileName);
            if (file_exists(full)) {
                wcsncpy(cand[count], full, MAX_PATH - 1);
                cand[count][MAX_PATH - 1] = L'\0';
                is_launcher[count] = 0;
                count++;
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }

    for (int i = 0; i < count; i++) {
        int ma = 0, mi = 0;
        if (!probe_python(cand[i], &ma, &mi)) continue;
        wcsncpy(exe_out, cand[i], cap - 1);
        exe_out[cap - 1] = L'\0';
        *use_dash3 = is_launcher[i];
        if (label && label_cap > 0) {
            char narrow[MAX_PATH * 4];
            WideCharToMultiByte(CP_UTF8, 0, cand[i], -1, narrow, (int)sizeof(narrow) - 1, NULL, NULL);
            _snprintf(label, label_cap, "Python %d.%d（%s）", ma, mi, narrow);
            label[label_cap - 1] = '\0';
        }
        return 1;
    }
    return 0;
}

/* --------------------------------------------------------------- 端口探测 */

/* 能不能连上 127.0.0.1:port —— 比"看端口有没有被占"更能说明服务真的在 */
static int port_open(int port) {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return 0;

    struct sockaddr_in addr;
    ZeroMemory(&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");

    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);           /* 非阻塞，别让探测本身卡住 */

    int ok = 0;
    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        ok = 1;
    } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
        fd_set wfds, efds;
        FD_ZERO(&wfds);
        FD_SET(s, &wfds);
        FD_ZERO(&efds);
        FD_SET(s, &efds);
        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 300000;
        if (select(0, NULL, &wfds, &efds, &tv) > 0 && FD_ISSET(s, &wfds)) ok = 1;
    }
    closesocket(s);
    return ok;
}

/* ------------------------------------------------------------------ main */

int main(int argc, char **argv) {
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);

    int port = DEFAULT_PORT;
    int no_browser = 0, web_only = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--no-browser") == 0) {
            no_browser = 1;
        } else if (strcmp(argv[i], "--web-only") == 0) {
            web_only = 1;
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            port = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            say("usage: AgentZeroToOne.exe [-h] [--port PORT] [--no-browser] [--web-only]\n\n");
            say("Agent Zero To One 一键启动器（起判题服务 + 打开网页版）\n\n");
            say("optional arguments:\n");
            say("  -h, --help    show this help message and exit\n");
            say("  --port PORT   判题服务端口，默认 8900\n");
            say("  --no-browser  不打开浏览器\n");
            say("  --web-only    只打开网页版，不启动判题服务\n");
            return 0;
        }
    }

    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);

    banner();

    wchar_t root[MAX_PATH];
    if (!find_root(root, MAX_PATH)) {
        wchar_t here[MAX_PATH];
        exe_dir(here, MAX_PATH);
        say("\n[!] 没找到项目文件（web\\index.html）。\n\n");
        say("    从这里开始向上找了 6 层：");
        sayw(here);
        say("\n\n");
        say("    这个 exe 要和 web\\、docs\\、code\\ 放在一起（也就是项目根目录）。\n");
        say("    用 GitHub Releases 下载的压缩包的话，解压后直接双击里面的 exe 就行。\n");
        hold_console();
        return 2;
    }

    /* 网页版 URL：file:/// 形式，按正斜杠拼 */
    wchar_t web_path[MAX_PATH * 2];
    _snwprintf(web_path, MAX_PATH * 2, L"%s\\%s", root, WEB_HTML);
    wchar_t url[MAX_PATH * 2];
    _snwprintf(url, MAX_PATH * 2, L"file:///%s", web_path);
    for (wchar_t *p = url; *p; p++) {
        if (*p == L'\\') *p = L'/';
    }
    wchar_t judge_url[MAX_PATH * 2 + 16];
    _snwprintf(judge_url, MAX_PATH * 2 + 16, L"%s#/judge", url);

    if (web_only) {
        say("\n[*] 只打开网页版：\n    ");
        sayw(url);
        say("\n");
        if (!no_browser) ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
        return 0;
    }

    HANDLE child = NULL;
    int ready = 0;

    if (port_open(port)) {
        sayf("\n[=] 端口 %d 上已经有一个判题服务在跑，直接用。\n", port);
        ready = 1;
    } else {
        wchar_t py[MAX_PATH];
        int use_dash3 = 0;
        char label[512];
        label[0] = '\0';

        if (!find_python(py, MAX_PATH, &use_dash3, label, (int)sizeof(label))) {
            say("\n--------------------------------------------------------------------\n");
            say("  没找到可用的 Python\n");
            say("--------------------------------------------------------------------\n\n");
            say("  网页版（17 章教程 + 算法题解 + 模拟面试 + 项目实战案例）不需要 Python，\n");
            say("  已经给你打开了。但这两件事需要它：\n\n");
            say("    · 跑每章的配套代码（code/ 下 17 个脚本）\n");
            say("    · 在线判题（要起子进程跑你提交的代码）\n\n");
            say("  装一个 Python 3.9 或更新版本，再双击本程序即可：\n");
            say("    https://www.python.org/downloads/\n\n");
            say("  安装时记得勾上「Add Python to PATH」。装完不用重启，直接再双击一次就行。\n\n");
            say("[*] 只打开网页版：\n    ");
            sayw(url);
            say("\n");
            if (!no_browser) ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
            hold_console();
            return 1;
        }

        sayf("\n[*] %s\n\n", label);

        wchar_t judge_path[MAX_PATH * 2];
        _snwprintf(judge_path, MAX_PATH * 2, L"%s\\%s", root, JUDGE_DIR);

        if (!dir_exists(judge_path)) {
            say("[!] 找不到判题服务目录：\n    ");
            sayw(judge_path);
            say("\n");
            say("\n  GitHub Releases 下载的发布包里应该带着 05-算法面试\\06-在线判题\\。\n");
            say("  如果你是只拷了 exe 出来，把整个目录恢复回去。\n");
            hold_console();
            return 2;
        }

        /* 命令：python [-3] judge_server.py --port N */
        wchar_t cmd[4096];
        _snwprintf(cmd, 4096, L"\"%s\"%s\"%s\" --port %d",
                   py, use_dash3 ? L" -3 " : L" ", SERVER_PY, port);

        say("[*] 启动判题服务：");
        sayw(cmd);
        say("\n    工作目录：");
        sayw(judge_path);
        say("\n\n");

        /* 子进程的中文输出按 UTF-8 编码，和控制台保持一致 */
        SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
        SetEnvironmentVariableW(L"PYTHONUTF8", L"1");

        STARTUPINFOW si;
        PROCESS_INFORMATION pi;
        ZeroMemory(&si, sizeof(si));
        si.cb = sizeof(si);
        ZeroMemory(&pi, sizeof(pi));

        if (!CreateProcessW(NULL, cmd, NULL, NULL, TRUE, 0, NULL, judge_path, &si, &pi)) {
            sayf("[!] 启动失败，错误码 %lu\n", (unsigned long)GetLastError());
            say("\n  手动跑一次看报错：\n    cd \"");
            sayw(judge_path);
            say("\"\n    python ");
            say("judge_server.py\n");
            hold_console();
            return 2;
        }

        child = pi.hProcess;
        CloseHandle(pi.hThread);

        /* 等服务起来：最多 25 秒；进程中途死掉就立刻放弃，别干等 */
        for (int i = 0; i < 125; i++) {
            if (port_open(port)) {
                ready = 1;
                break;
            }
            if (WaitForSingleObject(child, 0) == WAIT_OBJECT_0) break;
            Sleep(200);
        }
    }

    if (ready) {
        say("\n====================================================================\n");
        say("  就绪\n");
        say("====================================================================\n\n");
        sayf("  判题服务    http://127.0.0.1:%d\n", port);
        say("  网页版      ");
        sayw(url);
        say("\n\n");
        say("  在网页里点左侧「面试准备 → 在线判题」开始做题。\n");
        say("  关闭这个窗口就会停掉判题服务。\n\n");
        if (!no_browser) ShellExecuteW(NULL, L"open", judge_url, NULL, NULL, SW_SHOWNORMAL);
    } else {
        sayf("\n[!] 判题服务没起来。可能的原因：\n\n");
        sayf("    · 端口 %d 被别的程序占了 —— 换个端口：--port 8912\n", port);
        say("    · Python 环境有问题 —— 手动跑一次看报错：\n      cd \"");
        wchar_t judge_path2[MAX_PATH * 2];
        _snwprintf(judge_path2, MAX_PATH * 2, L"%s\\%s", root, JUDGE_DIR);
        sayw(judge_path2);
        say("\"\n      python judge_server.py\n\n");
        say("  网页版仍然可以打开（不需要判题服务）：\n    ");
        sayw(url);
        say("\n");
        if (!no_browser) ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
        hold_console();
        return 1;
    }

    /* 服务是别人起的：提示一句就走，不占窗口 */
    if (child == NULL) {
        say("  这个窗口可以关掉了。\n");
        return 0;
    }

    /* 守在这里，让关窗口 / Ctrl+C 都能干净地收掉子进程 */
    say("  按 Ctrl+C 退出。\n");
    for (;;) {
        if (WaitForSingleObject(child, 500) == WAIT_OBJECT_0) {
            DWORD ec = 0;
            GetExitCodeProcess(child, &ec);
            sayf("\n[!] 判题服务自己退出了（退出码 %lu）。\n", (unsigned long)ec);
            CloseHandle(child);
            hold_console();
            return 1;
        }
    }
}
