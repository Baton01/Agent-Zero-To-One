"""
命令行判题工具。

不想开网页也能刷题：写一个 .py 文件，交给它跑，它逐用例告诉你过没过。

用法：
    python judge.py list                      列出所有可判的题目
    python judge.py langs                     看哪些语言的编译器可用
    python judge.py show 1-两数之和            看题面、函数签名、用例数
    python judge.py stub 1-两数之和            生成解题模板 my_solution.py
    python judge.py run 1-两数之和 my.py        判题
    python judge.py run 1-两数之和 my.py -v     判题并显示每个用例的输入输出
    python judge.py selfcheck                 用参考解法验证全部用例（检查用例本身是否可信）
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from harness import (  # noqa: E402
    VERDICT_LABEL,
    load_testcases,
    run_all,
    selfcheck,
)
from languages import LANGUAGE_LABELS, language_status  # noqa: E402

MAX_SHOW = 6      # show 时最多展示几个用例
MAX_LINE = 160    # 单行预览的最大长度


def brief(value, limit=MAX_LINE):
    """把用例的值压成一行短预览"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def find_problem(problems, key):
    """支持题号前缀匹配：'1' 也能找到 '1-两数之和'；'两数之和' 也能找到"""
    if key in problems:
        return key, problems[key]

    matches = [(pid, spec) for pid, spec in problems.items() if pid.startswith(key + "-")]
    if len(matches) == 1:
        return matches[0]

    matches = [(pid, spec) for pid, spec in problems.items() if key in pid]
    if len(matches) == 1:
        return matches[0]

    if not matches:
        print("找不到题目：%s" % key)
        print("提示：用 `python judge.py list` 看全部题目")
    else:
        print("匹配到多道题，请写完整：")
        for pid, _ in matches:
            print("   " + pid)
    return None, None


