"""
第 09 章 · Skills 能力包 配套代码

本章学到什么：
  - Skill = 一个目录 + 一份 SKILL.md：frontmatter 负责「被找到」，正文负责「怎么做」，资源负责「确定性执行」
  - description 是生死线：写清「做什么 + 什么时候用 + 什么时候别用」，并把你真实说过的话抄进去
  - 渐进式加载：目录常驻（Level 0）→ 命中才注入正文（Level 1）→ 用到才读资源（Level 2）
  - 效果必须测：固定任务集 + 等长基线 + 代码判定 + 成功率和 token 一起看

怎么跑：
    cd code
    AGENT_MOCK=1 python ch09_skills.py    # 离线模式，不需要 API Key（扫描 ../skills 目录）
    python ch09_skills.py                 # 在线模式：A/B 两组真的调用模型，数字才有意义
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import load_env, print_banner, get_llm, chat, count_tokens_approx, count_message_tokens, estimate_cost, format_cost, is_mock_mode, set_mock_script, reset_mock  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.tools import Tool, ToolRegistry  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.trace import Trace  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前

# Windows 控制台的编码不一定是 UTF-8；这里只把"编码失败"降级为替换字符，别让脚本崩在 print 上
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


# ============================================================================
# 一、frontmatter 解析（手写，不引入 PyYAML）
# ============================================================================
#
# 只支持教学需要的子集：
#   key: value            一行一个键值对
#   key: >-               折叠块（后续缩进行拼成一段），用来绕开 YAML 的冒号陷阱
#   key:
#     - item              列表
# 这已经覆盖了本书所有 SKILL.md 的写法，也覆盖了绝大多数真实技能的 frontmatter。
# 不支持嵌套对象、锚点、多文档 —— 需要那些的时候，说明你的技能元信息写过头了。

def parse_frontmatter(text: str):
    """把 SKILL.md 拆成 (meta 字典, 正文)。项目坚持最小依赖，30 行手写解析足够。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta, key, folding, index = {}, None, False, 1
    while index < len(lines) and lines[index].strip() != "---":
        line = lines[index]
        stripped = line.strip()
        indented = line[:1] in (" ", "\t")
        if stripped.startswith("- ") and key:
            if not isinstance(meta.get(key), list):
                meta[key] = [] if not meta.get(key) else [meta[key]]
            meta[key].append(stripped[2:].strip())
        elif indented and key and folding:
            meta[key] = (meta[key] + " " + stripped).strip()      # 折叠块的续行
        elif ":" in line and not indented:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            folding = value in (">-", ">", "|", "|-")
            meta[key] = "" if folding else value.strip("\"'")
        index += 1
    return meta, "\n".join(lines[index + 1:]).strip()


# ============================================================================
# 二、Skill 与 SkillIndex
# ============================================================================

class Skill(object):
    """一个技能包。token 成本分三层算：目录（常驻）/ 正文（命中才加载）/ 资源（用到才读）。"""

    def __init__(self, name, description, body, path="", resources=None, source="磁盘"):
        self.name = name
        self.description = description
        self.body = body
        self.path = path
        self.resources = resources or []
        self.source = source

    def index_line(self) -> str:
        """Level 0：常驻上下文的那一行。技能能不能被选中，全看这一行。"""
        return "- {}: {}".format(self.name, self.description)

    def catalog_tokens(self) -> int:
        return count_tokens_approx(self.index_line())

    def body_tokens(self) -> int:
        return count_tokens_approx(self.body)

    def resource_tokens(self) -> int:
        total = 0
        for resource in self.resources:
            full = os.path.join(os.path.dirname(self.path), resource)
            if os.path.isfile(full):
                with io.open(full, encoding="utf-8", errors="replace") as handle:
                    total += count_tokens_approx(handle.read())
        return total

    def __repr__(self):
        return "<Skill {} {}>".format(self.name, self.source)


