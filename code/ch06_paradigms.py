"""
第 06 章 · 范式进阶 配套代码

本章学到什么：
  - CoT（先想再答）：不产生知识、只整理知识；只对"需要推理且模型知道"的任务有效
  - Plan-and-Solve（先规划再执行）：用全局视野换线性成本，但计划错了会一路错，所以 replan 不能省
  - Reflection（自我批判）：效果取决于批判来源 —— 没有外部信号时，模型会越改越自信但没变好
  - 决策顺序：直接调用 → CoT → ReAct → Plan → Reflect，每加一层都要有评测数据支撑

怎么跑：
    cd code
    AGENT_MOCK=1 python ch06_paradigms.py     # 离线模式，不需要 API Key（五种做法全跑一遍）
    python ch06_paradigms.py                  # 在线模式，需要 .env 里配好 Key

    AGENT_MOCK=1 python ch06_paradigms.py --compare        # 只跑对比表
    AGENT_MOCK=1 python ch06_paradigms.py --only cot       # 只跑 CoT
    AGENT_MOCK=1 python ch06_paradigms.py --task "你的任务"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import load_env, print_banner, get_llm, chat, count_tokens_approx, estimate_cost, format_cost, is_mock_mode, set_mock_script, reset_mock  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.tools import Tool, ToolRegistry  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.trace import Trace  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前

# Windows 控制台的编码不一定是 UTF-8；这里只把"编码失败"降级为替换字符，别让脚本崩在 print 上
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


# ============================================================================
# 一、任务与判据：能被程序判定对错，对比才有意义
# ============================================================================

#: 选这道题的理由：① 有唯一正确答案，程序能判对错；② 需要多步推理（折扣 → 门槛判断 → 相减），
#: CoT 才有发挥空间；③ 典型错法很清楚（忘记用券、算错减法），方便观察反思有没有"改坏"。
TASK = (
    "某网店一件商品原价 299 元。活动规则是：先打 8 折，再叠加一张『满 200 减 30』的优惠券。"
    "请算出最终实付价格（元，保留两位小数）。"
)
GROUND_TRUTH = 209.2  # 299 × 0.8 = 239.2；239.2 >= 200，可减 30；239.2 - 30 = 209.2

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def extract_answer_number(text: str):
    """
    从回答里抽出"最终答案"这个数。

    优先看「答案：」后面那个数（prompt 里约定好的格式），抽不到就退回最后一个数字。
    判据必须收敛成一个数 —— "读起来感觉对不对"没法用来比较五种做法。
    """
    match = re.search(r"答案\s*[：:]\s*￥?([0-9]+(?:\.[0-9]+)?)", text or "")
    if match:
        return float(match.group(1))
    numbers = _NUMBER_RE.findall(text or "")
    return float(numbers[-1]) if numbers else None


def judge(text: str) -> bool:
    """程序判定：回答里的最终答案是不是 209.2。"""
    value = extract_answer_number(text)
    return value is not None and abs(value - GROUND_TRUTH) < 0.01


def extract_json(text: str):
    """
    从模型输出里抠出 JSON。

    模型习惯把 JSON 包进 markdown 代码块（```json ... ```），直接 json.loads 会抛异常。
    截取第一个括号到最后一个括号是最省事的容错（第 03 章结构化输出讲了完整做法）。
    """
    text = text or ""
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出里没有可解析的 JSON：{}".format(text[:120]))


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def _mock(responses) -> None:
    """离线模式下预设本次调用的响应序列；在线模式什么都不做（真模型自己会答）。"""
    if is_mock_mode():
        set_mock_script(responses)


# ============================================================================
# 二、直接调用：基线（先有基线，才知道加范式有没有用）
# ============================================================================

def direct_answer(llm, task: str) -> str:
    """什么都不加，直接问。这是所有对比的基线。"""
    _mock([{"content": "打 8 折之后的价格是 239.2 元，这就是你需要支付的金额。", "tool_calls": []}])
    return llm.chat([{"role": "user", "content": task}], temperature=0.2)["content"]


# ============================================================================
# 三、CoT：先想再答
# ============================================================================

COT_EXAMPLES = """问题：一本书 240 页，第一天读了 1/4，第二天读了剩下的 1/3，还剩多少页？
思考：第一天读 240×1/4=60 页，剩 180 页。第二天读 180×1/3=60 页，剩 120 页。
答案：120
"""

COT_INSTRUCTION = "请一步一步思考，然后给出最终答案。最后一行用「答案：」开头。\n\n问题："


def cot_zero_shot(llm, task: str) -> str:
    """Zero-shot CoT：一句咒语"请一步一步思考"，是提示词工程里性价比最高的一招。"""
    _mock([{"content": (
        "第一步：算折扣价。299 × 0.8 = 239.2 元。\n"
        "第二步：判断优惠券门槛。239.2 元 >= 200 元，满足『满 200 减 30』，可以减 30 元。\n"
        "第三步：算实付价。239.2 - 30 = 209.2 元。\n"
        "答案：209.2"), "tool_calls": []}])
    return llm.chat([{"role": "user", "content": COT_INSTRUCTION + task}], temperature=0)["content"]


def cot_few_shot(llm, task: str) -> str:
    """Few-shot CoT：给示例比只喊咒语更稳，因为示例把推理格式也定死了。"""
    _mock([{"content": "思考：299×0.8=239.2，239.2>=200 可以减 30，239.2-30=209.2。\n答案：209.2",
            "tool_calls": []}])
    return llm.chat([{"role": "user", "content": COT_EXAMPLES + "\n问题：" + task}], temperature=0)["content"]


def cot_self_consistency(llm, task: str, n: int = 3) -> str:
    """
    Self-Consistency：采样 n 次，对最终答案投票。

    两个细节：① 温度必须 > 0，否则 n 次结果一模一样，投票毫无意义；
    ② 投票只能降低随机错误，对"模型压根不知道"的系统性错误完全无效。
    """
    _mock([{"content": "299×0.8=239.2，券后 209.2。\n答案：209.2", "tool_calls": []}] * max(n, 1))
    answers = []
    for _ in range(n):
        text = llm.chat([{"role": "user", "content": COT_INSTRUCTION + task}], temperature=0.7)["content"]
        answers.append(extract_answer_number(text))
    if len(set(answers)) <= 1:
        print("  [警告] n 次采样结果完全相同，投票没有意义 —— 离线 Mock 模式必然如此；"
              "在线模式请显式传 temperature>0，并检查 len(set(answers)) > 1")
    counter = {}
    for value in answers:
        counter[value] = counter.get(value, 0) + 1
    winner = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
    return "投票结果：{}（n={}）\n答案：{}".format(counter, n, winner)


# ============================================================================
# 四、Plan-and-Solve：先生成结构化计划，再逐步执行，最后汇总
# ============================================================================

PLANNER_PROMPT = """你是任务规划专家。把用户的复杂任务拆成 2-6 个可独立执行的步骤。