def cmd_list(problems, args):
    if not problems:
        print("没有用例文件。先确认 testcases.json 存在。")
        return 1

    groups = {}
    for pid, spec in sorted(problems.items(),
                            key=lambda kv: int(re.match(r"\d+", kv[0]).group()) if re.match(r"\d+", kv[0]) else 99999):
        number = re.match(r"(\d+)", pid)
        bucket = (int(number.group(1)) // 100) * 100 if number else 0
        groups.setdefault(bucket, []).append((pid, spec))

    print("=" * 76)
    print("  可判题目：%d 道（共 %d 个用例）" % (
        len(problems), sum(len(s.get("cases", [])) for s in problems.values())))
    print("=" * 76)
    for bucket in sorted(groups):
        for pid, spec in groups[bucket]:
            tag = "设计题" if spec.get("kind") == "operations" else "函数题"
            print("  %-32s %-2s %-6s %2d 用例  %s" % (
                pid, spec.get("level", ""), tag, len(spec.get("cases", [])),
                spec.get("entry", "")))
    print()
    langs = [i["label"] for i in language_status().values() if i["available"]]
    print("可用语言：%s（用 -l 指定，或按文件后缀自动识别）" % "、".join(langs))
    print("用法：python judge.py stub <题目>   生成解题模板")
    return 0


def cmd_show(problems, args):
    pid, spec = find_problem(problems, args.problem)
    if not pid:
        return 1

    doc = os.path.join(HERE, "..", "03-题目详解", pid + ".md")

    print("=" * 76)
    print("  %s" % pid)
    print("=" * 76)
    if spec.get("leetcode"):
        print("力扣链接：%s" % spec["leetcode"])
    print("入口：%s" % spec.get("entry", ""))
    if spec.get("kind") == "operations":
        print("类型：设计题（实现一个类，判题器会按操作序列调用它）")
    else:
        print("参数：%s" % ", ".join(spec.get("params", [])))
        if spec.get("types"):
            print("数据结构：%s" % ", ".join("%s=%s" % kv for kv in spec["types"].items()))
        if spec.get("returnType"):
            print("返回值类型：%s" % spec["returnType"])
        if spec.get("inplace"):
            print("注意：原地修改题 —— 判题器比对的是被改动的参数，不是返回值")
    print("用例：%d 个" % len(spec.get("cases", [])))
    print()

    for index, case in enumerate(spec.get("cases", [])[:MAX_SHOW]):
        if spec.get("kind") == "operations":
            head = "前 %d 个操作" % min(4, len(case["ops"]))
            print("  %d. %s  期望 %s" % (index + 1, head, brief(case.get("expected"))))
        else:
            print("  %d. %s" % (index + 1, brief(case.get("input"))))
            print("     期望 %s" % brief(case.get("expected")))
        if case.get("note"):
            print("     说明 %s" % case["note"])
        if case.get("compare"):
            print("     比较方式 %s" % case["compare"])
    if len(spec.get("cases", [])) > MAX_SHOW:
        print("  … 其余 %d 个用例未展示" % (len(spec["cases"]) - MAX_SHOW))

    print()
    if os.path.isfile(doc):
        print("完整题解：05-算法面试/03-题目详解/%s.md" % pid)
    print("生成模板：python judge.py stub %s" % pid)
    return 0


STUB_TEMPLATE = '''"""
{pid}

{note}
"""


def {entry}({params}):
    # 在这里写你的解法
{body}
'''

STUB_OPS = '''"""
{pid}

{note}
"""


class {entry}:
    def __init__({init_params}):
        # 在这里初始化你的数据结构
        pass

{methods}
'''


def cmd_stub(problems, args):
    pid, spec = find_problem(problems, args.problem)
    if not pid:
        return 1

    out_path = args.output or "my_solution.py"
    if os.path.isfile(out_path) and not args.force:
        print("%s 已存在。要覆盖请加 --force" % out_path)
        return 1

    note = "力扣链接：%s" % spec["leetcode"] if spec.get("leetcode") else ""

    if spec.get("kind") == "operations":
        ops = spec["cases"][0]["ops"] if spec.get("cases") else []
        first = ops[0] if ops else [spec.get("entry", "Class"), []]
        init_params = ", ".join("arg%d" % i for i in range(len(first[1])))
        methods_seen = []
        for op in ops[1:]:
            if op[0] not in methods_seen:
                methods_seen.append(op[0])
        methods = "\n".join(
            "    def %s(self, *args):\n        # 在这里实现\n        pass\n" % m
            for m in methods_seen) or "    # 按题目要求实现各个方法\n"
        text = STUB_OPS.format(pid=pid, note=note, entry=spec.get("entry", "Solution"),
                               init_params=init_params, methods=methods)
    else:
        params = ", ".join(spec.get("params", []))
        body = "".join("    # %s = ?\n" % p for p in spec.get("params", []))
        if body:
            body += "\n"
        text = STUB_TEMPLATE.format(pid=pid, note=note, entry=spec.get("entry", "solve"),
                                    params=params, body=body or "    pass\n")

    with io.open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("已生成 %s" % out_path)
    print("写完后运行：python judge.py run %s %s" % (pid, out_path))
    return 0


EXTENSION_LANGUAGE = {".py": "python", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
                      ".c++": "cpp"}


def guess_language(path, explicit=None):
    """按显式指定或文件后缀判断语言。"""
    if explicit:
        return explicit.lower()
    _, ext = os.path.splitext(path)
    return EXTENSION_LANGUAGE.get(ext.lower(), "python")


def cmd_run(problems, args):
    pid, spec = find_problem(problems, args.problem)
    if not pid:
        return 1
    if not os.path.isfile(args.solution):
        print("找不到解法文件：%s" % args.solution)
        return 1

    language = guess_language(args.solution, args.lang)
    if language not in LANGUAGE_LABELS:
        print("不支持的语言：%s（可选：%s）" % (language, "、".join(LANGUAGE_LABELS)))
        return 1
    status = language_status().get(language, {})
    if not status.get("available"):
        print("%s 不可用：%s" % (status.get("label", language), status.get("reason", "")))
        return 1

    with io.open(args.solution, encoding="utf-8") as handle:
        code = handle.read()

    print("=" * 76)
    print("  判题：%s  ←  %s（%s）" % (pid, args.solution, LANGUAGE_LABELS[language]))
    if spec.get("leetcode"):
        print("  力扣链接：%s" % spec["leetcode"])
    print("=" * 76)

    # -v 时不边跑边打（否则要等全部跑完才知道顺序），跑完统一按顺序输出；
    # 不开 -v 时只打印没过的用例 —— 那才是需要立刻看到的信息。
    def on_case(outcome):
        if not args.verbose and outcome["status"] != "pass":
            print(_format_case(outcome, spec))

    report = run_all(code, spec, timeout=args.timeout, on_case=on_case, language=language)

    if args.verbose:
        for item in report["results"]:
            print(_format_case(item, spec))

    print("-" * 76)
    print("  结果：%s   %d/%d 通过   总耗时 %.2f 秒   峰值内存 %.1f MB" % (
        VERDICT_LABEL[report["verdict"]], report["passed"], report["total"],
        report["ms"] / 1000, (report.get("memoryKB") or 0) / 1024))
    print("=" * 76)
    return 0 if report["verdict"] == "accepted" else 1


def _format_case(outcome, spec, verbose=False):
    mark = {"pass": "通过", "fail": "答案错误", "error": "运行时错误",
            "timeout": "超时", "syntax": "语法错误", "entry": "找不到函数"}.get(outcome["status"], outcome["status"])
    label = "  用例 %d" % outcome["index"]
    line = "%-10s %s" % (label, mark)
    if outcome.get("note"):
        line += "  （%s）" % outcome["note"]
    if outcome["status"] != "pass":
        if outcome.get("error"):
            line += "\n             %s" % outcome["error"][:300]
        else:
            line += "\n             得到 %s" % brief(outcome.get("actual"))
            line += "\n             期望 %s" % brief(outcome.get("expected"))
    return line


def main() -> int:
    parser = argparse.ArgumentParser(
        description="命令行判题工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python judge.py run 1-两数之和 my.py -v")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有可判的题目")
    sub.add_parser("selfcheck", help="用参考解法验证全部用例")

    p_show = sub.add_parser("show", help="看题目信息与用例")
    p_show.add_argument("problem")

    p_stub = sub.add_parser("stub", help="生成解题模板")
    p_stub.add_argument("problem")
    p_stub.add_argument("-o", "--output", help="输出文件名，默认 my_solution.py")
    p_stub.add_argument("--force", action="store_true", help="覆盖已存在的文件")

    p_run = sub.add_parser("run", help="判题")
    p_run.add_argument("problem")
    p_run.add_argument("solution")
    p_run.add_argument("-v", "--verbose", action="store_true", help="显示全部用例的输入输出")
    p_run.add_argument("--timeout", type=float, default=6.0, help="单用例超时秒数，默认 6")
    p_run.add_argument("-l", "--lang", default=None,
                       help="语言：python / cpp。不给就按文件后缀猜（.py / .cpp / .cc）")

    sub.add_parser("langs", help="看各语言的编译器是否可用")

    args = parser.parse_args()

    if args.command == "selfcheck":
        return selfcheck()

    problems = load_testcases()
    if not problems:
        print("读不到用例：%s" % os.path.join(HERE, "testcases.json"))
        return 2

    if args.command == "langs":
        for info in language_status().values():
            print("%-10s %-5s %s" % (info["label"],
                                     "可用" if info["available"] else "不可用",
                                     info["version"] or info["reason"]))
        return 0

    handlers = {"list": cmd_list, "show": cmd_show, "stub": cmd_stub, "run": cmd_run}
    if args.command in handlers:
        return handlers[args.command](problems, args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