def load_skill(skill_md_path: str, source="磁盘") -> Skill:
    """从一份 SKILL.md 载入技能：解析 frontmatter + 扫描附带资源。"""
    with io.open(skill_md_path, encoding="utf-8", errors="replace") as handle:
        raw = handle.read()
    meta, body = parse_frontmatter(raw)
    directory = os.path.dirname(skill_md_path)
    resources = []
    for dirpath, _dirs, names in os.walk(directory):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            if os.path.abspath(full) == os.path.abspath(skill_md_path):
                continue
            resources.append(os.path.relpath(full, directory).replace("\\", "/"))
    return Skill(
        name=meta.get("name") or os.path.basename(directory),
        description=meta.get("description", ""),
        body=body,
        path=skill_md_path,
        resources=resources,
        source=source,
    )


def scan_skills(root: str):
    """扫描技能目录：每个 <技能名>/SKILL.md 是一个技能。索引只建一次，别在每轮请求时遍历磁盘。"""
    found = []
    if not os.path.isdir(root):
        return found
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name == "SKILL.md":
                found.append(load_skill(os.path.join(dirpath, name)))
    return found


PUNCT = set(" \t\n\r，。！？；：、（）【】《》“”‘’\"'`~!@#$%^&*()_+-=[]{}|\\/<>,.?:;")


def bigrams(text: str) -> set:
    """取字符二元组并去掉标点：'审查代码' → {'审查','查代','代码'}。不需要分词器，英文也能匹配。"""
    chars = [char for char in (text or "").lower() if char not in PUNCT]
    return set(chars[i] + chars[i + 1] for i in range(len(chars) - 1))


def overlap(task: str, description: str) -> float:
    """任务文本的二元组里，有多大比例能在 description 中找到 —— 这就是「触发分数」。"""
    task_bigrams = bigrams(task)
    if not task_bigrams:
        return 0.0
    return len(task_bigrams & bigrams(description)) / float(len(task_bigrams))


class SkillIndex(object):
    """技能索引：负责「谁被注入上下文」这一个决策，以及按需组装渐进式 prompt。"""

    def __init__(self, skills, threshold=0.40, top_k=2):
        self.skills = list(skills)
        self.threshold = threshold
        self.top_k = top_k
        self.trace = Trace("skill_index")

    # ------------------------------------------------------------ 目录与匹配

    def catalog(self) -> str:
        return "\n".join(skill.index_line() for skill in self.skills)

    def catalog_tokens(self) -> int:
        return count_tokens_approx(self.catalog())

    def score_all(self, task: str):
        scored = [(overlap(task, skill.description), skill) for skill in self.skills]
        scored.sort(key=lambda pair: -pair[0])
        return scored

    def match(self, task: str):
        """返回 [(分数, Skill)]，最多 top_k 个，且分数必须过阈值 —— 「全部命中」等于没做渐进式加载。"""
        picked = [(score, skill) for score, skill in self.score_all(task) if score >= self.threshold]
        result = picked[: self.top_k]
        self.trace.log("match", task=task[:30],
                       picked=[(round(s, 2), sk.name) for s, sk in result],
                       best=round(self.score_all(task)[0][0], 2) if self.skills else 0.0)
        return result

    # ------------------------------------------------------------ 三种注入方式

    def full_prompt(self) -> str:
        """反面教材：不做渐进式加载，每次请求都带上全部技能的正文。"""
        parts = ["你是 Agent。可用技能（全部正文）："]
        for skill in self.skills:
            parts.append("\n## 技能：{}\n{}".format(skill.name, skill.body))
        return "\n".join(parts)

    def progressive_prompt(self, picked, include_resources=False) -> str:
        """正确做法：目录常驻 + 只注入命中技能的正文；资源只给路径，用到才读。"""
        parts = ["你是 Agent。可用技能目录（命中才需要执行）：", self.catalog()]
        if picked:
            parts.append("\n本次任务请按以下技能执行：")
            for _score, skill in picked:
                hint = ""
                if skill.resources:
                    hint = "（附带资源，需要时再读取：{}）".format(", ".join(skill.resources))
                parts.append("\n## 技能：{}\n{}\n{}".format(skill.name, skill.body, hint))
                if include_resources:
                    for resource in skill.resources:
                        full = os.path.join(os.path.dirname(skill.path), resource)
                        if os.path.isfile(full):
                            with io.open(full, encoding="utf-8", errors="replace") as handle:
                                parts.append("\n## 资源：{}\n{}".format(resource, handle.read()))
        return "\n".join(parts)