要求：
1. 每一步必须是"一次具体动作"，不能是"继续研究"这类空话。
2. 每一步要能独立执行，尽量少依赖其他步骤的结果。
3. 每一步都要写清"完成标准"：做完之后应该得到什么。

只输出 JSON，格式：
{"steps": [{"id": 1, "goal": "查 2026 年 Agent 的三大技术方向", "done_when": "列出 3 个方向名"}]}

用户任务：%s"""

EXECUTOR_PROMPT = """你在执行一个大任务的一个步骤。只做这一步，不要扩展范围。

大任务：%s
已完成的步骤与结果：
%s
当前步骤：%s
完成标准：%s

请执行并给出这一步的产出（不超过 200 字）。"""

REPLAN_PROMPT = """原任务：%s
原计划：%s
剩余步骤：%s

执行过程中发现新信息，判断原计划是否还成立：
- 成立 → {"action": "continue"}
- 需要调整 → {"action": "replan", "steps": [...]}
- 已能回答 → {"action": "done"}
只输出 JSON。"""


def make_plan(llm, task: str):
    """Planner：把任务拆成可判定的步骤列表。计划质量决定整条流水线的上限。"""
    _mock([{"content": json.dumps({"steps": [
        {"id": 1, "goal": "算出打 8 折后的价格", "done_when": "得到折扣价这个具体数字"},
        {"id": 2, "goal": "判断折扣价是否达到满 200 减 30 的门槛", "done_when": "明确给出能用或不能用"},
        {"id": 3, "goal": "用折扣价减去优惠金额，得到最终实付价", "done_when": "得到最终价格（保留两位小数）"},
    ]}, ensure_ascii=False), "tool_calls": []}])
    raw = llm.chat([{"role": "user", "content": PLANNER_PROMPT % task}], temperature=0)["content"]
    return extract_json(raw)["steps"]


def execute_step(llm, task: str, step: dict, done: list) -> str:
    """Executor：只做这一步。历史结果只带摘要，避免每步都把全部历史重发（那是 ReAct 的成本特征）。"""
    mocks = {
        1: "299 × 0.8 = 239.2 元。",
        2: "239.2 元 >= 200 元，满足『满 200 减 30』，可以抵扣 30 元。",
        3: "239.2 - 30 = 209.2 元。",
        4: "239.2 - 30 = 209.2 元。",
    }
    _mock([{"content": mocks.get(step.get("id"), "这一步的产出。"), "tool_calls": []}])
    context = "\n".join("- 步骤 %s：%s → %s" % (s["id"], s["goal"], str(s.get("result", ""))[:120])
                        for s in done) or "（无）"
    return llm.chat([{"role": "user", "content": EXECUTOR_PROMPT % (task, context, step["goal"], step.get("done_when", ""))}],
                    temperature=0.2)["content"]


def replan(llm, task: str, remaining: list, done: list) -> dict:
    """
    Replan：计划的错误会被完整地执行下去，所以必须留检查点。

    这里每 2 步检查一次 —— 太频繁会让 Agent 每步都想重规划（成本翻倍），太稀疏就失去意义。
    """
    _mock([{"content": json.dumps({"action": "replan", "steps": [
        {"id": 4, "goal": "直接把折扣价 239.2 减去 30，得到最终实付价",
         "done_when": "得到最终价格（保留两位小数）"}
    ]}, ensure_ascii=False), "tool_calls": []}])
    raw = llm.chat([{"role": "user", "content": REPLAN_PROMPT % (
        task, json.dumps(remaining, ensure_ascii=False), json.dumps([d.get("goal") for d in done], ensure_ascii=False))}],
        temperature=0)["content"]
    return extract_json(raw)


def summarize(llm, task: str, done: list) -> str:
    """
    汇总必须是一次独立请求。

    为什么不让 executor 顺带总结？"每步只做一步"和"看全局给结论"是两个不同的任务，
    合在一起模型就会把各步结果拼一遍交差。
    """
    _mock([{"content": "三步串起来：239.2 - 30 = 209.2 元。\n答案：209.2", "tool_calls": []}])
    context = "\n".join("- 步骤 %s：%s → %s" % (s["id"], s["goal"], str(s.get("result", ""))[:160]) for s in done)
    return llm.chat([{"role": "user", "content": "大任务：%s\n\n各步结果：\n%s\n\n请汇总成最终答案，最后一行用「答案：」开头。"
                      % (task, context)}], temperature=0)["content"]


def plan_and_execute(llm, task: str, enable_replan: bool = True, max_replans: int = 2, verbose: bool = True):
    """完整实现：planner → executor → replan → summarize。返回 (最终答案, trace)。"""
    trace = Trace("plan_and_execute")
    steps = make_plan(llm, task)
    trace.log("plan_created", steps=len(steps))
    if verbose:
        print("  [计划] {} 步：".format(len(steps)))
        for step in steps:
            print("    {}. {}（完成标准：{}）".format(step["id"], step["goal"], step.get("done_when", "")))

    done, replans, index = [], 0, 0
    while index < len(steps):
        step = steps[index]
        result = execute_step(llm, task, step, done)
        done.append(dict(step, result=result))
        if verbose:
            print("    [步骤 {}] {}".format(step["id"], result.strip()[:60]))
        trace.log("step_done", step=step["id"], result=result[:120])

        index += 1
        if enable_replan and index % 2 == 0 and index < len(steps) and replans < max_replans:
            decision = replan(llm, task, steps[index:], done)
            action = decision.get("action")
            trace.log("replan", action=action, remaining=len(steps) - index)
            if verbose:
                print("    [Replan 检查] 判定：{}".format(action))
            if action == "replan" and decision.get("steps"):
                replans += 1
                steps = steps[:index] + decision["steps"]
                if verbose:
                    print("    → 剩余步骤被替换为 {} 步（累计 replan {} 次）".format(len(decision["steps"]), replans))
            elif action == "done":
                trace.log("finish", reason="replan_done")
                break

    trace.log("finish", reason="summarize")
    return summarize(llm, task, done), trace


# ============================================================================
# 五、Reflection：让模型批判自己（以及那个必须亲眼看到的陷阱）
# ============================================================================

L1_CRITIC_PROMPT = """请检查下面这份回答，说说你觉得哪里可以更好。

