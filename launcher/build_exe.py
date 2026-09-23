"""
编译 Windows 一键启动器（发布用）。

用法：
    python launcher/build_exe.py            # 编译
    python launcher/build_exe.py --clean    # 先删掉上次的产物再编译

产物：
    launcher/dist/AgentZeroToOne.exe        （单个文件，几十 KB）

需要 gcc（MinGW-w64 或 TDM-GCC）。项目里的在线判题本来就要 g++，所以这个依赖
通常已经在。

--------------------------------------------------------------------------
为什么用 C 而不是 PyInstaller
--------------------------------------------------------------------------
最早这版是用 PyInstaller 把 launcher.py 冻成 exe 的。在装了 360 安全卫士的机器上，
双击之后 exe 被直接隔离：先提示"拒绝访问"，随后文件从磁盘消失。
PyInstaller 的引导器（bootloader）是杀软启发式规则的常客，而未签名的 exe
对国产杀软尤其敏感 —— 而这个项目的读者大多在国内。

对照实验：同样用 gcc 编出来的原生 exe，运行正常、不被删。
所以改成原生实现：体积从 13.9 MB 降到约 73 KB，启动更快，也绕开了整类误报。

跨平台（macOS / Linux）仍然用 launcher/launcher.py：
    python launcher/launcher.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
SOURCE = os.path.join(HERE, "launcher_win.c")
OUTPUT = os.path.join(DIST, "AgentZeroToOne.exe")

#: 需要链接的库：ws2_32 给 Winsock（探测端口），shell32 给 ShellExecuteW（开浏览器）
LIBS = ["-lws2_32", "-lshell32"]


def find_compiler():
    for name in ("gcc", "x86_64-w64-mingw32-gcc", "clang"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def clean():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST, ignore_errors=True)
        print("  已删除 %s" % DIST)


def build():
    compiler = find_compiler()
    if not compiler:
        print("[!] 找不到 C 编译器（gcc / clang）。")
        print("    Windows 上装 MinGW-w64 或 TDM-GCC；这个项目判 C++ 题本来也需要它。")
        return 2

    os.makedirs(DIST, exist_ok=True)
    command = [compiler, "-O2", "-Wall", "-o", OUTPUT, SOURCE] + LIBS
    print("[*] " + " ".join(command))
    print()
    result = subprocess.run(command, cwd=HERE)
    if result.returncode != 0:
        print()
        print("[!] 编译失败（退出码 %d）" % result.returncode)
        return result.returncode

    if not os.path.isfile(OUTPUT):
        print("[!] 编译结束但找不到 %s" % OUTPUT)
        return 1

    print()
    print("[OK] %s" % OUTPUT)
    print("     %.1f KB" % (os.path.getsize(OUTPUT) / 1024.0))
    print()
    print("     注意：这个 exe 要和 web/、docs/、code/ 放在一起才能工作。")
    print("     打发布包用：python launcher/package_release.py 1.0.0")
    return 0


def main() -> int:
    if "--clean" in sys.argv:
        print("[*] 清理上次的构建产物")
        clean()
    return build()


if __name__ == "__main__":
    sys.exit(main())