# ============================================================================
# 三、演示技能（内存构造，不落盘）—— 用来演示「描述决定触发」
# ============================================================================

#: 演示技能的正文骨架：一份"合格技能"该有的五节（何时使用 / 不适用 / 步骤 / 验收标准 / 越界禁令）。
#: 正文写长一点是有意的 —— 它决定 Level 1 的成本，也是"全量注入 vs 渐进加载"差距的来源。
BODY_TEMPLATE = (
    "# {title}\n\n"
    "## 何时使用\n{when}\n\n"
    "## 不适用\n{not_when}\n\n"
    "## 步骤\n"
    "1. 先确认范围与验收标准：写下这次任务的输入是什么、产出必须满足哪几条。\n"
    "2. 收集材料并逐条编号：每条事实都要能追溯到来源，来源找不到就标注「未验证」。\n"
    "3. 按固定顺序处理：结构 → 内容 → 边界 → 风险，不要跳步，也不要只做最容易的两步。\n"
    "4. 产出固定形状的交付物：表格、清单、结论段三者至少各一。\n"
    "5. 交付前对着验收标准逐条自检，打勾后再输出。\n\n"
    "## 验收标准\n"
    "- [ ] 交付物含表格或清单，每行都能定位到具体位置\n"
    "- [ ] 每个结论都能追溯到来源\n"
    "- [ ] 明确写出未验证项与局限性\n\n"
    "## 越界禁令\n"
    "- 不要顺手扩大范围；发现额外问题时只在末尾用「建议：」注明，不要直接动手。\n"
)


def demo_body(title, when, not_when):
    return BODY_TEMPLATE.format(title=title, when=when, not_when=not_when)


def demo_skills():
    """教学用的对照技能：同一个能力，description 写得具体 vs 写得模糊，触发结果完全不同。"""
    specific = Skill(
        name="research-report",
        description=("调研一个技术主题并产出结构化报告。当用户说「写一份研究报告 / 调研一下 / "
                     "帮我做个技术选型 / 对比一下这几个方案 / 帮我写一份关于……的报告」时使用。"
                     "只是问一个概念的定义、或者只要一段简短解释时不要用。"),
        body=demo_body("研究报告", "用户要求调研、选型或对比多个方案。",
                       "只要一个名词解释，或只问一个具体事实。"),
        source="内存（演示）")
    vague = Skill(
        name="code-review-vague",
        description="一个帮助审查代码的技能",
        body=demo_body("代码审查（模糊描述版）", "用户要求审查代码。", "（没写不适用场景）"),
        source="内存（演示）")
    too_wide = Skill(
        name="generic-code",
        description="处理代码相关任务，提供专业的代码建议，包括审查、编写、重构、解释、补全等一切代码工作",
        body=demo_body("万能代码技能", "一切跟代码有关的事。", "（没写不适用场景）"),
        source="内存（演示）")
    weekly = Skill(
        name="weekly-report",
        description=("把本周的待办、提交记录与会议纪要整理成一份周报：按「本周完成 / 进行中 / 下周计划 / 风险」"
                     "四段输出，每条都要带负责人和时间。当用户说「写周报 / 整理本周工作 / 生成周报」时使用。"
                     "只是让你总结一篇文章时不要用。"),
        body=demo_body("周报", "用户要求整理本周工作、生成周报。", "总结一篇文章、写月报或项目复盘。"),
        source="内存（演示）")
    sql_review = Skill(
        name="sql-review",
        description=("专项检查 SQL 与数据库访问代码：索引命中、N+1 查询、事务边界、注入风险。"
                     "当用户明确要求「查一下 SQL 性能问题 / 审计数据库访问 / 有没有 N+1」时使用。"
                     "一般性的代码审查请改用代码审查技能。"),
        body=demo_body("SQL 专项审查", "用户点名要查 SQL 或数据库访问问题。", "一般性代码审查（用代码审查技能）。"),
        source="内存（演示）")
    return [specific, vague, too_wide, weekly, sql_review]


