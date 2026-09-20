"""
判题内核：跑用户代码、逐用例判定、汇总判决。

它做三件事：
    1. 把用户代码交给对应语言的**后端**（languages.py）去准备、编译、执行
    2. 比对输出、统计用时与峰值内存
    3. 汇总成一份判决报告

隔离是怎么做的
--------------
用户代码是不可信的：死循环、递归爆栈、sys.exit()、把 stdout 打满都可能发生。
所以每次提交都在**临时目录**里跑**子进程**，带超时和输出上限，跑完删目录。
一次提交的失败绝不会影响下一次。

为什么内存上限抓的是"吃爆"而不是抠几 MB
-----------------------------------------
报的是进程峰值工作集，包含语言运行时自身的开销（Python 解释器 10~15 MB，
C++ 程序 3~4 MB）。所以默认上限给到 256 MB —— 它抓的是"开了个巨大的表"，
不是"多用了两兆"。

作为库用：
    from harness import run_all, load_testcases
    report = run_all(code, problem, language="cpp")

自检（验证用例本身是否可信）：
    python harness.py --selfcheck
    python harness.py --langs
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from languages import (  # noqa: E402
    _compare,
    get_backend,
    language_status,
)

DEFAULT_TESTCASES = os.path.join(HERE, "testcases.json")

#: 单个用例的墙钟上限（秒）。力扣是 10 秒，这里给 6 秒更贴近"复杂度是否达标"的直觉。
DEFAULT_TIMEOUT = 6.0

#: 峰值内存上限（KB）。含语言运行时自身开销，见模块 docstring。
DEFAULT_MEMORY_LIMIT_KB = 256 * 1024

#: 用户程序 stdout 上限，防止 print 刷屏把内存打爆
MAX_OUTPUT = 200_000


# ---------------------------------------------------------------------------
# 判决
# ---------------------------------------------------------------------------

VERDICT_LABEL = {
    "accepted": "通过",
    "partial": "部分通过",
    "wrong_answer": "答案错误",
    "runtime_error": "运行时错误",
    "timeout": "超时",
    "memory_limit": "内存超限",
    "compile_error": "编译错误",
}


def compare(actual, expected, mode="exact"):
    """判定实际输出是否等于期望（实现在 languages.py，各语言共用）。"""
    return _compare(actual, expected, mode)


def _summarize(results, cases):
    """从逐用例结果归纳出整场判决。按严重程度取最重的那个。"""
    if not cases:
        return "accepted"

    if any(r["status"] in ("syntax", "entry") for r in results):
        return "compile_error"
    if any(r["status"] == "timeout" for r in results):
        return "timeout"
    if any(r["status"] == "memory" for r in results):
        return "memory_limit"
    if any(r["status"] == "error" for r in results):
        return "runtime_error"

    passed = sum(1 for r in results if r["status"] == "pass")
    if passed == len(cases):
        return "accepted"
    return "partial" if passed else "wrong_answer"


def _compile_error_report(prep, cases):
    """编译失败时，每个用例都返回同一条错误 —— 学员要看到的正是编译错误本身。"""
    results = []
    for index, case in enumerate(cases):
        results.append({"index": index + 1, "status": "syntax",
                        "error": prep.get("error", "编译失败"), "ms": 0,
                        "memoryKB": 0, "note": case.get("note", "")})
    return {"verdict": "compile_error", "passed": 0, "total": len(cases),
            "results": results, "ms": 0.0, "memoryKB": 0,
            "compileError": prep.get("error", ""),
            "compileDetail": prep.get("detail", "")}


def _finish_report(results, cases):
    passed = sum(1 for r in results if r["status"] == "pass")
    return {
        "verdict": _summarize(results, cases),
        "passed": passed,
        "total": len(cases),
        "results": results,
        "ms": round(sum(r.get("ms", 0) for r in results), 2),
        "memoryKB": max([r.get("memoryKB", 0) for r in results] + [0]),
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------

def run_case(code: str, problem: dict, case: dict, timeout: float = DEFAULT_TIMEOUT,
             memory_limit_kb: int = DEFAULT_MEMORY_LIMIT_KB,
             language: str = "python") -> dict:
    """
    跑**单个**用例（每次都要重新准备/编译，适合一次性调用）。

    要跑完一个题目的全部用例，用 run_all —— 它只编译一次。
    """
    backend = get_backend(language)
    workdir = tempfile.mkdtemp(prefix="azto_judge_")
    try:
        prep = backend.prepare(code, problem, workdir)
        if not prep["ok"]:
            return {"status": "syntax", "ms": 0, "memoryKB": 0,
                    "error": prep.get("error", "编译失败")}
        return backend.run_case(workdir, problem, case, 0, timeout, memory_limit_kb)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_all(code: str, problem: dict, timeout: float = DEFAULT_TIMEOUT, on_case=None,
            memory_limit_kb: int = DEFAULT_MEMORY_LIMIT_KB, language: str = "python") -> dict:
    """
    跑完一个题目的全部用例。

    编译型语言**只编译一次**，之后每个用例只是一次进程启动 ——
    不这样做的话一次提交要等 6~8 次 g++，学员根本没法迭代。

    一旦出现 fail/error/timeout **不早停**：把所有用例的结果都给出来，
    学员才能看出"是边界没过还是整体思路就错了"。
    """
    cases = problem.get("cases", [])
    if not cases:
        return {"verdict": "accepted", "passed": 0, "total": 0, "results": [],
                "ms": 0.0, "memoryKB": 0}

    backend = get_backend(language)
    workdir = tempfile.mkdtemp(prefix="azto_judge_")
    try:
        prep = backend.prepare(code, problem, workdir)
        if not prep["ok"]:
            report = _compile_error_report(prep, cases)
            if on_case:
                for item in report["results"]:
                    on_case(item)
            return report

        results = []
        for index, case in enumerate(cases):
            outcome = backend.run_case(workdir, problem, case, index, timeout, memory_limit_kb)
            outcome["index"] = index + 1
            outcome["note"] = case.get("note", "")
            results.append(outcome)
            if on_case:
                on_case(outcome)
        return _finish_report(results, cases)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 用例加载与自检
# ---------------------------------------------------------------------------

def load_testcases(path: str = DEFAULT_TESTCASES) -> dict:
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle).get("problems", {})


def selfcheck(path: str = DEFAULT_TESTCASES, only=None, verbose: bool = True) -> int:
    """
    自检：用题目里存的 `reference` 参考解法跑全部用例。

    这是**用例本身是否可信**的唯一保证 —— 参考解法都过不了的用例，
    要么用例写错了，要么函数的入参约定写错了。

    参考解法都是 Python，所以自检只覆盖 Python。其他语言的运行时由
    tests/ 里的多语言测试单独覆盖（那些测试会真的编译并跑 C++ 解法）。
    """
    problems = load_testcases(path)
    if not problems:
        print("没有找到用例文件：%s" % path)
        return 2

    bad, checked, skipped = [], 0, 0
    for pid, problem in sorted(problems.items()):
        if only and pid not in only:
            continue
        reference = problem.get("reference")
        if not reference:
            skipped += 1
            continue
        checked += 1
        report = run_all(reference, problem, language="python")
        if report["verdict"] != "accepted":
            bad.append((pid, report))
            if verbose:
                print("[FAIL] %-34s %s (%d/%d)" % (
                    pid, VERDICT_LABEL[report["verdict"]], report["passed"], report["total"]))
                for item in report["results"]:
                    if item["status"] != "pass":
                        detail = item.get("error") or "得到 %s，期望 %s" % (
                            item.get("actual"), item.get("expected"))
                        print("         用例 %d %s：%s" % (
                            item["index"], item["status"], str(detail)[:160]))
        elif verbose:
            print("[ OK ] %-34s %d/%d" % (pid, report["passed"], report["total"]))

    print()
    print("=" * 68)
    print("自检完成：%d 题用参考解法验证通过，%d 题不通过，%d 题无用例/参考解法" % (
        checked - len(bad), len(bad), skipped))
    print("=" * 68)
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="在线判题运行时内核 / 用例自检")
    parser.add_argument("--selfcheck", action="store_true", help="用参考解法验证全部用例")
    parser.add_argument("--only", nargs="*", help="只自检这些题（题号-题名）")
    parser.add_argument("--quiet", action="store_true", help="自检时只输出失败的")
    parser.add_argument("--file", default=DEFAULT_TESTCASES, help="用例文件路径")
    parser.add_argument("--langs", action="store_true", help="打印各语言编译器的可用性")
    args = parser.parse_args()

    if args.langs:
        for info in language_status().values():
            print("%-10s %-5s %s" % (info["label"],
                                     "可用" if info["available"] else "不可用",
                                     info["version"] or info["reason"]))
        return 0

    if args.selfcheck:
        return selfcheck(args.file, only=args.only, verbose=not args.quiet)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
