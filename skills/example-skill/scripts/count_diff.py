#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""count_diff.py —— 统计一份 unified diff 的改动范围（纯标准库，Python 3.9+）。

为什么要有这个脚本：
    "这次改动涉及几个文件、加了多少行"是确定性计算，不该让模型去"看"。跑脚本更便宜、更准，
    而且结果可以被后面的步骤断言。

用法：
    python scripts/count_diff.py --diff a.patch            # 从文件读
    git diff | python scripts/count_diff.py --stdin        # 从标准输入读
    python scripts/count_diff.py --diff a.patch --json     # 机器可读输出
    python scripts/count_diff.py --diff big.patch --max-lines 2000
                                                          # 超过阈值时退出码为 2（用于"大 diff 降级"判断）

退出码：
    0  正常（或未超过 --max-lines）
    1  参数/输入错误（文件不存在、内容不是 diff）
    2  改动行数超过 --max-lines（只在使用该参数时出现）

支持 git 风格（含 `diff --git` 头）与普通 unified diff（只有 `--- / +++` 头）。
不解析二进制 diff 的内容，只标记为 binary。
"""

import argparse
import io
import json
import os
import sys

HIGH_RISK_SUFFIXES = (
    ".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".sh", ".bash", ".sql", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
)


def new_entry(path):
    """一条文件级统计记录。"""
    return {
        "path": path,
        "added": 0,
        "removed": 0,
        "hunks": 0,
        "is_new": False,
        "is_deleted": False,
        "binary": False,
    }


def clean_path(raw):
    """去掉 diff 头里的前缀（a/ b/）、时间戳与引号。"""
    text = raw.strip().strip('"')
    if not text:
        return "(unknown)"
    # 普通 unified diff 可能带制表符分隔的时间戳：--- a/x.c\t2026-01-01 ...
    text = text.split("\t")[0].strip()
    if text == "/dev/null":
        return "/dev/null"
    if text.startswith("a/") or text.startswith("b/"):
        return text[2:]
    return text


def path_from_git_header(line):
    """从 `diff --git a/x b/y` 里取路径。"""
    parts = line.split()
    if len(parts) >= 4:
        return clean_path(parts[-1])
    return "(unknown)"


def looks_like_diff(text):
    for line in text.splitlines()[:200]:
        if line.startswith("diff --git ") or line.startswith("@@ "):
            return True
        if line.startswith("--- ") and "+++ " in text:
            return True
    return False


def parse_diff(text):
    """把 diff 文本解析成文件级统计列表。"""
    lines = text.splitlines()
    entries = []
    current = None
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]

        if line.startswith("diff --git "):
            current = new_entry(path_from_git_header(line))
            entries.append(current)
        elif line.startswith("Binary files ") and " and " in line:
            # 没有 `diff --git` 头的二进制段：自己开一条，避免把标记记到上一个文件上
            target = line[len("Binary files "):].split(" and ")[-1].strip()
            if target.endswith(" differ"):
                target = target[:-len(" differ")].strip()
            if current is None or current["path"] != clean_path(target):
                current = new_entry(clean_path(target))
                entries.append(current)
            current["binary"] = True
        elif line.startswith("--- ") and index + 1 < total and lines[index + 1].startswith("+++ "):
            old_path = clean_path(line[4:])
            new_path = clean_path(lines[index + 1][4:])
            if current is None:
                current = new_entry(new_path)
                entries.append(current)
            if old_path == "/dev/null":
                current["is_new"] = True
            if new_path == "/dev/null":
                current["is_deleted"] = True
            index += 1
        elif line.startswith("@@ "):
            if current is not None:
                current["hunks"] += 1
        elif current is not None:
            if line.startswith("new file mode"):
                current["is_new"] = True
            elif line.startswith("deleted file mode"):
                current["is_deleted"] = True
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current["binary"] = True
            elif line.startswith("+++ ") or line.startswith("--- "):
                # 头部已被上面处理；这里只可能是内容行形如 "--- xxx"，按删除行计
                if not (index + 1 < total and lines[index + 1].startswith("+++ ")):
                    current["removed"] += 1
            elif line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["removed"] += 1

        index += 1

    kept = []
    for entry in entries:
        if entry["added"] or entry["removed"] or entry["hunks"] or entry["binary"] \
                or entry["is_new"] or entry["is_deleted"]:
            kept.append(entry)
    return kept


def change_kind(entry):
    """返回 新增 / 删除 / 二进制 / 修改。"""
    if entry["binary"]:
        return "二进制"
    if entry["is_new"]:
        return "新增"
    if entry["is_deleted"]:
        return "删除"
    return "修改"


def display_width(text):
    """中文等全角字符按 2 列计算，便于表格对齐。"""
    width = 0
    for char in text:
        if unicodedata_east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def unicodedata_east_asian_width(char):
    """延迟导入 unicodedata，避免在顶部堆积无关注释；行为等价于 unicodedata.east_asian_width。"""
    import unicodedata
    return unicodedata.east_asian_width(char)


def pad(text, width):
    """按显示宽度左侧对齐补空格。"""
    return text + " " * max(0, width - display_width(text))


def render_table(entries):
    """渲染成可直接粘贴进报告的对齐表格。"""
    headers = ["文件", "新增", "删除", "净变化", "块", "类型"]
    rows = []
    for entry in entries:
        rows.append([
            entry["path"],
            str(entry["added"]),
            str(entry["removed"]),
            "%+d" % (entry["added"] - entry["removed"]),
            str(entry["hunks"]),
            change_kind(entry),
        ])

    widths = [display_width(h) for h in headers]
    for row in rows:
        for position, cell in enumerate(row):
            widths[position] = max(widths[position], display_width(cell))

    lines = []
    lines.append("  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        lines.append("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)))

    total_added = sum(e["added"] for e in entries)
    total_removed = sum(e["removed"] for e in entries)
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    lines.append("合计 %d 个文件：+%d / -%d（共 %d 行改动）"
                 % (len(entries), total_added, total_removed, total_added + total_removed))

    risky = [e["path"] for e in entries
             if any(e["path"].endswith(suffix) for suffix in HIGH_RISK_SUFFIXES)]
    if risky:
        lines.append("提示：以下文件涉及代码/配置，建议按高风险路径优先审查：%s" % ", ".join(risky[:10]))
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(
        description="统计 unified diff 的改动范围（文件数 / 增删行数 / hunk 数）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/count_diff.py --diff a.patch\n"
               "  git diff | python scripts/count_diff.py --stdin --json\n",
    )
    parser.add_argument("--diff", help="diff 文件路径（.patch / .diff）")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取 diff（如 git diff | ...）")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于程序继续处理")
    parser.add_argument("--max-lines", type=int, default=0,
                        help="改动行数上限；超出时退出码为 2（用于判断是否降级审查）")
    return parser


def read_input(args):
    if args.diff:
        if not os.path.isfile(args.diff):
            sys.stderr.write("找不到文件：%s\n" % args.diff)
            return None
        with io.open(args.diff, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    sys.stderr.write("从标准输入读取 diff（Ctrl+Z / Ctrl+D 结束）……\n")
    return sys.stdin.read()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    text = read_input(args)
    if text is None:
        return 1
    if not text.strip():
        sys.stderr.write("输入为空：请用 --diff <文件> 或 --stdin 提供 diff 内容。\n")
        return 1
    if not looks_like_diff(text):
        sys.stderr.write("输入看起来不是 unified diff（没有 diff --git / --- +++ / @@ 标记）。\n")
        return 1

    entries = parse_diff(text)
    if not entries:
        sys.stderr.write("没有解析到任何文件改动。\n")
        return 1

    total_changed = sum(e["added"] + e["removed"] for e in entries)

    if args.json:
        payload = {
            "files": entries,
            "summary": {
                "file_count": len(entries),
                "added": sum(e["added"] for e in entries),
                "removed": sum(e["removed"] for e in entries),
                "changed": total_changed,
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_table(entries) + "\n")

    if args.max_lines and total_changed > args.max_lines:
        sys.stderr.write("改动 %d 行，超过上限 %d 行：建议先缩小范围或走降级审查。\n"
                         % (total_changed, args.max_lines))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