#: 6 条任务，覆盖典型 + 边界 + 一条「不该触发」的（第 09 章 6.1 的评测集要求）
TASKS = [
    ("审查一下这个 PR 的改动，看看有没有 bug", "example-skill"),
    ("帮我写一份关于向量数据库选型的研究报告", "research-report"),
    ("解释一下 def foo(x): return x + 1 这段代码在做什么", None),
    ("把这个函数补全实现一下", None),
    ("帮我整理本周的工作，生成周报", "weekly-report"),
    ("帮我审一下这个文件有没有安全问题", "example-skill"),
]

#: 判据（可机械判定）：每条任务「必须出现哪些要素」。真项目里应该由没读过技能的人来写。
CHECKS = {
    "example-skill": ["问题清单", "风险等级", "测试建议", "文件:行", "正确处理", "边界条件", "错误处理",
                      "并发", "安全"],
    "research-report": ["对比表", "来源", "结论", "局限", "关键词", "事实", "未验证", "范围", "验收"],
    "weekly-report": ["本周完成", "进行中", "下周计划", "风险", "负责人", "时间", "待办", "会议", "提交"],
}

#: 离线模式下的固定答案：带技能 = 命中全部判据，不带技能 = 只命中前 3 条（模拟"模型自由发挥"）
FAKE_WITH_SKILL = {
    "example-skill": "问题清单（文件:行 / 风险等级 / 问题 / 修复建议）：正确处理的路径已覆盖；"
                     "边界条件与错误处理各发现 1 处；并发与安全各 1 条高风险。测试建议：3 条用例。",
    "research-report": "范围与验收标准明确；关键词 5 个；事实 8 条（每条带来源）；结论 + 对比表 + 局限；"
                       "未验证项 2 条。",
    "weekly-report": "本周完成 / 进行中 / 下周计划 / 风险 四段；每条带负责人与时间；待办 7 条、会议 2 场、"
                     "提交 12 次。",
}
FAKE_WITHOUT_SKILL = {
    "example-skill": "我看了下，整体没什么大问题，风险不大，建议测试一下。",
    "research-report": "向量数据库主要有几款，各有优劣，建议根据场景选择。",
    "weekly-report": "这周做了不少事情，进度正常，下周继续努力。",
}
GENERIC_ANSWER = "好的，我来看一下。"


def judge(answer: str, checks) -> int:
    """判定器：返回命中了几条判据。真项目里应换成更严格的断言或模型评审。"""
    return sum(1 for word in checks if word in answer)


def mock_llm_answer(prompt: str, task: str, skill_name) -> str:
    """离线模式的"模型"：按「有没有拿到技能正文」返回固定答案，让 A/B 的差异可复现。"""
    if "本次任务请按以下技能执行" in prompt:
        return FAKE_WITH_SKILL.get(skill_name, GENERIC_ANSWER)
    return FAKE_WITHOUT_SKILL.get(skill_name, GENERIC_ANSWER)


# ============================================================================
# 四、跑起来
# ============================================================================

def report_index(skills_from_disk, demo):
    section("第 1-2 节 · 扫描目录 + 解析 frontmatter")
    print("  磁盘技能目录：{}".format(SKILLS_DIR))
    for skill in skills_from_disk:
        print("  [磁盘] {:<14} 路径={}".format(skill.name, os.path.relpath(skill.path, os.path.dirname(SKILLS_DIR))))
        print("         description（Level 0 常驻的那一行）：{}".format(skill.description.replace("\n", " ")[:70] + "……"))
        print("         正文 {} token ｜ 附带资源：{}".format(skill.body_tokens(), ", ".join(skill.resources) or "（无）"))
    if not skills_from_disk:
        print("  [警告] 没有扫描到任何技能 —— 请确认 skills/<技能名>/SKILL.md 存在（文件名必须大写）")
    print("\n  [解析自检] 直接读 example-skill 的 frontmatter（这是真实文件，不是硬编码）：")
    sample = os.path.join(SKILLS_DIR, "example-skill", "SKILL.md")
    if os.path.isfile(sample):
        with io.open(sample, encoding="utf-8", errors="replace") as handle:
            meta, body = parse_frontmatter(handle.read())
        print("         name        = {}".format(meta.get("name")))
        print("         version     = {}".format(meta.get("version", "（未写）")))
        print("         description = {}{}".format(str(meta.get("description", ""))[:60], "……"))
        print("         正文小节    = {}".format(re.findall(r"^##\s+(.+)$", body, re.M)))
        print("         折叠块（>-）已还原成一段：{}".format("\n" not in str(meta.get("description", ""))))
    print("\n  另外 {} 个演示技能来自内存（不落盘，只用于演示描述写法的影响）：{}".format(
        len(demo), ", ".join(skill.name for skill in demo)))


