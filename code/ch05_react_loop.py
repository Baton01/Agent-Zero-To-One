"""
第 05 章 · ReAct 最小 Agent Loop 配套代码

本章学到什么：
  - ReAct = Reasoning + Acting：每一步先想再做，并把「想」留在上下文里（这是自我纠正的载体）
  - 几十行就能写出一个 Agent；但用正则解析自然语言一上线就碎 —— 正确做法是用 Function Calling 承载 Action
  - 五种停止条件全上：最大步数 / 重复动作 / 无进展 / 成本上限 / 超时
  - 每一步都记 trace：没有 trace，你只能凭感觉调提示词

怎么跑：
    cd code
    AGENT_MOCK=1 python ch05_react_loop.py              # 离线模式，三部分全跑
    AGENT_MOCK=1 python ch05_react_loop.py --part 3     # 只跑五种停止条件
    python ch05_react_loop.py                           # 在线模式，需要 .env 里配好 Key
    python ch05_react_loop.py --trace                   # 额外打印每一步的完整 messages（调试用）
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
import time
from collections import Counter

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (  # noqa: E402
    chat,
    estimate_cost,
    format_cost,
    get_llm,
    is_mock_mode,
    load_env,
    print_banner,
    set_mock_script,
)
from common.tools import ToolRegistry  # noqa: E402
from common.trace import Trace  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

#: 默认值都可用环境变量覆盖，其中 AGENT_BUDGET_CNY 就是正文点名的那个成本闸门
DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_SECONDS = 120.0
DEFAULT_MAX_COST = float(os.environ.get("AGENT_BUDGET_CNY", "") or 0.5)
DEFAULT_REPEAT_LIMIT = 3
DEFAULT_NO_PROGRESS_LIMIT = 3
FLAGS = {"trace": False}          # 命令行开关（--trace），用 dict 避免在函数里写 global


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def short(text, limit: int = 120) -> str:
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# ============================================================================
# 第 1 部分：极简版 ReAct（文本约定 + 正则解析，故意不写容错）
# ============================================================================

REACT_PROMPT = """你是一个会用工具的助手。可用工具：
- calculator(expression)：计算数学表达式，例如 calculator(1234*5678)
- get_current_time(timezone_name)：查询当前时间，timezone_name 取 local 或 UTC

严格按下面的格式回答，每次只输出一轮：
Thought: 你的思考 —— 现在该做什么，为什么
Action: 工具名(参数)
（写完 Action 就停下，等系统返回 Observation）

已经能回答时，输出：
Thought: 我已经知道答案了
Final Answer: 最终答案

