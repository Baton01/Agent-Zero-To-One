"""
把 cases/ 下的四组用例文件合并成 testcases.json。

为什么要分组合并，而不是直接编辑 6MB 的 testcases.json：
    单个用例文件小得多（几十 KB 到 3 MB），改一道题只用动对应那一组；
    合并时按"用例更完整的那份优先"处理，重复的题不会被覆盖成更差的版本。

用法：
    python merge_cases.py              # 合并并写入 testcases.json
    python merge_cases.py --check      # 只报告会合并出什么，不写文件
    python merge_cases.py --selfcheck  # 合并后立刻用参考解法验证全部用例

改完用例后**务必**跑一次 --selfcheck —— 用例错了比没有用例更糟。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.path.join(HERE, "cases")
OUTPUT = os.path.join(HERE, "testcases.json")

#: 作者文件的顺序。靠前的优先（同样一道题，用例更多的版本胜出）。
SOURCES = ["part-a.json", "part-b.json", "part-c.json", "part-d.json"]

#: 题解目录，用来补 LeetCode 链接与难度
SOLUTIONS_DIR = os.path.join(HERE, "..", "03-题目详解")

LINK_RE = re.compile(r"\*\*LeetCode 链接[：:]\*\*\s*(\S+)")
LEVEL_RE = re.compile(r"^#\s*\d+\.\s*.+?\((Easy|Medium|Hard)\)", re.M)


def problem_number(problem_id):
    match = re.match(r"^(\d+)", problem_id)
    return int(match.group(1)) if match else 999999


def merge(sources):
    """按优先级合并。同一道题保留用例更多的那份。"""
    merged = {}
    report = []
    for filename in sources:
        path = os.path.join(CASES_DIR, filename)
        if not os.path.isfile(path):
            report.append((filename, 0, 0, "不存在"))
            continue
        problems = (json.loads(io.open(path, encoding="utf-8").read()) or {}).get("problems", {})
        added = replaced = 0
        for problem_id, spec in problems.items():
            count = len(spec.get("cases", []))
            if problem_id not in merged:
                merged[problem_id] = spec
                added += 1
            elif count > len(merged[problem_id].get("cases", [])):
                merged[problem_id] = spec
                replaced += 1
        report.append((filename, added, replaced, ""))
    return merged, report


def enrich(problems):
    """从题解里补 LeetCode 链接和难度"""
    linked = leveled = 0
    for problem_id, spec in problems.items():
        spec.setdefault("leetcode", "")
        spec.setdefault("level", "")
        path = os.path.join(SOLUTIONS_DIR, problem_id + ".md")
        if not os.path.isfile(path):
            continue
        text = io.open(path, encoding="utf-8").read()
        match = LINK_RE.search(text)
        if match:
            spec["leetcode"] = match.group(1).strip()
            linked += 1
        match = LEVEL_RE.search(text)
        if match:
            spec["level"] = match.group(1)
            leveled += 1
    return linked, leveled


def main() -> int:
    parser = argparse.ArgumentParser(description="合并用例作者文件")
    parser.add_argument("--check", action="store_true", help="只报告，不写文件")
    parser.add_argument("--selfcheck", action="store_true", help="合并后立刻用参考解法验证")
    args = parser.parse_args()

    problems, report = merge(SOURCES)
    linked, leveled = enrich(problems)
    problems = {k: problems[k] for k in sorted(problems, key=problem_number)}

    total_cases = sum(len(s.get("cases", [])) for s in problems.values())

    print("=" * 68)
    for filename, added, replaced, note in report:
        if note:
            print("  %-18s %s" % (filename, note))
        else:
            print("  %-18s 新增 %2d 题，因用例更多而覆盖 %d 题" % (filename, added, replaced))
    print("-" * 68)
    print("  合计 %d 题 / %d 个用例" % (len(problems), total_cases))
    print("  补出力扣链接 %d 题，补出难度 %d 题" % (linked, leveled))

    missing_link = [pid for pid, spec in problems.items() if not spec.get("leetcode")]
    if missing_link:
        print("  [注意] 这些题没找到力扣链接：%s" % "、".join(missing_link[:5]))
    print("=" * 68)

    if args.check:
        return 0

    payload = {"$schema": "azto-judge-testcases-v1", "problems": problems}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    with io.open(OUTPUT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("已写入 %s（%.1f MB）" % (OUTPUT, len(text.encode("utf-8")) / 1024 / 1024))

    if args.selfcheck:
        print()
        sys.path.insert(0, HERE)
        from harness import selfcheck
        return selfcheck(OUTPUT)

    print()
    print("下一步（务必做）：python harness.py --selfcheck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