def report_progressive(index: SkillIndex):
    section("第 4 节 · 渐进式加载：三级 token 占用对比")
    catalog_tokens = index.catalog_tokens()
    full_tokens = count_tokens_approx(index.full_prompt())
    body_total = sum(skill.body_tokens() for skill in index.skills)
    resource_total = sum(skill.resource_tokens() for skill in index.skills)
    print("  Level 0 · 目录（{} 个技能，常驻）：{} token".format(len(index.skills), catalog_tokens))
    print("  Level 1 · 正文（命中才注入，合计若全量注入）：{} token".format(body_total))
    print("  Level 2 · 资源（脚本 / 模板，用到才读，全量合计）：{} token".format(resource_total))
    print("  [全量注入] 每次请求都带上全部技能正文：{} token".format(full_tokens))
    print("\n  逐任务对比（渐进式 = 目录 + 命中技能的正文；资源只给路径）：")
    print("  {:<30} | {:<20} | {:>8} | {:>6}".format("任务", "命中技能", "渐进式", "节省"))
    print("  " + "-" * 74)
    save_rates = []
    for task, _expect in TASKS:
        picked = index.match(task)
        prompt = index.progressive_prompt(picked)
        tokens = count_tokens_approx(prompt)
        saved = 100.0 * (1 - tokens / float(max(full_tokens, 1)))
        save_rates.append(saved)
        print("  {:<30} | {:<20} | {:>8} | {:>5.0f}%".format(
            task[:28], ",".join(skill.name for _s, skill in picked) or "（无）", tokens, saved))
    print("\n  技能越多，差距越大 —— 现在只有 {} 个技能，所以「省」得并不夸张（平均 {:.0f}%）。".format(
        len(index.skills), sum(save_rates) / max(len(save_rates), 1)))
    for count in (20, 50):
        per_skill = body_total / float(max(len(index.skills), 1))
        full = full_tokens / float(max(len(index.skills), 1)) * count
        progressive = catalog_tokens / float(max(len(index.skills), 1)) * count + per_skill
        print("  如果有 {} 个技能（每个正文约 {:.0f} token）：全量注入约 {:.0f} token/次，"
              "渐进式只要 {:.0f} token/次，差 {:.0f} 倍".format(
                  count, per_skill, full, progressive, full / max(progressive, 1)))
    print("  结论：两三个技能不值得搭这套机制；二十个技能时它是刚需。")


