"""
把 launcher/launcher.py 打成 Windows 可执行文件（发布用）。

用法：
    python launcher/build_exe.py            # 构建
    python launcher/build_exe.py --clean    # 先删掉上次的构建产物再构建

产物：
    launcher/dist/AgentZeroToOne/AgentZeroToOne.exe   （目录形态，双击即可）
    launcher/dist/AgentZeroToOne/_internal/…          （PyInstaller 运行时，必须一起分发）

**为什么用 onedir 而不是 onefile？**
    1. onefile 每次运行都要把整个运行时解压到临时目录，启动慢（这个有几秒），
       而启动器的作用就是"双击后赶紧打开网页"，慢就失去意义了。
    2. onefile 的自解压行为是杀毒软件误报的重灾区，onedir 少得多。
    3. 出问题时 onedir 能直接看到 _internal 里的东西，好排查。

**这个 exe 里没有 Python 解释器吗？**
    有。PyInstaller 会把构建机的 Python 打进去，所以 exe 自己能跑起来。
    但**判题**用的是另一个进程：判题器要执行用户提交的 Python 代码，
    必然要一个真实的 python.exe（见 launcher.py 顶部的说明）。
    所以 exe 自己不需要用户装 Python 就能启动、开网页；
    但要用「在线判题」和跑 `code/` 里的章节脚本，用户机器上得有 Python。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
SPEC = os.path.join(HERE, "AgentZeroToOne.spec")

APP_NAME = "AgentZeroToOne"
ENTRY = os.path.join(HERE, "launcher.py")


def clean():
    for path in (DIST, BUILD, SPEC):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print("  已删除 %s" % path)
        elif os.path.isfile(path):
            os.remove(path)
            print("  已删除 %s" % path)


def build():
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",              # 见模块注释：不用 onefile
        "--console",             # 要控制台：服务日志、Ctrl+C 停服务、报错可读
        "--name", APP_NAME,
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", HERE,
        # 启动器只用标准库，没有第三方依赖要收；显式排除几个体积大户，
        # 免得 PyInstaller 顺着环境里的包把它们拖进来。
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "test",
        ENTRY,
    ]
    print("[*] " + " ".join(command))
    print()
    result = subprocess.run(command, cwd=HERE)
    if result.returncode != 0:
        print()
        print("[!] 构建失败（退出码 %d）" % result.returncode)
        return result.returncode

    exe = os.path.join(DIST, APP_NAME, APP_NAME + ".exe")
    if not os.path.isfile(exe):
        print("[!] 构建结束但找不到 %s" % exe)
        return 1

    size = 0
    for dirpath, _dirnames, filenames in os.walk(os.path.join(DIST, APP_NAME)):
        for name in filenames:
            size += os.path.getsize(os.path.join(dirpath, name))
    print()
    print("[OK] %s" % exe)
    print("     目录总大小 %.1f MB" % (size / 1024.0 / 1024.0))
    return 0


def main() -> int:
    if "--clean" in sys.argv:
        print("[*] 清理上次的构建产物")
        clean()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] 没装 PyInstaller。先跑：pip install pyinstaller")
        return 2
    return build()


if __name__ == "__main__":
    sys.exit(main())
