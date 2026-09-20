"""
Agent Zero To One · 章节代码启动器

为什么要有这个文件：章节脚本都在 code/ 目录下，但你在项目根目录。
这个启动器让你在根目录也能直接跑，不用先 cd。

用法：
    python run.py                # 列出所有章节脚本
    python run.py 5              # 跑第 05 章
    python run.py ch05           # 同上（写法随意）
    python run.py 5 --part 3     # 带参数跑（参数会原样传给脚本）
    python run.py judge          # 启动在线判题服务（做题用）
    python run.py --help         # 看帮助

它做了三件小事：
    1. 把 + 号/大小写等写法归一，帮你找到正确的脚本
    2. 设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台中文乱码
    3. 在 code/ 目录下运行脚本，让 traces/ 等产物落在该落的地方
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.join(ROOT, "code")

#: 章节号 → (标题, 脚本名)。顺序就是学习顺序。
CHAPTERS = [
    ("00", "环境准备与第一次模型调用", "ch00_env_check.py"),
    ("01", "认识 AI Agent", "ch01_first_call.py"),
    ("02", "大模型基础", "ch02_tokens_cost.py"),
    ("03", "Prompt 工程与结构化输出", "ch03_prompt_structured.py"),
    ("04", "工具调用", "ch04_tool_calling.py"),
    ("05", "ReAct 最小 Agent Loop", "ch05_react_loop.py"),
    ("06", "范式进阶", "ch06_paradigms.py"),
    ("07", "记忆与上下文工程", "ch07_memory.py"),
    ("08", "RAG 全链路", "ch08_rag.py"),
    ("09", "Skills 能力包", "ch09_skills.py"),
    ("10", "MCP 与通信协议", "ch10_mcp_client.py"),
    ("11", "多智能体协作", "ch11_multi_agent.py"),
    ("12", "评估、可观测性与安全", "ch12_eval.py"),
    ("13", "部署、成本与性能", "ch13_deploy.py"),
    ("14", "构建你自己的 Agent 框架", "ch14_mini_harness.py"),
    ("15", "综合实战", "ch15_agent_app.py"),
    ("16", "毕业设计与面试", "ch16_graduation.py"),
]


def normalize(token: str) -> str:
    """把用户输入归一成两位章节号：'5' / 'ch5' / '第5章' / '05' → '05'。"""
    digits = re.findall(r"\d+", token)
    if not digits:
        return ""
    return "{:02d}".format(int(digits[0]))


def find_chapter(token: str):
    number = normalize(token)
    for chapter_no, title, script in CHAPTERS:
        if chapter_no == number:
            return chapter_no, title, script
    return None


def print_list() -> None:
    line = "=" * 68
    print(line)
    print("  Agent Zero To One · 章节代码清单")
    print("  用法：python run.py <章节号> [脚本参数...]")
    print(line)
    print("  {:<4} {:<28} {}".format("章节", "主题", "脚本"))
    print("-" * 68)
    for chapter_no, title, script in CHAPTERS:
        print("  {:<4} {:<28} {}".format(chapter_no, title, script))
    print("-" * 68)
    print("  例：python run.py 5              跑第 05 章")
    print("      python run.py judge          启动在线判题服务")
    print("      python run.py 5 --part 1     带参数跑")
    print()
    print("  提示：不带 .env 也能跑 —— 会自动进入离线 Mock 模式。")
    print("  想接真模型：复制 code/.env.example 为 code/.env 并填入 API Key。")
    print(line)


def main() -> int:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "-l", "--list"):
        print_list()
        return 0

    # 在线判题服务：它不在 code/ 下，单独转发
    if args[0] in ("judge", "judge-server", "判题"):
        return run_script_at(
            os.path.join(ROOT, "05-算法面试", "06-在线判题"),
            "judge_server.py", args[1:])

    target = args[0]
    found = find_chapter(target)

    # 也支持直接写脚本名：python run.py ch05_react_loop.py
    if found is None and target.endswith(".py"):
        script_path = os.path.join(CODE_DIR, os.path.basename(target))
        if os.path.isfile(script_path):
            return run_script(os.path.basename(target), args[1:])

    if found is None:
        print("[找不到] 无法识别章节：{}".format(target))
        print()
        print_list()
        return 2

    chapter_no, title, script = found
    print("=" * 68)
    print("  第 {} 章 · {}".format(chapter_no, title))
    print("  脚本：code/{}".format(script))
    print("=" * 68)
    print()
    return run_script(script, args[1:])


def run_script_at(directory: str, script: str, extra_args) -> int:
    """在指定目录下运行一个脚本（判题服务用）"""
    script_path = os.path.join(directory, script)
    if not os.path.isfile(script_path):
        print("[错误] 脚本不存在：{}".format(script_path))
        return 2
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        return subprocess.call([sys.executable, script_path] + list(extra_args),
                               cwd=directory, env=env)
    except KeyboardInterrupt:
        print("")
        print("[已中断]")
        return 130


def run_script(script: str, extra_args) -> int:
    script_path = os.path.join(CODE_DIR, script)
    if not os.path.isfile(script_path):
        print("[错误] 脚本不存在：{}".format(script_path))
        return 2

    # 中文输出在 Windows 控制台上容易乱码，这里统一指定 UTF-8
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    command = [sys.executable, script_path] + list(extra_args)
    try:
        # 在 code/ 目录下运行：这样脚本里的相对路径（traces/ 等）位置一致
        return subprocess.call(command, cwd=CODE_DIR, env=env)
    except KeyboardInterrupt:
        print("\n[已中断]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