def report_matching(index: SkillIndex):
    section("第 3 节 · 触发判断：description 是生死线")
    print("  判定方式：任务文本的字符二元组有多大比例能在 description 里找到（阈值 {:.2f}）".format(index.threshold))
    for task, expect in TASKS:
        scored = index.score_all(task)
        best_score, best_skill = scored[0] if scored else (0.0, None)
        picked = index.match(task)
        hit_names = [skill.name for _s, skill in picked]
        if hit_names:
            reason = "命中：与 {}({:.2f}) 的二元组重合度 >= 阈值".format(best_skill.name, best_score)
        else:
            reason = "未命中：最高分只有 {}({:.2f})，低于阈值 {:.2f}".format(
                best_skill.name if best_skill else "-", best_score, index.threshold)
        mark = "OK" if (expect in hit_names or (expect is None and not hit_names)) else "!!"
        print("  [{}] {:<34} → {:<22} {}".format(mark, task[:32], ",".join(hit_names) or "不注入技能", reason))
    print("\n  [正反例] 同一个能力，三种 description 写法在两个任务上的表现：")
    review_task = "审查一下这个 PR 的改动，看看有没有 bug"
    explain_task = "解释一下 def foo(x): return x + 1 这段代码在做什么"
    example_desc = ""
    for skill in index.skills:
        if skill.name == "example-skill":
            example_desc = skill.description
    cases = [
        ("正例（example-skill）", example_desc,
         "该触发的触发，不该触发的不触发 —— 触发词来自用户原话，边界句排除邻居"),
        ("反例 1：太虚", "一个帮助审查代码的技能",
         "该触发却不触发：分数只有 {:.2f} —— 描述里没有用户真实会说的话"),
        ("反例 2：太宽", "处理代码相关任务，提供专业的代码建议，包括审查、编写、重构、解释、补全等一切代码工作",
         "字面分数不高，但语义匹配（在线模式）会把相邻任务全拉进来；且没有边界句"),
    ]
    print("  {:<22}{:>10}{:>10}{:>12}   {}".format("写法", "审查任务", "解释任务", "目录 token", "结论"))
    for label, desc, comment in cases:
        print("  {:<22}{:>10.2f}{:>10.2f}{:>12}   {}".format(
            label, overlap(review_task, desc), overlap(explain_task, desc),
            count_tokens_approx(desc), comment.format(overlap(review_task, desc))))
    print("  阈值 {:.2f}：审查任务是「该触发」的代表，解释任务是「不该触发」的代表。".format(index.threshold))
    print("  一句话：**description 决定召回（用户说那句话时能不能命中），边界句决定准确率（会不会乱触发）**。")


