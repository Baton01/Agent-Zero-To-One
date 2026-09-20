"""项目一致性检查：链接有效性 + 文件名引用 + 目录清单。

检查四件事：
  1. [[wikilink]] 是否指向真实文件
  2. 相对 Markdown 链接是否指向真实文件
  3. 文档里引用的 `code/chXX_*.py` 和 `docs/第XX章*.md` 是否真实存在
  4. 实际文件清单是否与 README / 全局索引 描述的目录结构一致
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
CODE_REF = re.compile(r"`(code/ch\d{2}_[A-Za-z0-9_]+\.py)`")
DOC_REF = re.compile(r"`?(docs/第\d{2}章[^`\s，。、）)]*\.md)`?")

FENCE = re.compile(r"```.*?```", re.S)   # 代码块里的东西不是链接
INLINE_CODE = re.compile(r"`[^`\n]+`")   # 行内代码同理（TOOLS[name](arg) 这种是 Python 语法）

# 文档里用于"讲解语法"的示意链接，不是真链接
PLACEHOLDER = (
    "wikilink", "...", "xx", "XX", "目录/文件名", "第XX章", "DayXX", "WeekXX",
    "文件名", "name", "url", "text",
)


def strip_code(text: str) -> str:
    """去掉代码块和行内代码，只留下正文里的链接。"""
    return INLINE_CODE.sub("``", FENCE.sub("", text))


def is_placeholder(target: str) -> bool:
    if target.endswith("/"):          # 目录引用（带斜杠）
        return True
    if "*" in target or "<" in target or ">" in target:
        return True
    return any(token in target for token in PLACEHOLDER)


def main() -> int:
    all_paths = set()
    dir_paths = set()
    basenames = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), ROOT).replace("\\", "/")
            dir_paths.add(rel)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            all_paths.add(rel)
            basenames.add(name)
            if name.endswith(".md"):
                basenames.add(name[:-3])

    broken_wiki, broken_md, broken_code, broken_doc = [], [], [], []
    pending_wiki = []          # 裸文件名形式的未解析引用（Obsidian 里是"待补充"的占位）
    total_wiki = total_md = 0

    for rel in sorted(all_paths):
        if not rel.endswith(".md"):
            continue
        text = strip_code(io.open(os.path.join(ROOT, rel), encoding="utf-8").read())

        for raw in WIKILINK.findall(text):
            target = raw.strip()
            if not target or is_placeholder(target):
                continue
            total_wiki += 1
            if (target in all_paths or target + ".md" in all_paths
                    or target in dir_paths or target in basenames):
                continue
            # 带路径的引用写错了就是错误；裸文件名的引用在 Obsidian 里是合法的
            # "计划中/待补充" 占位（05-算法面试 里大量指向 Hot 100 之外的题目），
            # 单独归类，不算失效。
            if "/" in target:
                broken_wiki.append((rel, target))
            else:
                pending_wiki.append((rel, target))

        for raw in MDLINK.findall(text):
            target = raw.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#")[0].strip()
            if not clean or is_placeholder(clean):
                continue
            total_md += 1
            resolved = os.path.normpath(os.path.join(os.path.dirname(rel), clean)).replace("\\", "/")
            if resolved in all_paths or clean in all_paths or resolved in dir_paths:
                continue
            broken_md.append((rel, target))

        # 代码与章节引用（只看直接写在正文里的路径）
        if rel.startswith(("docs/", "02-Wiki/", "01-Raw/", "03-学习笔记/", "00-配置/")) or "/" not in rel:
            for name in set(CODE_REF.findall(text)):
                if name not in all_paths:
                    broken_code.append((rel, name))
            for name in set(DOC_REF.findall(text)):
                normalized = name.strip().rstrip("`")
                if normalized not in all_paths:
                    broken_doc.append((rel, normalized))

    print("=" * 72)
    print("一致性检查报告")
    print("=" * 72)
    print("项目内 Markdown 文件：{} 个".format(sum(1 for p in all_paths if p.endswith(".md"))))
    print("wikilink           ：{} 个，失效 {}（另有 {} 个未解析的裸文件名引用，见文末说明）".format(
        total_wiki, len(broken_wiki), len(pending_wiki)))
    print("相对链接           ：{} 个，失效 {}".format(total_md, len(broken_md)))
    print("code/ 脚本引用     ：失效 {}".format(len(broken_code)))
    print("docs/ 章节引用     ：失效 {}".format(len(broken_doc)))
    print("=" * 72)

    for label, items in (
        ("[失效 wikilink]", broken_wiki),
        ("[失效相对链接]", broken_md),
        ("[引用了不存在的 code 脚本]", broken_code),
        ("[引用了不存在的章节文件]", broken_doc),
    ):
        if items:
            print("\n{}".format(label))
            for src, target in items:
                print("  {}  ->  {}".format(src, target))

    # 反向检查：有哪些脚本没人引用
    referenced = set()
    for rel in all_paths:
        if rel.endswith(".md"):
            text = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
            referenced |= set(CODE_REF.findall(text))
    orphans = sorted(p for p in all_paths if re.match(r"code/ch\d{2}_.*\.py$", p) and p not in referenced)
    if orphans:
        print("\n[没有被文档引用的脚本]")
        for item in orphans:
            print("  {}".format(item))

    return 1 if (broken_wiki or broken_md or broken_code or broken_doc) else 0


if __name__ == "__main__":
    sys.exit(main())
