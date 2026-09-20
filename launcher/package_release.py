"""
打发布包：把项目 + exe 打成一个"双击就能用"的 zip。

用法：
    python launcher/package_release.py 1.0.0

产物：
    launcher/release/Agent-Zero-To-One-v1.0.0-win64.zip

包内结构（exe 在根目录，和 web/、docs/ 平级 —— 启动器就是这么找项目根的）：

    Agent-Zero-To-One-v1.0.0-win64/
    ├── AgentZeroToOne.exe        ← 双击这个
    ├── _internal/                ← PyInstaller 运行时（必须一起带着）
    ├── web/                      ← 网页版（也可以直接双击 index.html）
    ├── docs/ code/ 05-算法面试/ …
    └── README.md

为什么包里同时有 exe 和完整源码？
    这个项目是「拿来学」的 —— 光有 exe 没法读章节、没法改代码，等于把价值砍掉一半。
    exe 只是省掉「开终端、cd、敲命令」这一步，源码本身才是主体。
    只想要源码的人用 GitHub 自动生成的 Source code (zip) 就行，那个里面没有 exe。
"""

from __future__ import annotations

import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RELEASE = os.path.join(HERE, "release")
DIST_APP = os.path.join(HERE, "dist", "AgentZeroToOne")
APP_NAME = "AgentZeroToOne"

#: 不进发布包的东西
EXCLUDE_DIRS = {".git", "__pycache__", "build", "dist", "release", ".idea", ".vscode"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".spec")


def copy_tree(src, dst):
    """把 src 拷到 dst，按上面的规则跳过不需要的东西"""
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            if name in EXCLUDE_FILES or name.endswith(EXCLUDE_SUFFIX):
                continue
            shutil.copy2(os.path.join(dirpath, name), os.path.join(target_dir, name))
            copied += 1
    return copied


def make_zip(source_dir, zip_path):
    """打包成 zip。用 ZIP_DEFLATED；中文文件名靠 zipfile 的 UTF-8 标志位（Python 3 默认）"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for dirpath, dirnames, filenames in os.walk(source_dir):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                arc = os.path.relpath(full, os.path.dirname(source_dir))
                archive.write(full, arc)


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
    stage_name = "Agent-Zero-To-One-v%s-win64" % version
    stage = os.path.join(RELEASE, stage_name)
    zip_path = os.path.join(RELEASE, stage_name + ".zip")

    exe = os.path.join(DIST_APP, APP_NAME + ".exe")
    internal = os.path.join(DIST_APP, "_internal")
    if not os.path.isfile(exe) or not os.path.isdir(internal):
        print("[!] 还没构建 exe。先跑：python launcher/build_exe.py")
        return 2

    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage, exist_ok=True)

    print("[*] 拷贝项目文件…")
    count = copy_tree(ROOT, stage)
    print("    %d 个文件" % count)

    print("[*] 放入 exe…")
    shutil.copy2(exe, os.path.join(stage, APP_NAME + ".exe"))
    shutil.copytree(internal, os.path.join(stage, "_internal"))

    # 发布包里带一份"怎么用"，免得用户双击之前还要回来翻 GitHub
    readme = os.path.join(stage, "怎么用.txt")
    with open(readme, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(
            "Agent Zero To One v%s\n"
            "==================================================\n"
            "\n"
            "两种打开方式，随便挑一种：\n"
            "\n"
            "  1. 双击 AgentZeroToOne.exe\n"
            "     会启动本地判题服务并自动打开网页版 —— 要用「在线判题」就用这个。\n"
            "\n"
            "  2. 双击 web\\index.html\n"
            "     纯网页版，不需要任何服务，也不需要 Python。\n"
            "     17 章教程、算法题解、模拟面试、项目实战案例都能看。\n"
            "\n"
            "--------------------------------------------------\n"
            "关于 Python\n"
            "--------------------------------------------------\n"
            "\n"
            "看网页、读文档、读代码 —— 不需要 Python。\n"
            "\n"
            "这两件事需要 Python 3.9+：\n"
            "  · 跑 code\\ 下 17 章的配套脚本（python run.py 5 这样跑）\n"
            "  · 在线判题（判题器要起子进程执行你提交的代码）\n"
            "\n"
            "没装的话去 https://www.python.org/downloads/ 装一个，\n"
            "安装时勾上「Add Python to PATH」。装完再双击 exe 即可。\n"
            "\n"
            "C++ 判题还需要 g++（MinGW-w64）或 clang++，没装就只能判 Python。\n"
            "\n"
            "--------------------------------------------------\n"
            "目录说明\n"
            "--------------------------------------------------\n"
            "\n"
            "  AgentZeroToOne.exe   一键启动器\n"
            "  web\\                 网页版（双击 index.html 直接看）\n"
            "  docs\\                17 章教程正文\n"
            "  code\\                每章配套代码 + 公共模块\n"
            "  00-配置\\             学习状态（学员档案 / 进度看板 / 学习日志）\n"
            "  01-Raw\\              学习路线、12 周计划、项目阶梯\n"
            "  02-Wiki\\             专题总结 / 速查表 / 面试题库\n"
            "  05-算法面试\\         13 专题 + 100 题详解 + 本地判题器\n"
            "  06-模拟面试\\         面试流程、提示词、65 道题、评分表\n"
            "  07-项目实战案例\\     真实 Go Agent 项目的完整面试复盘（76 道练习题）\n"
            "  skills\\              可复用技能包示例\n"
            "  学习中枢.md           AI 教练系统提示词（配给任意 AI 助手）\n"
            "\n"
            "完整说明见 README.md。\n"
            % version)
    print("    怎么用.txt")

    print("[*] 打 zip…")
    make_zip(stage, zip_path)
    size = os.path.getsize(zip_path)
    print()
    print("[OK] %s" % zip_path)
    print("     %s" % human(size))
    print("     解包后目录：%s" % stage_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