规则：每次只输出一个 Action；不要编造 Observation（它由系统提供）；不要输出其他内容。"""

ACTION_RE = re.compile(r"^\s*Action:\s*(\w+)\s*\((.*)\)\s*$", re.M | re.S)
FINAL_RE = re.compile(r"^\s*Final Answer:\s*(.+)$", re.M)
FINISH_RE = re.compile(r"Action:\s*Finish\[(.*)\]", re.S)

#: 极简版不解析参数名，只能靠这张表把裸字符串塞进正确的参数里
ARG_NAME = {"calculator": "expression", "get_current_time": "timezone_name"}


def parse_action(text):
    """从模型输出里解析出 (工具名, 参数字符串)；解析失败返回 (None, None)。"""
    match = ACTION_RE.search(text)
    return (None, None) if not match else (match.group(1), match.group(2).strip())


def react_minimal(question, registry, max_steps=6):
    scratchpad = "Question: {}\n".format(question)
    for step in range(1, max_steps + 1):
        output = chat(REACT_PROMPT + "\n\n" + scratchpad, temperature=0)
        scratchpad += output.strip() + "\n"
        final = FINAL_RE.search(output) or FINISH_RE.search(output)
        if final:
            print("  第 {} 步 | Final Answer".format(step))
            return final.group(1).strip()
        name, arg = parse_action(output)
        if name is None:
            return "格式错误：模型没有给出合法的 Action。原始输出：\n{}".format(output)
        arg = arg.strip().strip("'\"")
        observation = registry.execute(name, {ARG_NAME.get(name, "input"): arg})
        scratchpad += "Observation: {}\n".format(observation)
        print("  第 {} 步 | {}({}) → {}".format(step, name, arg, short(observation, 70)))
    return "已达到最大步数 {}，仍未得到答案。".format(max_steps)


def part1_minimal() -> None:
    section("第 1 部分：几十行极简版 ReAct（文本约定：Thought / Action / Final Answer）")
    registry = ToolRegistry()
    registry.add("calculator", "计算数学表达式",
                 {"type": "object", "properties": {"expression": {"type": "string"}},
                  "required": ["expression"]},
                 lambda expression: str(eval(str(expression), {"__builtins__": {}}, {})))
    registry.add("get_current_time", "查询当前时间",
                 {"type": "object", "properties": {"timezone_name": {"type": "string"}}, "required": []},
                 lambda timezone_name="local": time.strftime("%Y-%m-%d %H:%M:%S"))
    # 用 inspect 数一下真实行数，证明「几十行」不是口号（空行与注释不计）
    def _lines(obj):
        return sum(1 for line in inspect.getsource(obj).splitlines()
                   if line.strip() and not line.strip().startswith("#"))
    prompt_lines = len([x for x in REACT_PROMPT.splitlines() if x.strip()])
    print("  代码量：核心循环 {} 行 + 解析函数 {} 行 + 提示词 {} 行 ≈ {} 行\n".format(
        _lines(react_minimal), _lines(parse_action), prompt_lines,
        _lines(react_minimal) + _lines(parse_action) + prompt_lines))

    if is_mock_mode():
        # 离线模式没有真模型，用 set_mock_script 预设它这三轮会写什么
        set_mock_script([
            {"content": "Thought: 先算乘法，精确计算必须用工具。\nAction: calculator(1234*5678)"},
            {"content": "Thought: 乘法算完了，再查一下当前时间。\nAction: get_current_time(local)"},
            {"content": "Thought: 两个信息都拿到了。\n"
                        "Final Answer: 1234*5678 = 7006652；当前时间见上一步的 Observation。"},
        ])
        print("  [离线] 模型的三轮输出由 set_mock_script 预设；在线模式下是它自己写的。")
    question = "帮我算一下 1234*5678，再告诉我现在几点"
    print("  题目：{}".format(question))
    print("  最终答案：{}\n".format(react_minimal(question, registry)))

    print("  ── 但这一版一上线就碎：正则解析不了模型的自由发挥 ──")
    broken = [("加了加粗", "**Action:** calculator(1+1)"),
              ("被代码块包住", "```\nAction: calculator(1+1)\n```"),
              ("参数写成 Python 字面量", "Action: read_file({'path': 'a.md'})"),
              ("参数跨了多行", "Action: calculator(1 +\n 1)")]
    for label, text in broken:
        parsed = parse_action(text)
        verdict = "解析失败（正则不认这个格式）" if parsed[0] is None else \
            "解析成 {}({!r})：名字对了，参数还得自己猜类型".format(parsed[0], parsed[1])
        print("    {:<18} → {}".format(label, verdict))
    print("\n  三个致命问题：① 格式只是「建议」，模型可以不遵守；② 解析正则越补越长，最后变成")
    print("  一个半吊子自然语言解析器；③ 参数没有类型约束，只能靠猜。")
    print("  解法不是更聪明的正则，而是换协议 —— 见第 2 部分。")


# ============================================================================
# 第 2 部分：健壮版 —— 原生 Function Calling 承载 Action，content 承载 Thought
# ============================================================================

REACT_SYSTEM = """你是一个会用工具的助手。规则：
1. 每一轮先用一两句话写清楚你的思考：现在要做什么、为什么这么做、有没有别的可能。
2. 如果需要使用工具，紧接着调用它（可以一次调用多个）。
3. 如果信息已经足够，直接用中文给出最终答案，不要再调用工具。
4. 不要在思考里编造工具结果 —— 你没有执行任何工具，结果由系统返回给你。
5. 如果上一次工具调用失败了，先判断原因：是可以改参数重试，还是应该换个方法。"""


class StopReason:
    """停止原因。前 5 个是正文点名的五道闸门，FINISH 是唯一正常出口。"""
    FINISH = "finish"                 # 模型给了最终答案
    MAX_STEPS = "max_steps"           # ① 步数上限
    REPEAT_ACTION = "repeat_action"   # ② 同一动作重复
    NO_PROGRESS = "no_progress"       # ③ 观察结果连续无变化
    BUDGET = "budget"                 # ④ 成本上限
    TIMEOUT = "timeout"               # ⑤ 墙上时钟超时


def assistant_message(response: dict) -> dict:
    """统一格式的响应 → API 原生的 assistant 消息（arguments 必须是 JSON 字符串）。"""
    calls = [{"id": item["id"], "type": "function",
              "function": {"name": item["name"],
                           "arguments": json.dumps(item.get("arguments", {}), ensure_ascii=False)}}
             for item in response.get("tool_calls") or []]
    return {"role": "assistant", "content": response.get("content") or "", "tool_calls": calls}


def signature(name: str, arguments: dict) -> str:
    """动作签名：参数 sort_keys 排序，避免键序不同导致漏判「同一个动作」。"""
    return "{}|{}".format(name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))


def dump_messages(messages: list, step=None) -> None:
    """调试神器：99% 的「Agent 不动了」，答案都在这份 messages 里。"""
    print("  Step {} | 共 {} 条消息".format(step, len(messages)))
    for index, msg in enumerate(messages):
        calls = "".join(" {}({})".format(c["function"]["name"], c["function"]["arguments"])
                        for c in msg.get("tool_calls") or [])
        content = (msg.get("content") or "").replace("\n", "\\n")
        print("    [{}] {}: {}{}".format(index, msg.get("role"), content[:100], calls[:120]))


class ReActAgent:
    """
    ReAct 的健壮实现：Action 走 tool_calls（结构化协议，不可能解析失败），Thought 走 content。

    五种停止条件都写在这里，每条都是独立的 if 和明确的停止原因字符串 —— 这样 Agent
    永远不会无限跑下去，而且停下来时你能立刻知道是为什么。
    """

    def __init__(self, registry: ToolRegistry, llm, trace: Trace, max_steps: int = DEFAULT_MAX_STEPS,
                 max_cost: float = DEFAULT_MAX_COST, max_seconds: float = DEFAULT_MAX_SECONDS,
                 repeat_limit: int = DEFAULT_REPEAT_LIMIT, no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT):
        self.registry, self.llm, self.trace = registry, llm, trace
        self.max_steps, self.max_cost, self.max_seconds = max_steps, max_cost, max_seconds
        self.repeat_limit, self.no_progress_limit = repeat_limit, no_progress_limit

    def stop(self, reason: str, detail: str, steps: int, spent: float) -> dict:
        print("  [停止] 因为「{}」停下：{}".format(reason, detail))
        self.trace.log("stop", reason=reason, detail=detail, steps=steps, spent=round(spent, 6))
        return {"answer": None, "stop_reason": reason, "detail": detail, "steps": steps, "spent": spent}

    def run(self, question: str) -> dict:
        messages = [{"role": "user", "content": question}]
        schemas = self.registry.to_openai_schema()
        action_counter, same_count, last_observation, spent = Counter(), 0, None, 0.0
        started = time.time()

        for step in range(1, self.max_steps + 1):
            if time.time() - started > self.max_seconds:                        # ⑤ 超时
                return self.stop(StopReason.TIMEOUT, "运行 {:.2f} 秒，超过上限 {} 秒".format(
                    time.time() - started, self.max_seconds), step, spent)
            response = self.llm.chat(messages, tools=schemas)
            usage = response["usage"]
            spent += estimate_cost(usage["prompt_tokens"], usage["completion_tokens"], self.llm.model)
            self.trace.llm_call(step, response)
            if spent > self.max_cost:                                           # ④ 成本
                return self.stop(StopReason.BUDGET, "已花费 {}，达到上限 {}".format(
                    format_cost(spent), format_cost(self.max_cost)), step, spent)

            thought, tool_calls = (response["content"] or "").strip(), response["tool_calls"]
            print("\n  ── 第 {} 步 ─────────────────────────".format(step))
            print("  [思考] {}".format(short(thought, 120) if thought else "（模型这一步没写思考）"))
            if not tool_calls:                                                  # 唯一正常出口
                print("  [行动] 不再调用工具，输出最终答案\n  [答案] {}".format(
                    short(response["content"], 200)))
                self.trace.log("finish", step=step, reason=StopReason.FINISH)
                return {"answer": response["content"], "stop_reason": StopReason.FINISH,
                        "detail": "模型给出最终答案", "steps": step, "spent": spent}

            for item in tool_calls:                                             # ② 重复动作
                key = signature(item["name"], item["arguments"])
                action_counter[key] += 1
                if action_counter[key] >= self.repeat_limit:
                    return self.stop(StopReason.REPEAT_ACTION, "工具 {} 用完全相同的参数被调用了 {} 次".format(
                        item["name"], action_counter[key]), step, spent)

            messages.append(assistant_message(response))
            observations = []
            for item in tool_calls:
                result = self.registry.execute(item["name"], item["arguments"])
                if action_counter[signature(item["name"], item["arguments"])] >= 2:
                    result = ("{}\n[提醒] 你已经用完全相同的参数调用过 {}，如果这次仍然没有进展，"
                              "请换参数或换工具。".format(result, item["name"]))
                print("  [行动] {}({})\n  [观察] {}".format(
                    item["name"], json.dumps(item["arguments"], ensure_ascii=False), short(result)))
                self.trace.tool_call(step, item["name"], item["arguments"], result, 0.0)
                observations.append(str(result))
                messages.append({"role": "tool", "tool_call_id": item["id"], "content": str(result)})

            observation = "\n".join(observations)
            if observation == last_observation:                                 # ③ 无进展
                same_count += 1
            else:
                same_count, last_observation = 1, observation
            if same_count >= self.no_progress_limit:
                return self.stop(StopReason.NO_PROGRESS,
                                 "连续 {} 次观察结果完全相同".format(same_count), step, spent)
            if FLAGS["trace"]:
                dump_messages(messages, step)

        return self.stop(StopReason.MAX_STEPS,                                  # ① 步数上限
                         "已达到最大步数 {}".format(self.max_steps), self.max_steps, spent)


def build_react_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add("calculator", "计算一个纯数学表达式并返回精确结果，例如 1234*5678。",
                 {"type": "object", "properties": {"expression": {
                     "type": "string", "description": "数学表达式，只允许数字和 + - * / ( ) 运算符"}},
                  "required": ["expression"]},
                 lambda expression: str(eval(str(expression), {"__builtins__": {}}, {})))
    registry.add("get_current_time", "获取当前日期和时间。当用户问「现在几点」时使用。",
                 {"type": "object", "properties": {"timezone_name": {
                     "type": "string", "description": "local 或 UTC"}}, "required": []},
                 lambda timezone_name="local": time.strftime("%Y-%m-%d %H:%M:%S"))
    return registry


def part2_native_fc(llm, trace) -> None:
    section("第 2 部分：健壮版 —— 原生 Function Calling 承载 Action，content 承载 Thought")
    registry = build_react_registry()
    print("  与极简版的区别只有两处：Action 用 tool_calls（协议字段，不可能解析失败），")
    print("  Thought 用 content（模型自愿写，偶尔会偷懒）；提示词里写清了「先想再做」。")
    print("  工具清单：{}".format(registry.names()))
    question = "帮我算一下 1234*5678 等于多少，再告诉我现在几点"
    print("  题目：{}".format(question))
    result = ReActAgent(registry, llm, trace).run(question)
    print("\n  [结果] 停止原因={}，步数={}，累计成本={}".format(
        result["stop_reason"], result["steps"], format_cost(result["spent"])))
    print("  观察重点：① 每轮 Thought 说了什么；② 观察结果变了之后，下一步策略有没有跟着变。")


# ============================================================================
# 第 3 部分：五种停止条件 —— 每种都构造一个能稳定触发它的场景
# ============================================================================

def call(name: str, arguments: dict) -> dict:
    """构造一条预设的工具调用（id 递增，模拟协议里每个申请都有独立编号）。"""
    call.counter += 1
    return {"id": "mock_call_{}".format(call.counter), "name": name, "arguments": arguments}


call.counter = 0


def turn(thought: str, *calls: dict) -> dict:
    return {"content": thought, "tool_calls": list(calls)}


def scenario(title, question, script, expect, **kwargs) -> dict:
    return {"title": title, "question": question, "script": script,
            "expect": expect, "kwargs": kwargs}


def slow_lookup(keyword: str) -> str:
    """故意很慢的工具（真实项目里是一次外部 API），用来演示墙上时钟超时。"""
    time.sleep(0.25)
    return "慢检索完成：{}".format(keyword)


SCENARIOS = [
    scenario("① 最大步数：每步都换动作、永远不给答案 → 只有步数上限拦得住", "帮我连算几道题",
             [turn("先算第一道。", call("calculator", {"expression": "1+1"})),
              turn("再算第二道。", call("calculator", {"expression": "2+2"})),
              turn("再算第三道。", call("calculator", {"expression": "3+3"}))],
             StopReason.MAX_STEPS, max_steps=3),
    scenario("② 重复动作：同一工具 + 同一参数出现 3 次（第 2 次只警告）", "帮我算 1/0",
             [turn("再试一次。", call("calculator", {"expression": "1/0"}))] * 3,
             StopReason.REPEAT_ACTION),
    scenario("③ 无进展：换了动作，观察结果却完全一样（换汤不换药）", "帮我算一下这几个除法",
             [turn("试试 1/0。", call("calculator", {"expression": "1/0"})),
              turn("换个分母再试。", call("calculator", {"expression": "2/0"})),
              turn("再换一个。", call("calculator", {"expression": "3/0"}))],
             StopReason.NO_PROGRESS),
    scenario("④ 成本上限：AGENT_BUDGET_CNY 默认 0.5 元（这里临时调到 0.0005 演示）",
             "帮我连续算几道题",
             [turn("继续算。", call("calculator", {"expression": "{}*{}".format(i, i)}))
              for i in range(1, 6)],
             StopReason.BUDGET, max_cost=0.0005),
    scenario("⑤ 超时：默认 120 秒（这里临时设成 0.3 秒，每步调用一个睡 0.25 秒的慢工具）",
             "帮我慢慢查几个关键词",
             [turn("查第一个。", call("slow_lookup", {"keyword": "react"}))] * 4,
             StopReason.TIMEOUT, max_seconds=0.3),
]


def part3_stop_conditions(llm, trace) -> None:
    section("第 3 部分：五种停止条件（本章重点）—— 每一种真的会被触发")
    print("  为什么必须全上：max_steps 只能兜底；拦住「同动作重复」的是第 2 条；")
    print("  拦住「换工具不换思路」的是第 3 条；烧钱和挂死各有专门的一道闸门。")

    for item in SCENARIOS:
        section(item["title"])
        print("  题目：{}".format(item["question"]))
        registry = build_react_registry()
        if "slow_lookup" in json.dumps(item["script"]):
            registry.add("slow_lookup", "一个很慢的外部检索接口，仅演示超时保护时使用。",
                         {"type": "object", "properties": {"keyword": {"type": "string"}},
                          "required": ["keyword"]}, slow_lookup)
        if is_mock_mode():
            set_mock_script(item["script"])
        result = ReActAgent(registry, llm, trace, **item["kwargs"]).run(item["question"])
        hit = "命中" if result["stop_reason"] == item["expect"] else "未命中（期望 {}）".format(item["expect"])
        print("  自检：期望停止原因 {} → {}；用了 {} 步，花了 {}".format(
            item["expect"], hit, result["steps"], format_cost(result["spent"])))

    print("\n  五道闸门的推荐默认值（正文 4.3 节）：步数 8 ｜ 同签名第 3 次停 ｜ 观察连续相同 3 次停")
    print("  ｜ 单任务成本 ¥0.5 ｜ 单任务 120 秒。加起来每步不到 10 行代码，收益是永远不会无限跑下去。")


# ============================================================================
# 第 4 部分：收尾 —— 两个摘要告诉你这一趟花了多少、为什么停
# ============================================================================

def part4_summary(llm, traces: list) -> None:
    section("第 4 部分：Trace 摘要与用量汇总")
    for trace in traces:
        print(trace.summary())
        print("")
    print(llm.usage_summary())
    print("  （第 1 部分用的是 chat() 便捷函数，它每次新建客户端，所以不计入这份累计）")
    print("  一次运行至少要能回答三个问题：几步、花了多少、为什么停 —— 这三个数字都在上面了。")


def main() -> int:
    parser = argparse.ArgumentParser(description="第 05 章 ReAct 最小 Agent Loop")
    parser.add_argument("--part", choices=["1", "2", "3", "all"], default="all",
                        help="选择要跑哪一部分（默认 all）")
    parser.add_argument("--trace", action="store_true", help="打印每一步的完整 messages（调试用）")
    args = parser.parse_args()

    load_env()
    FLAGS["trace"] = args.trace
    print_banner("第 05 章 · ReAct 最小 Agent Loop")
    llm = get_llm(temperature=0.2, system=REACT_SYSTEM)
    traces = []

    if args.part in ("1", "all"):
        part1_minimal()
    if args.part in ("2", "all"):
        traces.append(Trace("ch05_react_main"))
        part2_native_fc(llm, traces[-1])
    if args.part in ("3", "all"):
        traces.append(Trace("ch05_stop_conditions"))
        part3_stop_conditions(llm, traces[-1])
    if traces:
        part4_summary(llm, traces)

    section("本章要点回顾")
    print("  1. ReAct = Reasoning + Acting：显式的 Thought 是模型能自我纠正的物理载体。")
    print("  2. 极简版只要几十行：格式约束提示词 + 正则解析 + 观察回填 + Final Answer 终止。")
    print("  3. 但文本约定一上线就碎 —— 换协议才治本：Action 走 tool_calls，Thought 走 content。")
    print("  4. 五种停止条件全上（步数/重复动作/无进展/成本/超时），每条都要有独立的 if 和")
    print("     明确的停止原因，停下来时能立刻知道「因为什么停了」。")
    print("  5. 重复动作检测：第 2 次警告并喂回，第 3 次才终止 —— 永远给一次改的机会。")
    print("  6. 失败恢复的前提是分清「能不能救」：可重试的退避重试，不可重试的换参数或放弃。")
    print("  7. Agent 不动了：先看 messages 和工具 schema，最后才怀疑模型。")
    print("  8. 一个 5 步 Agent ≈ 6 次请求、几千 token，其中约 80% 的 prompt 是重复发送的历史。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