用户需求：%s
回答全文：%s"""

#: L1（无外部信号）自我批判的 Mock 台词。设计要点：批判越来越"自信"（自我评分 6 → 9），
#: 但每一轮修改都在加内容，最终把正确答案 209.2 改成了 229.2 —— 这就是 over-editing。
_L1_SCRIPT = [
    {"content": "整体不错，但解释可以更详细，语言也可以更专业一些。自我评分：6/10。", "tool_calls": []},
    {"content": "299 打 8 折是 239.2 元；为了让说明更完整，这里补充一句：优惠券通常需要手动勾选，"
                "请在结算页面确认。综合来看价格是 239.2 元。\n答案：239.2", "tool_calls": []},
    {"content": "还可以更完整，建议补上活动规则背景，并说明一下例外情况。自我评分：8/10。", "tool_calls": []},
    {"content": "最终价格为 219.2 元。\n补充说明：活动规则以结算页显示为准，建议下单前再核对一次；"
                "若优惠券不可叠加，则按页面价格支付。\n答案：219.2", "tool_calls": []},
    {"content": "结构可以更清晰，建议加小标题、加过渡句。自我评分：9/10。", "tool_calls": []},
    {"content": "【结论】\n最终实付价格为 229.2 元。\n【说明】\n1. 先按 8 折计算；\n2. 再扣除优惠金额；\n"
                "3. 具体以结算页为准。\n答案：229.2", "tool_calls": []},
]


def reflect_l1(llm, task: str, max_rounds: int = 3, verbose: bool = True):
    """
    L1 反思：模型自我批判，没有任何外部信号。

    实现上故意"天真"（批判者是模型自己、提示词很客气、轮次放到 3 轮），
    这样才能在输出里亲眼看到那个陷阱：**越改越自信，但答案越改越错**。
    生产环境里 L1 最多一轮，只有 L2（外部信号）才值得多轮。
    """
    _mock([{"content": "299 × 0.8 = 239.2，再减 30 元，最终 209.2 元。\n答案：209.2", "tool_calls": []}])
    current = llm.chat([{"role": "user", "content": task}], temperature=0.2)["content"]
    versions = [("第 0 版（初始回答）", current)]
    trace = Trace("reflect_l1")

    for round_no in range(1, max_rounds + 1):
        _mock([_L1_SCRIPT[(round_no - 1) * 2]])
        critique = llm.chat([{"role": "user", "content": L1_CRITIC_PROMPT % (task, current)}], temperature=0.2)["content"]
        if verbose:
            print("    [第 {} 轮自我批判] {}".format(round_no, critique.strip()[:70]))
        _mock([_L1_SCRIPT[(round_no - 1) * 2 + 1]])
        current = llm.chat([{"role": "user", "content": "按下面的意见修改这份回答，其他内容保持不变。\n\n"
                                                       "原文：\n%s\n\n意见：%s" % (current, critique)}],
                           temperature=0.7)["content"]
        versions.append(("第 {} 版（反思后）".format(round_no), current))
        trace.log("reflect_round", round=round_no, correct=judge(current), chars=len(current),
                  critique=critique.strip()[:60])

    return versions, trace


def reflect_l2(llm, task: str, draft: str, registry: ToolRegistry, max_rounds: int = 2, verbose: bool = True):
    """
    L2 反思：批判来自**外部信号** —— 这里是一台真计算器。

    区别就在这里：外部信号可验证、可复现，所以它能指出"你这里算错了，正确值是 X"，模型照着改就行；
    没有外部信号时，模型只能凭感觉重新采样一遍，改动不等于改善。
    """
    trace = Trace("reflect_l2")
    current = draft
    for round_no in range(1, max_rounds + 1):
        observed = registry.execute("calculator", {"expression": "299*0.8-30"})
        match = re.search(r"=\s*(-?\d+(?:\.\d+)?)", observed)
        expected = float(match.group(1)) if match else GROUND_TRUTH
        actual = extract_answer_number(current)
        trace.log("external_check", round=round_no, expected=expected, actual=actual)
        if actual is not None and abs(actual - expected) < 0.01:
            if verbose:
                print("  [L2 第 {} 轮] 外部校验通过（计算器：{}，答案一致）".format(round_no, expected))
            break
        if verbose:
            print("  [L2 第 {} 轮] 外部校验不通过：计算器给出 {}，答案里写的是 {}".format(round_no, expected, actual))
        _mock([{"content": "根据外部校验结果修正：299×0.8=239.2，239.2-30=209.2。\n答案：209.2", "tool_calls": []}])
        current = llm.chat([{"role": "user", "content":
                             "你的答案被外部校验判定为错误。\n题目：%s\n你的答案：%s\n计算器给出的正确结果：%s\n"
                             "请只修正错误的部分，重新给出答案（最后一行用「答案：」开头）。" % (task, current, expected)}],
                           temperature=0.2)["content"]
        trace.log("revised", round=round_no, correct=judge(current))
    return current


# ============================================================================
# 六、组合流水线：Plan → Execute → Reflect(L2)，每一层都能单独关掉
# ============================================================================

def pipeline(llm, task: str, registry: ToolRegistry, enable_plan: bool = True, enable_reflection: bool = True,
             verbose: bool = True):
    """逐层加法的实验台：关掉某一层就能做 A/B，这是"范式税"里必须付的那点实现成本。"""
    trace = Trace("pipeline")
    steps = make_plan(llm, task) if enable_plan else [{"id": 1, "goal": task, "done_when": "给出最终价格"}]
    done = []
    for step in steps:
        done.append(dict(step, result=execute_step(llm, task, step, done)))
    draft = summarize(llm, task, done)
    trace.log("draft_ready", correct=judge(draft), preview=draft.strip()[:80])
    if verbose:
        print("  [流水线] 汇总初稿：{}".format(draft.strip().splitlines()[-1][:60]))
    if enable_reflection:
        draft = reflect_l2(llm, task, draft, registry, verbose=verbose)
    return draft, trace


# ============================================================================
# 七、对比实验：同一个任务，五种做法各跑一遍
# ============================================================================

#: 每种做法对应的"适用场景"一句话，打在对比表最后一列，方便对着决策表复习
FITNESS = {
    "直接调用": "答案在模型知识里、一次问答就能答 —— 一切从这一行开始试",
    "CoT": "一次性的多步推理（算术、逻辑、约束），无外部信息",
    "Plan-and-Solve": "步骤 >= 4 且依赖弱、能事先列清（调研、批量处理）",
    "反思 L1(无外部信号)": "创作类润色，最多一轮；再改只会漂移",
    "Plan + 反思 L2(外部信号)": "有客观验收标准（测试、计算器、检索证据）时才值得多轮",
}


def _usage(llm) -> dict:
    return {"calls": llm.call_count, "prompt": llm.total_prompt_tokens, "completion": llm.total_completion_tokens}


def _print_reflection_table(versions) -> None:
    """把每一版摊开对比：改动和改善不是一回事，这张表就是证据。"""
    print("\n  反思轨迹（L1 无外部信号；判据：最终数字是否等于 {}）：".format(GROUND_TRUTH))
    print("  " + "-" * 74)
    print("  {:<20}{:>10}{:>14}{:>10}{:>14}".format("版本", "字符数", "抽出的数字", "是否正确", "与上一版相比"))
    print("  " + "-" * 74)
    previous = None
    for label, text in versions:
        number = extract_answer_number(text)
        changed = "-" if previous is None else ("有改动" if text.strip() != previous.strip() else "没改动")
        print("  {:<20}{:>10}{:>14}{:>10}{:>14}".format(
            label, len(text), "无" if number is None else number, "对" if judge(text) else "错", changed))
        previous = text
    print("  " + "-" * 74)
    print("  结论：字符数一路在涨（每轮都在补充内容），但正确答案只出现在前两版 —— 这就是 over-editing。")
    print("  原因：批判者是模型自己，没有外部锚点，每一轮「改进」其实是重新采样一次 —— 改动不等于改善。")


def compare(task: str, registry: ToolRegistry, only: str = "all"):
    """五种做法各跑一遍并打印对比表 —— 本章最该带走的一张表。"""
    section("对比实验：同一个任务，五种做法")
    print("任务：{}".format(task))
    print("判据（可程序判定）：回答里的最终答案 == {} 元".format(GROUND_TRUTH))
    rows = []

    if only in ("all", "direct"):
        reset_mock()
        llm = get_llm(system="你是购物助手，直接给出最终价格。")
        started = time.time()
        answer = direct_answer(llm, task)
        rows.append(("直接调用", _usage(llm), judge(answer), time.time() - started))
        print("\n[直接调用] {}".format(answer.strip().replace("\n", " ")[:70]))

    if only in ("all", "cot"):
        reset_mock()
        llm = get_llm(system="你是严谨的推理助手，最后一行用「答案：」开头给出结论。", temperature=0)
        started = time.time()
        answer = cot_zero_shot(llm, task)
        rows.append(("CoT", _usage(llm), judge(answer), time.time() - started))
        print("\n[CoT zero-shot] {}".format(answer.strip().replace("\n", " ")[:70]))
        print("[CoT few-shot ] {}".format(cot_few_shot(llm, task).strip().replace("\n", " ")[:70]))
        print("[Self-Consistency 投票 n=3] {}".format(
            cot_self_consistency(llm, task, n=3).strip().replace("\n", " ")[:70]))

    if only in ("all", "plan"):
        reset_mock()
        llm = get_llm(system="你是严谨的推理助手。", temperature=0)
        started = time.time()
        answer, plan_trace = plan_and_execute(llm, task)
        rows.append(("Plan-and-Solve", _usage(llm), judge(answer), time.time() - started))
        print("\n[Plan-and-Solve] 最终答案：{}".format(answer.strip().replace("\n", " ")[:70]))

    if only in ("all", "reflect"):
        reset_mock()
        llm = get_llm(system="你是严谨的推理助手。", temperature=0)
        started = time.time()
        versions, l1_trace = reflect_l1(llm, task)
        rows.append(("反思 L1(无外部信号)", _usage(llm), judge(versions[-1][1]), time.time() - started))
        _print_reflection_table(versions)

    if only in ("all", "pipeline"):
        reset_mock()
        llm = get_llm(system="你是严谨的推理助手。", temperature=0)
        started = time.time()
        answer, pipe_trace = pipeline(llm, task, registry)
        rows.append(("Plan + 反思 L2(外部信号)", _usage(llm), judge(answer), time.time() - started))
        print("\n[流水线 Plan + 反思 L2] 最终答案：{}".format(answer.strip().splitlines()[-1][:70]))

    print("\n" + "-" * 104)
    print("{:<26}{:>8}{:>12}{:>10}{:>10}{:>10}  {}".format(
        "范式", "请求次数", "总 token", "是否答对", "耗时(s)", "成本", "适用场景"))
    print("-" * 104)
    for name, usage, ok, elapsed in rows:
        total = usage["prompt"] + usage["completion"]
        cost = estimate_cost(usage["prompt"], usage["completion"])
        print("{:<26}{:>8}{:>12}{:>10}{:>10.2f}{:>10}  {}".format(
            name, usage["calls"], total, "对" if ok else "错", elapsed, format_cost(cost), FITNESS.get(name, "")))
    print("-" * 104)

    if rows:
        cheapest = min(rows, key=lambda r: r[1]["prompt"] + r[1]["completion"])
        dearest = max(rows, key=lambda r: r[1]["prompt"] + r[1]["completion"])
        low = cheapest[1]["prompt"] + cheapest[1]["completion"]
        high = dearest[1]["prompt"] + dearest[1]["completion"]
        if low:
            print("token 差距：最贵的『{}』是『{}』的 {:.1f} 倍 —— 这就是范式税。".format(dearest[0], cheapest[0], high / float(low)))
        print("读表提醒：最高配置不等于最优配置；先用能过的最简单方案，每加一层都要有评测数据支撑。")
    return rows


# ============================================================================
# 八、入口
# ============================================================================

def build_registry() -> ToolRegistry:
    """给 L2 反思准备的"外部信号"：一台真计算器。外部信号是 L2 与 L1 的唯一区别。"""
    registry = ToolRegistry()

    @registry.tool(description="计算数学表达式，返回计算结果，用于验证算式的正确性",
                   parameters={"type": "object",
                               "properties": {"expression": {"type": "string",
                                                             "description": "要计算的算式，如 299*0.8-30"}},
                               "required": ["expression"]})
    def calculator(expression: str) -> str:
        if not re.fullmatch(r"[0-9\.\+\-\*/\(\)\s]+", expression or ""):
            raise ValueError("表达式含不允许的字符")
        return "{} = {}".format(expression, round(eval(expression), 4))  # 教学示例，生产环境别用 eval

    return registry


def main() -> int:
    parser = argparse.ArgumentParser(description="第 06 章 · 三种范式的对比实验")
    parser.add_argument("--only", default="all",
                        choices=["all", "direct", "cot", "plan", "reflect", "pipeline"],
                        help="只跑某一种做法")
    parser.add_argument("--compare", action="store_true", help="只跑对比表")
    parser.add_argument("--task", default=TASK, help="换成你自己的任务（判据仍按 209.2 判定）")
    args = parser.parse_args()

    load_env()
    print_banner("第 06 章 · 范式进阶：CoT / Plan-and-Solve / Reflection")
    registry = build_registry()
    task = args.task

    if args.compare or args.only != "all":
        compare(task, registry, only=("all" if args.compare else args.only))
    else:
        section("第 1 节 · 直接调用（基线）")
        llm = get_llm(system="你是购物助手，直接给出最终价格。")
        print("[回答] {}".format(direct_answer(llm, task)))

        section("第 2 节 · CoT：先想再答")
        llm_cot = get_llm(system="你是严谨的推理助手，最后一行用「答案：」开头给出结论。", temperature=0)
        print("[Zero-shot CoT]\n{}".format(cot_zero_shot(llm_cot, task)))
        print("\n[Few-shot CoT]\n{}".format(cot_few_shot(llm_cot, task)))
        print("\n[Self-Consistency 投票 n=3]\n{}".format(cot_self_consistency(llm_cot, task, n=3)))

        section("第 3 节 · Plan-and-Solve：先规划再执行")
        llm_plan = get_llm(system="你是严谨的推理助手。", temperature=0)
        plan_answer, plan_trace = plan_and_execute(llm_plan, task)
        print("\n[汇总] {}".format(plan_answer.strip()))

        section("第 4 节 · Reflection：那个必须亲眼看到的陷阱")
        print("L1（模型自我批判、没有外部信号）跑 3 轮，观察答案怎么变：")
        versions, l1_trace = reflect_l1(get_llm(system="你是严谨的推理助手。", temperature=0), task)
        _print_reflection_table(versions)
        print("\n再看 L2（批判来自外部信号：一台真计算器）：")
        l2_answer = reflect_l2(get_llm(system="你是严谨的推理助手。", temperature=0), task,
                               "239.2 元（没算优惠券）。\n答案：239.2", registry)
        print("  [L2 结果] {}".format(l2_answer.strip().splitlines()[-1]))

        section("第 5 节 · 组合流水线与对比表")
        compare(task, registry)

    section("本章要点回顾")
    for line in [
        "1. CoT 只整理知识、不产生知识：多步推理有效，事实检索反而给幻觉加戏。",
        "2. Plan-and-Solve 用先规划换全局视野和线性成本，但计划错了会一路错 —— replan 不能省。",
        "3. Reflection 的效果取决于批判来源：L2（外部信号）可靠，L1（自我批判）不稳定。",
        "4. 没有外部信号时，模型会越改越自信但没变好（over-editing）—— 所以 L1 最多一轮。",
        "5. 反思循环的四个保命设计：具体 rubric、定位到原文片段、问题没变少就停、返回最好的一版。",
        "6. 范式不是越多越好：先上最简单能过的方案，每加一层都要有评测数据支撑。",
    ]:
        print("  " + line)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
