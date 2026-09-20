"""
网页构建脚本：把项目里的 Markdown 抽取成网页数据。

为什么用 Python 生成 JS 而不是运行时 fetch：
    浏览器在 file:// 协议下禁止 fetch 本地文件（CORS）。所以网页要能"双击直接打开"，
    内容就必须在构建期嵌进 .js 文件里，用 <script src> 加载 —— 这条路不受 CORS 限制。

输出两个内容包（分开是为了首屏快）：
    web/content.js        主内容：17 章教程 + 知识库 + 笔记 + 规划 + 模拟面试题库（约 1.5 MB）
    web/content-algo.js   算法轨道：13 专题 + 100 题目详解 + 14 天笔记（约 1.4 MB，按需懒加载）

用法：
    python web/_build.py          # 生成两个内容包
    python web/_build.py --check  # 只报告解析结果，不写文件
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

# ---------------------------------------------------------------------------
# 结构定义：这些是"课程设计"，不是从正文里猜的，所以写死在脚本里
# ---------------------------------------------------------------------------

STAGES = [
    {"id": "s1", "name": "起步", "weeks": "第 1 周", "chapters": ["00", "01", "02"],
     "desc": "先有能跑的体感，再建立判断力与度量单位"},
    {"id": "s2", "name": "单 Agent 核心", "weeks": "第 2–4 周", "chapters": ["03", "04", "05", "06"],
     "desc": "从会聊天到会开发，手写一个能自转的 Agent 循环"},
    {"id": "s3", "name": "能力扩展", "weeks": "第 5–8 周", "chapters": ["07", "08", "09", "10"],
     "desc": "记忆、检索、技能包、协议 —— 让 Agent 撑过 20 步"},
    {"id": "s4", "name": "工程化", "weeks": "第 9–11 周", "chapters": ["11", "12", "13", "14"],
     "desc": "评估、部署、抽象收束：从 demo 走到能上线"},
    {"id": "s5", "name": "实战与毕业", "weeks": "第 12 周及以后", "chapters": ["15", "16"],
     "desc": "三个可交付项目 + 独立毕设 + 面试表达"},
]

#: 周次 → 覆盖章节（与 01-Raw/02-12周速通学习计划.md 一致）
WEEK_CHAPTERS = {
    1: ["00", "01", "02"], 2: ["03"], 3: ["04", "05"], 4: ["06", "07"],
    5: ["08"], 6: ["08"], 7: ["09", "10"], 8: ["11"], 9: ["12"], 10: ["13"],
    11: ["14", "15"], 12: ["16"], 13: [], 14: [], 15: [], 16: [],
}

#: 项目阶梯（与 01-Raw/03-项目阶梯与能力对照表.md 一致）
LADDER = [
    {"id": "L1", "name": "计算器 Agent", "one": "理解自然语言里的计算需求，调用计算工具算出结果",
     "skills": ["最小 tool call loop", "工具 schema 定义", "错误处理"],
     "chapters": ["04", "05"], "scale": "100–150 行",
     "criteria": ["至少 3 个工具", "工具失败时程序不崩溃，错误被喂回模型", "有最大步数限制", "有 README"]},
    {"id": "L2", "name": "网页研究 Agent", "one": "输入主题，自动搜索、筛选、综合，输出带可追溯引用的报告",
     "skills": ["任务拆解", "多步规划", "来源可信度判断", "引用保真"],
     "chapters": ["06", "08", "15"], "scale": "300–500 行",
     "criteria": ["有明确的报告结构", "引用可追溯，编造引用会被拦截", "有失败处理", "给出一次真实运行的产物"]},
    {"id": "L3", "name": "文档问答 Agent", "one": "喂一批文档，用自然语言提问，得到带出处的答案；不知道的会说不知道",
     "skills": ["RAG 全链路", "切片策略", "混合检索", "Rerank", "拒答", "评估"],
     "chapters": ["08", "15"], "scale": "400–700 行",
     "criteria": ["有切片对比实验", "混合检索 + RRF 融合", "引用校验，不合格就拒答", "30 条评测集与三个指标", "bad case 分类统计"]},
    {"id": "L4", "name": "代码审查 Agent", "one": "读一个 git diff，按风险排序给出可执行的修改建议",
     "skills": ["长上下文处理", "风险分类", "误报控制", "结构化输出"],
     "chapters": ["09", "11", "15"], "scale": "300–600 行",
     "criteria": ["大 diff 有降级策略并声明覆盖范围", "建议可执行", "置信度阈值 + 条数上限", "20 个真实 diff 的检出率与误报率"]},
    {"id": "L5", "name": "浏览器 Agent", "one": "只操作公开网页：打开、观察、提取信息、生成摘要",
     "skills": ["页面理解", "元素定位", "失败恢复", "安全边界"],
     "chapters": ["10", "13"], "scale": "300–500 行",
     "criteria": ["页面变化/弹窗/定位失败都有降级路径", "有动作日志可复盘", "安全边界写进 README", "遵守 robots 与访问频率"]},
    {"id": "L6", "name": "Nano Coding Agent", "one": "能读写文件、执行命令、跑测试的编码 Agent —— 迷你版 Claude Code",
     "skills": ["shell 工具", "文件编辑", "权限门", "session", "上下文压缩", "trace"],
     "chapters": ["14"], "scale": "500–900 行",
     "criteria": ["命令执行在白名单内", "危险操作需确认且展示完整参数", "session 可中断恢复", "上下文压缩有前后对比数据", "完整 trace 可复盘"]},
    {"id": "L7", "name": "可复用 Skill Pack", "one": "把一类任务的流程知识打包成可复用、可验证的能力包",
     "skills": ["SKILL.md 设计", "渐进式加载", "效果验证"],
     "chapters": ["09"], "scale": "3–5 个 skill",
     "criteria": ["每个 skill 有清晰 frontmatter", "description 写明做什么/何时用/何时别用", "每个 skill 有验收标准", "A/B smoke test 给出成功率对比"]},
    {"id": "L8", "name": "生产级 Harness", "one": "能上线、可评估、可观测、有权限边界、成本可控的 Agent 运行时",
     "skills": ["全部前七档", "评估体系", "可观测性", "生产工程"],
     "chapters": ["12", "13", "14"], "scale": "1000 行以上",
     "criteria": ["evals + 指标报告 + 回归测试进 CI", "trace 可导出并按失败模式分类", "危险动作白名单 + 人工确认", "单次/单用户/每日三级成本上限", "分层超时 + 分类重试 + 降级 + 幂等", "可中断、可续跑、可回放", "压测或容量报告"]},
]

#: 四个上游项目的能力地图（首页用）
SOURCES = [
    {"name": "LeetCode-BaiTiTong", "repo": "https://github.com/mo-lx/LeetCode-BaiTiTong",
     "layer": "系统层 + 算法轨道", "color": "violet",
     "gives": ["AI 教练系统提示词", "学员档案 / 进度看板 / 学习日志", "13 个算法专题 + 100 题详解 + 14 天路线"],
     "solves": "坚持不下去 · 算法轮过不去",
     "lands": ["学习中枢.md", "00-配置/", "05-算法面试/"]},
    {"name": "Hello-Agents", "repo": "https://github.com/datawhalechina/hello-agents",
     "layer": "内容层", "color": "blue",
     "gives": ["系统化章节教程", "从零造框架", "记忆 / RAG / 上下文 / 协议 / 评估", "综合案例"],
     "solves": "学不会",
     "lands": ["docs/ 17 章", "code/ 17 个脚本", "skills/"]},
    {"name": "LLM-Master", "repo": "https://github.com/youngyangyang04/llm-master",
     "layer": "参考层 + 叙事风格", "color": "green",
     "gives": ["工程视角的专题深度", "生产工程（部署 / 量化 / 压测）", "面试题库与回答框架", "讲稿式叙事模板"],
     "solves": "讲不出",
     "lands": ["02-Wiki/专题总结/", "02-Wiki/速查表/", "02-Wiki/写作风格规范.md"]},
    {"name": "Agent-Learning-Hub", "repo": "https://github.com/datawhalechina/Agent-Learning-Hub",
     "layer": "导航层", "color": "amber",
     "gives": ["阶段化 Todo List", "Project Ladder 项目阶梯", "精选资源地图", "工程判断力"],
     "solves": "走错路",
     "lands": ["01-Raw/路线调研", "01-Raw/12周计划", "01-Raw/项目阶梯"]},
]

# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

CHAPTER_TITLE = re.compile(r"^#\s*第(\d{2})章\s*(.+?)\s*$", re.M)
CHAPTER_GOAL = re.compile(r"\*\*本章目标：\*\*\s*(.+?)(?:\n|$)")
CHAPTER_HOURS = re.compile(r"\*\*预计用时：\*\*\s*([0-9]+(?:\.[0-9]+)?)\s*小时")
CHAPTER_PREREQ = re.compile(r"\*\*前置章节：\*\*\s*(.+?)(?:\s*　|\s*$)")
CHAPTER_CODE = re.compile(r"\*\*配套代码：\*\*\s*`([^`]+)`")

TOPIC_TITLE = re.compile(r"^#\s*专题：\s*(.+?)\s*$", re.M)
SHEET_TITLE = re.compile(r"^#\s*(.+?)\s*$", re.M)
QUOTE_LINE = re.compile(r"^>\s*(.+?)$", re.M)

#: 模拟面试题库的字段 → 内部键名
BANK_FIELDS = {
    "维度": "dimension", "难度": "level", "岗位": "role", "时长": "minutes",
    "考察": "probe", "题面": "prompt", "参考思路": "outline", "加分": "plus",
    "扣分": "minus", "追问": "followups", "关联": "related",
}
BANK_LISTS = {"outline", "plus", "minus", "followups"}


def read(path: str) -> str:
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def split_frontmatter(text: str):
    """
    拆出 YAML frontmatter 和正文。

    返回 (meta, body)。没有 frontmatter 时 meta 为空字典、body 是原文。
    只做 `key: value` 和 `- item` 的简单解析 —— 够用，且不引入 pyyaml 依赖。
    """
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not match:
        return {}, text
    block = match.group(1)
    body = text[match.end():]
    meta = {}
    current_list = None

    for raw in block.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        list_item = re.match(r"^\s*-\s+(.*)$", line)
        if list_item and current_list:
            meta[current_list].append(list_item.group(1).strip().strip('"\''))
            continue
        kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not kv:
            continue
        key = kv.group(1).strip()
        value = kv.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip('"\'') for item in value[1:-1].split(",")]
            meta[key] = [item for item in items if item]
            current_list = key
        elif value:
            meta[key] = value.strip('"\'')
            current_list = None
        else:
            meta[key] = []
            current_list = key
    return meta, body


def first_quote(text: str, limit: int = 260) -> str:
    """取正文里第一段有信息量的引用块，作为卡片摘要。"""
    for line in QUOTE_LINE.findall(text):
        clean = re.sub(r"[*`>]", "", line).strip()
        if len(clean) < 12:
            continue
        if clean.startswith(("|", "---")):
            continue
        clean = re.sub(r"^(一句话|本专题对应|读完你能回答|本章目标)[:：·]*\s*", "", clean)
        return clean[:limit]
    return ""


def extract_section(text: str, heading_keyword: str, limit: int = 900) -> str:
    """抽出某个二级标题下的正文（用于"交付标准"这类小节）。"""
    pattern = re.compile(r"^##\s+[^\n]*" + re.escape(heading_keyword) + r"[^\n]*\n(.*?)(?=\n##\s|\Z)",
                         re.M | re.S)
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()[:limit]


def parse_chapter(path: str, filename: str):
    raw = read(path)
    meta, body = split_frontmatter(raw)
    title_match = CHAPTER_TITLE.search(body)
    if not title_match:
        return None

    hours_match = CHAPTER_HOURS.search(body)
    prereq_match = CHAPTER_PREREQ.search(body)
    code_match = CHAPTER_CODE.search(body)
    goal_match = CHAPTER_GOAL.search(body)

    return {
        "no": title_match.group(1),
        "title": title_match.group(2).strip(),
        "headline": meta.get("title", ""),          # frontmatter 里的长标题（讲稿式）
        "desc": meta.get("description", ""),
        "keywords": meta.get("keywords", []),
        "tags": meta.get("tags", []),
        "goal": goal_match.group(1).strip() if goal_match else "",
        "hours": float(hours_match.group(1)) if hours_match else None,
        "prereq": prereq_match.group(1).strip() if prereq_match else "",
        "code": code_match.group(1) if code_match else "",
        "summary": first_quote(body),
        "deliverables": extract_section(body, "交付标准") or extract_section(body, "本章产出"),
        "file": filename,
        "markdown": body,          # 网页只渲染正文，frontmatter 不进阅读器
    }


def parse_wiki(directory: str, kind: str):
    items = []
    full = os.path.join(ROOT, directory)
    for name in sorted(os.listdir(full)):
        if not name.endswith(".md"):
            continue
        meta, body = split_frontmatter(read(os.path.join(full, name)))
        if kind == "topic":
            match = TOPIC_TITLE.search(body)
            title = match.group(1) if match else name[:-3]
        else:
            heading = SHEET_TITLE.search(body)
            title = heading.group(1).strip() if heading else name[:-3]
        items.append({
            "id": name[:-3],
            "title": title,
            "headline": meta.get("title", ""),
            "summary": meta.get("description", "") or first_quote(body),
            "keywords": meta.get("keywords", []),
            "count": len(body) // 1000,
            "file": "{}/{}".format(directory, name),
            "markdown": body,
        })
    return items


def parse_plain_dir(directory: str, prefix: str = ""):
    """解析一个"每篇独立成页"的目录（00-配置 / 01-Raw）。"""
    items = []
    full = os.path.join(ROOT, directory)
    if not os.path.isdir(full):
        return items
    for name in sorted(os.listdir(full)):
        if not name.endswith(".md"):
            continue
        meta, body = split_frontmatter(read(os.path.join(full, name)))
        heading = SHEET_TITLE.search(body)
        items.append({
            "id": prefix + name[:-3],
            "title": (heading.group(1).strip() if heading else name[:-3]),
            "summary": meta.get("description", "") or first_quote(body, 180),
            "keywords": meta.get("keywords", []),
            "file": "{}/{}".format(directory, name),
            "markdown": body,
        })
    return items


def parse_notes():
    directory = os.path.join(ROOT, "03-学习笔记")
    items = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        match = re.match(r"Week(\d+)-(.+)\.md$", name)
        if not match:
            continue
        week = int(match.group(1))
        meta, body = split_frontmatter(read(os.path.join(directory, name)))
        heading = SHEET_TITLE.search(body)
        items.append({
            "week": week,
            "key": "Week{:02d}".format(week),
            "title": match.group(2),
            "heading": heading.group(1).strip() if heading else "",
            "chapters": WEEK_CHAPTERS.get(week, []),
            "file": "03-学习笔记/{}".format(name),
            "markdown": body,
        })
    return items


# ---------------------------------------------------------------------------
# 模拟面试：解析结构化题库
# ---------------------------------------------------------------------------

def parse_questions(body: str):
    """解析一"轮"里的所有题目。格式见 06-模拟面试/03-题库与追问链.md 的说明。"""
    questions = []
    blocks = re.split(r"^###\s+", body, flags=re.M)

    for block in blocks[1:]:
        lines = block.split("\n")
        head = lines[0].strip()
        if "·" in head:
            qid, title = head.split("·", 1)
        else:
            qid, title = head, ""
        question = {
            "id": qid.strip(),
            "title": title.strip().strip("　 "),
            "dimension": "", "level": "", "role": "", "minutes": 0,
            "probe": "", "prompt": "", "related": "",
            "outline": [], "plus": [], "minus": [], "followups": [],
        }
        current = None

        for line in lines[1:]:
            single = re.match(r"^-\s+\*\*(.+?)\*\*：\s*(.*)$", line)
            if single:
                key = BANK_FIELDS.get(single.group(1).strip())
                value = single.group(2).strip()
                if not key:
                    current = None
                    continue
                if value:
                    question[key] = int(value) if key == "minutes" and value.isdigit() else value
                    current = None
                else:
                    question[key] = []
                    current = key
                continue

            item = re.match(r"^\s{2,}-\s+(.*)$", line)
            if item and current in BANK_LISTS:
                text = item.group(1).strip()
                if text:
                    question[current].append(text)
                continue

        questions.append(question)
    return questions


def parse_mock_bank(path: str):
    """把题库按 `## 轮次：` 切成几轮。"""
    body = split_frontmatter(read(path))[1]
    rounds = []
    parts = re.split(r"^##\s*轮次：\s*(.+?)\s*$", body, flags=re.M)
    for index in range(1, len(parts), 2):
        name = parts[index].strip()
        questions = parse_questions(parts[index + 1])
        if not questions:
            continue
        rounds.append({
            "name": name,
            "prefix": questions[0]["id"][:1],
            "count": len(questions),
            "questions": questions,
        })
    return rounds


def parse_mock_documents():
    """06-模拟面试 下除了题库之外的说明文档。"""
    directory = os.path.join(ROOT, "06-模拟面试")
    items = []
    if not os.path.isdir(directory):
        return items
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md") or "题库" in name:
            continue
        meta, body = split_frontmatter(read(os.path.join(directory, name)))
        heading = SHEET_TITLE.search(body)
        items.append({
            "id": name[:-3],
            "title": heading.group(1).strip() if heading else name[:-3],
            "headline": meta.get("title", ""),
            "summary": meta.get("description", "") or first_quote(body),
            "file": "06-模拟面试/{}".format(name),
            "markdown": body,
        })
    return items


# ---------------------------------------------------------------------------
# 算法轨道（单独成包，网页按需懒加载）
# ---------------------------------------------------------------------------

#: 判题器的目录。网页构建要用它生成 C++ 解题模板 —— 返回类型得从期望值反推，
#: 那套逻辑在判题器里，不重复实现一份（重复就会有两边不一致的风险）。
JUDGE_DIR = os.path.join(ROOT, "05-算法面试", "06-在线判题")


def _load_cpp_starter():
    if JUDGE_DIR not in sys.path:
        sys.path.insert(0, JUDGE_DIR)
    try:
        from languages import cpp_starter  # noqa: WPS433 - 构建期按路径导入
        return cpp_starter
    except Exception:                       # noqa: BLE001 - 判题器缺失时网页照样能用（只是没 C++ 模板）
        return None


def load_judge_meta():
    """
    读在线判题题库，取出每道题的"判题元数据"给网页用。

    注意：**不把用例内容塞进浏览器**（testcases.json 有 6MB）。网页只拿到
    "这道题能不能判、函数签名是什么、有几个用例、力扣链接在哪"，真正的用例在
    本地判题服务里。这样网页体积不受题库影响。
    """
    path = os.path.join(ROOT, "05-算法面试", "06-在线判题", "testcases.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(read(path))
    except (ValueError, OSError):
        return {}

    cpp_starter = _load_cpp_starter()

    meta = {}
    for pid, spec in (data.get("problems") or {}).items():
        entry = spec.get("entry", "solve")
        if spec.get("kind") == "operations":
            ops = spec["cases"][0]["ops"] if spec.get("cases") else []
            methods = []
            for op in ops[1:]:
                if op[0] not in methods:
                    methods.append(op[0])
            signature = "class %s —— 需要实现：%s" % (entry, "、".join(methods))
        else:
            signature = "def %s(%s)" % (entry, ", ".join(spec.get("params", [])))

        hints = []
        if spec.get("types"):
            hints.append("参数含 " + "、".join(sorted(set(spec["types"].values()))) + "，判题器会自动转换")
        if spec.get("inplace"):
            hints.append("原地修改题：判题器比对的是被改动的参数，写 return None 也算对")
        if any(c.get("compare") == "unordered" for c in spec.get("cases", [])):
            hints.append("解集顺序不固定，判题器会忽略顺序")
        if any(c.get("compare") == "any_of" for c in spec.get("cases", [])):
            hints.append("答案不唯一，多个正确答案都算通过")
        if spec.get("kind") == "operations":
            hints.append("按操作序列调用你的类，逐个比对每次调用的返回值")

        starter = ""
        if cpp_starter:
            try:
                starter = cpp_starter(spec)
            except Exception:               # noqa: BLE001 - 单题模板生成失败不该拖垮构建
                starter = ""

        meta[pid] = {
            "entry": entry,
            "cppStarter": starter,
            "kind": spec.get("kind", "function"),
            "level": spec.get("level", ""),
            "signature": signature,
            "caseCount": len(spec.get("cases", [])),
            "leetcode": spec.get("leetcode", ""),
            "hints": hints,
            "sample": _first_case_preview(spec),
        }
    return meta


def _first_case_preview(spec, limit=180):
    """给编辑器旁边看的第一条用例预览（不含答案）"""
    cases = spec.get("cases") or []
    if not cases:
        return ""
    case = cases[0]
    if spec.get("kind") == "operations":
        ops = case.get("ops") or []
        head = ops[:4]
        text = " → ".join(
            "%s(%s)" % (op[0], ", ".join(json.dumps(a, ensure_ascii=False) for a in (op[1] if len(op) > 1 else [])))
            for op in head)
        if len(ops) > 4:
            text += " → …"
        return text[:limit]
    parts = []
    for name in spec.get("params", []):
        parts.append("%s = %s" % (name, json.dumps(case["input"].get(name), ensure_ascii=False)))
    return "输入：" + "，".join(parts)


def parse_algo_track():
    """05-算法面试：专题 / 题目详解 / 周笔记 / 原始素材。"""
    base = os.path.join(ROOT, "05-算法面试")
    if not os.path.isdir(base):
        return None

    def collect(sub, kind):
        full = os.path.join(base, sub)
        items = []
        if not os.path.isdir(full):
            return items
        for name in sorted(os.listdir(full)):
            if not name.endswith(".md"):
                continue
            body = split_frontmatter(read(os.path.join(full, name)))[1]
            heading = SHEET_TITLE.search(body)
            items.append({
                "id": name[:-3],
                "title": heading.group(1).strip() if heading else name[:-3],
                "kind": kind,
                "summary": first_quote(body, 160),
                "file": "05-算法面试/{}/{}".format(sub, name),
                "markdown": body,
            })
        return items

    topics = collect("02-专题总结", "topic")
    problems = collect("03-题目详解", "problem")
    day_notes = collect("04-学习笔记", "note")

    # 题目详解按题号排序（文件名形如 "146-LRU缓存.md"）
    def problem_number(item):
        match = re.match(r"^(\d+)", item["id"])
        return int(match.group(1)) if match else 999999

    problems.sort(key=problem_number)

    # 把在线判题的元数据挂到对应题目上：哪些题能判、函数签名是什么、力扣链接在哪
    judge_meta = load_judge_meta()
    for item in problems:
        item["judge"] = judge_meta.get(item["id"])

    config = parse_plain_dir("05-算法面试/00-配置", prefix="algo-")
    readme_path = os.path.join(base, "README.md")
    readme = None
    if os.path.isfile(readme_path):
        meta, body = split_frontmatter(read(readme_path))
        readme = {
            "title": meta.get("title", "算法面试轨道"),
            "summary": meta.get("description", ""),
            "markdown": body,
        }

    return {
        "readme": readme,
        "config": config,
        "topics": topics,
        "problems": problems,
        "dayNotes": day_notes,
        "stats": {
            "topics": len(topics),
            "problems": len(problems),
            "dayNotes": len(day_notes),
            "judgeable": sum(1 for item in problems if item.get("judge")),
            "judgeCases": sum((item.get("judge") or {}).get("caseCount", 0) for item in problems),
            "hours": 42,
        },
    }


# ---------------------------------------------------------------------------

def build():
    docs_dir = os.path.join(ROOT, "docs")

    chapters = []
    for name in sorted(os.listdir(docs_dir)):
        if not name.endswith(".md") or name == "README.md":
            continue
        parsed = parse_chapter(os.path.join(docs_dir, name), "docs/{}".format(name))
        if parsed:
            chapters.append(parsed)
    chapters.sort(key=lambda c: c["no"])

    stage_of = {}
    for stage in STAGES:
        for chapter_no in stage["chapters"]:
            stage_of[chapter_no] = stage["id"]
    for chapter in chapters:
        chapter["stage"] = stage_of.get(chapter["no"], "")

    wiki = {
        "topics": parse_wiki("02-Wiki/专题总结", "topic"),
        "cheatsheets": parse_wiki("02-Wiki/速查表", "sheet"),
        "interview": parse_wiki("02-Wiki/面试题库", "sheet"),
    }

    mock_bank_path = os.path.join(ROOT, "06-模拟面试", "03-题库与追问链.md")
    mock_rounds = parse_mock_bank(mock_bank_path) if os.path.isfile(mock_bank_path) else []

    notes = parse_notes()
    config = parse_plain_dir("00-配置")
    raw = parse_plain_dir("01-Raw")

    code_scripts = sorted(
        name for name in os.listdir(os.path.join(ROOT, "code"))
        if re.match(r"ch\d{2}_.*\.py$", name)
    )

    total_lines = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for name in filenames:
            if not name.endswith((".md", ".py")):
                continue
            with io.open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as handle:
                total_lines += sum(1 for _ in handle)

    algo = parse_algo_track()
    mock_total = sum(r["count"] for r in mock_rounds)

    main_payload = {
        "meta": {
            "name": "Agent Zero To One",
            "subtitle": "从零开始学 AI Agent",
            "tagline": "0 基础入门 · 17 章渐进教程 · 每章可运行代码 · 内嵌 AI 教练 · 模拟面试 · 直通面试",
            "builtAt": "由 web/_build.py 生成",
        },
        "stats": {
            "chapters": len(chapters),
            "topics": len(wiki["topics"]),
            "cheatsheets": len(wiki["cheatsheets"]),
            "interview": len(wiki["interview"]),
            "notes": len(notes),
            "ladder": len(LADDER),
            "scripts": len(code_scripts),
            "lines": total_lines,
            "hours": sum(c["hours"] or 0 for c in chapters),
            "mockRounds": len(mock_rounds),
            "mockQuestions": mock_total,
            "algoProblems": (algo or {}).get("stats", {}).get("problems", 0),
            "algoTopics": (algo or {}).get("stats", {}).get("topics", 0),
        },
        "stages": STAGES,
        "sources": SOURCES,
        "ladder": LADDER,
        "chapters": chapters,
        "wiki": wiki,
        "notes": notes,
        "config": config,
        "raw": raw,
        "mock": {
            "documents": parse_mock_documents(),
            "rounds": mock_rounds,
            "total": mock_total,
            "file": "06-模拟面试/03-题库与追问链.md",
        },
        "hub": {
            "title": "学习中枢 · AI 教练操作手册",
            "markdown": split_frontmatter(read(os.path.join(ROOT, "学习中枢.md")))[1],
        },
        "readme": {
            "title": "项目说明",
            "markdown": split_frontmatter(read(os.path.join(ROOT, "README.md")))[1],
        },
        "docsIndex": {
            "title": "教程总索引",
            "markdown": split_frontmatter(read(os.path.join(ROOT, "docs", "README.md")))[1],
        },
        "scripts": code_scripts,
        # 主包里只放算法轨道的"索引信息"（标题 / 统计 / 入口），
        # 完整内容（13 专题 + 100 题解 + 14 篇笔记）在 content-algo.js 里按需加载。
        "algo": None if algo is None else {
            "title": "算法面试轨道",
            "summary": "13 个专题 + 100 道 Hot 100 详解 + 14 天路线",
            "stats": algo["stats"],
        },
    }

    algo_payload = {
        "meta": {"title": "算法面试轨道", "source": "LeetCode-BaiTiTong (MIT)"},
        "readme": (algo or {}).get("readme"),
        "config": (algo or {}).get("config", []),
        "topics": (algo or {}).get("topics", []),
        "problems": (algo or {}).get("problems", []),
        "dayNotes": (algo or {}).get("dayNotes", []),
    }

    return main_payload, algo_payload


def write_bundle(path: str, variable: str, payload) -> float:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("/* 本文件由 web/_build.py 自动生成，请勿手改。 */\n")
        handle.write("window.{} = ".format(variable))
        handle.write(text)
        handle.write(";\n")
    return os.path.getsize(path) / 1024.0


def main() -> int:
    main_payload, algo_payload = build()

    if "--check" in sys.argv:
        print("章节 {} 个：".format(len(main_payload["chapters"])))
        for chapter in main_payload["chapters"]:
            print("  {:<3} 第{}章 {}  （{} 小时，阶段{}）".format(
                "OK", chapter["no"], chapter["title"], chapter["hours"], chapter["stage"]))
        print("\n专题 {} / 速查表 {} / 题库 {} / 周笔记 {}".format(
            len(main_payload["wiki"]["topics"]), len(main_payload["wiki"]["cheatsheets"]),
            len(main_payload["wiki"]["interview"]), len(main_payload["notes"])))
        print("\n模拟面试 {} 轮：".format(len(main_payload["mock"]["rounds"])))
        for round_info in main_payload["mock"]["rounds"]:
            print("  {:<14} {} 题（前缀 {}）".format(
                round_info["name"], round_info["count"], round_info["prefix"]))
        incomplete = []
        for round_info in main_payload["mock"]["rounds"]:
            for question in round_info["questions"]:
                missing = [k for k, v in question.items()
                           if v in ("", 0, []) and k not in ("related",)]
                if missing:
                    incomplete.append((question["id"], missing))
        if incomplete:
            print("  [警告] {} 道题字段不全：{}".format(
                len(incomplete), incomplete[:5]))
        algo = algo_payload
        print("\n算法轨道：专题 {} / 题目 {} / 天笔记 {}".format(
            len(algo["topics"]), len(algo["problems"]), len(algo["dayNotes"])))
        missing = [c["no"] for c in main_payload["chapters"] if not c["goal"] or not c["code"]]
        if missing:
            print("[警告] 这些章节缺目标或配套代码信息：{}".format(missing))
        return 0

    size_main = write_bundle(os.path.join(WEB_DIR, "content.js"), "AZTO", main_payload)
    size_algo = write_bundle(os.path.join(WEB_DIR, "content-algo.js"), "AZTO_ALGO", algo_payload)

    print("已生成 web/content.js        {:.0f} KB".format(size_main))
    print("已生成 web/content-algo.js   {:.0f} KB  （按需懒加载）".format(size_algo))
    print("  章节 {} / 专题 {} / 速查表 {} / 题库 {} / 周笔记 {} / 模拟面试 {} 题 / 算法题 {}".format(
        len(main_payload["chapters"]), len(main_payload["wiki"]["topics"]),
        len(main_payload["wiki"]["cheatsheets"]), len(main_payload["wiki"]["interview"]),
        len(main_payload["notes"]), main_payload["mock"]["total"],
        len(algo_payload["problems"])))
    print("  项目总行数 {:,}".format(main_payload["stats"]["lines"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