def report_resources(index: SkillIndex):
    section("第 2.3 节 · Level 2 资源：正文只写「什么时候用脚本」，不写「脚本里是什么」")
    for skill in index.skills:
        if not skill.resources:
            continue
        for resource in skill.resources:
            full = os.path.join(os.path.dirname(skill.path), resource)
            if not os.path.isfile(full):
                continue
            with io.open(full, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            print("  {} / {}：{} 字，约 {} token —— 只有真的需要时才读进上下文".format(
                skill.name, resource, len(text), count_tokens_approx(text)))
    print("  做法：正文里给一行用法（如 python scripts/count_diff.py --diff a.patch），模型就不会去猜脚本内容。")
    print("        技能里出现的每个路径都必须存在 —— 否则模型读不到文件就开始编造。")


def report_ab_test(index: SkillIndex):
    section("第 6 节 · A/B smoke test：这个技能到底值不值得留")
    rounds = 1 if is_mock_mode() else 3
    print("  设计：{} 条任务 × {} 轮；A 组给**等长的无关文本**（不是空白 prompt），B 组给渐进式注入的技能".format(
        len(TASKS), rounds))
    print("  指标一起看：判据命中率提升 >= 20 个百分点，且 token 增幅 < 50%，才算值得留。\n")
    print("  {:<28} | {:>14} | {:>14} | {}".format("任务", "A 组(无关文本)", "B 组(技能)", "命中技能"))
    print("  " + "-" * 78)
    total_a = total_b = checks_total = 0
    tokens_a = tokens_b = 0
    for task, expect in TASKS:
        picked = index.match(task)
        skill_name = picked[0][1].name if picked else expect
        checks = CHECKS.get(skill_name, [])
        if picked:
            prompt_a = "你是 Agent。\n" + filler_text(index.catalog_tokens() + sum(s.body_tokens() for _s, s in picked))
        else:
            prompt_a = "你是 Agent。\n" + filler_text(index.catalog_tokens())
        prompt_b = index.progressive_prompt(picked)
        tokens_a += count_tokens_approx(prompt_a)
        tokens_b += count_tokens_approx(prompt_b)

        hit_a = hit_b = 0
        for _round in range(rounds):
            if is_mock_mode():
                answer_a = mock_llm_answer(prompt_a, task, skill_name)
                answer_b = mock_llm_answer(prompt_b, task, skill_name)
            else:
                answer_a = chat(task, system=prompt_a, temperature=0.2)
                answer_b = chat(task, system=prompt_b, temperature=0.2)
            hit_a += judge(answer_a, checks)
            hit_b += judge(answer_b, checks)
        total_a += hit_a
        total_b += hit_b
        checks_total += len(checks) * rounds
        print("  {:<28} | {:>14} | {:>14} | {}".format(
            task[:26], "{}/{}".format(hit_a, len(checks) * rounds),
            "{}/{}".format(hit_b, len(checks) * rounds), skill_name or "（无技能，不该触发）"))
    print("  " + "-" * 72)
    rate_a = 100.0 * total_a / max(checks_total, 1)
    rate_b = 100.0 * total_b / max(checks_total, 1)
    print("  判据命中率：A 组 {}/{}（{:.0f}%）　B 组 {}/{}（{:.0f}%）　提升 {:.0f} 个百分点".format(
        total_a, checks_total, rate_a, total_b, checks_total, rate_b, rate_b - rate_a))
    print("  system prompt token 合计：A 组 {}　B 组 {}（增幅 {:.0f}%）".format(
        tokens_a, tokens_b, 100.0 * (tokens_b - tokens_a) / float(max(tokens_a, 1))))
    print("  判定：{}".format(
        "值得留（命中率提升 >= 20 点且 token 增幅 < 50%）" if rate_b - rate_a >= 20 and
        (tokens_b - tokens_a) / float(max(tokens_a, 1)) < 0.5 else "需要改描述或步骤后再测"))
    if is_mock_mode():
        print("  [重要] 离线模式下 B 组全对，是因为答案来自脚本里的固定表 —— 它只验证「判定器与统计口径能跑通」。")
        print("         真实结论必须去掉 AGENT_MOCK 重跑（在线跑的是效果验收，离线跑的是检查器自检）。")


def filler_text(tokens: int) -> str:
    """生成与技能块 token 量级相当的无关文本 —— A/B 的基线必须等长，否则只证明了「上下文变长有用」。"""
    sentence = "以下是与本任务无关的背景资料，用于让两组上下文长度保持一致，避免实验出现偏差。"
    per = max(count_tokens_approx(sentence), 1)
    return sentence * max(int(tokens / per) + 1, 1)


def main() -> int:
    load_env()
    print_banner("第 09 章 · Skills 能力包：渐进式加载与 A/B 验证")
    skills_from_disk = scan_skills(SKILLS_DIR)
    demo = demo_skills()
    index = SkillIndex(skills_from_disk + demo, threshold=0.40, top_k=2)
    print("  索引构建完成：磁盘 {} 个 + 演示 {} 个 = {} 个技能（索引只建一次，常驻内存）".format(
        len(skills_from_disk), len(demo), len(index.skills)))

    report_index(skills_from_disk, demo)
    report_matching(index)
    report_progressive(index)
    report_resources(index)
    report_ab_test(index)

    section("本轮 Trace 摘要（选了哪个技能、分数多少 —— 调阈值时唯一能依赖的记录）")
    print(index.trace.summary())

    section("本章要点回顾")
    for line in [
        "1. 四个概念各就各位：Tool 让它能做，Prompt 管这一次，Skill 管一类任务的流程，MCP 管工具怎么复用接入。",
        "2. Skill = 一个目录 + 一份 SKILL.md：frontmatter 负责被找到，正文负责怎么做，资源负责确定性执行。",
        "3. description 是生死线：做什么 + 什么时候用（抄用户原话）+ 什么时候别用；正文再好，没被选中等于不存在。",
        "4. 渐进式加载：目录常驻、正文按需、资源最后；技能越多收益越大，两三个技能不值得搭。",
        "5. 正文 7±2 个步骤，每步写产物；验收标准必须能被机械判定，并配越界禁令。",
        "6. 有效果必须测：固定任务集 + 等长基线 + 重复多轮 + 代码判定，成功率与 token 一起看。",
        "7. 离线跑是「检查器自检」，在线跑才是「效果验收」—— 两者不能互相替代。",
    ]:
        print("  " + line)
    print("\n完成。")
    return 0


#: 技能目录：默认取项目根的 skills/（脚本在 code/ 下），也支持用环境变量指到别处
SKILLS_DIR = os.environ.get("SKILLS_DIR", "").strip() or os.path.join(os.path.dirname(_CODE_DIR), "skills")


if __name__ == "__main__":
    sys.exit(main())
