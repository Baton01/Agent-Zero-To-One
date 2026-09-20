"""
第 01 章 · 认识 AI Agent 配套代码

本章学到什么：
  - 「一次问答」和「一个循环」的差别不在模型强弱，而在「下一步做什么」由谁决定
  - Agent 循环只有四步：observe（代码）→ think（模型）→ act（代码）→ observe（代码）
  - 四种形态（Chatbot / Workflow / Agent / Multi-Agent）的取舍：能用 Workflow 就别上 Agent

怎么跑：
    cd code
    AGENT_MOCK=1 python ch01_first_call.py     # 离线模式，不需要 API Key
    python ch01_first_call.py                  # 在线模式，需要 .env 里配好 Key
"""

from __future__ import annotations

import json
import os
import sys
import time

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (  # noqa: E402
    estimate_cost,
    format_cost,
    get_llm,
    is_mock_mode,
    load_env,
    print_banner,
    set_mock_script,
)
from common.tools import ToolRegistry  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

QUESTION = "订单 DP1002 六天了还没发货，我要退款，该怎么回复？"


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


# ============================================================================
# 实验 A：一次问答 —— 模型只能「说」，它不知道 DP1002 的真实状态
# ============================================================================

def experiment_a() -> dict:
    section("实验 A：一次问答（1 次模型调用，模型只负责把输入变成更好的文本）")
    llm = get_llm(temperature=0.2)
    messages = [{"role": "user", "content": QUESTION}]
    print("── 请求（这就是要发出去的 messages）──")
    print(json.dumps(messages, ensure_ascii=False, indent=2))

    started = time.time()
    response = llm.chat(messages)
    elapsed = time.time() - started

    print("── 响应 ──")
    print("回答：{}".format((response["content"] or "").replace("\n", " ")[:160]))
    usage = response["usage"]
    print("调用次数：1    输入 token：{}    输出 token：{}    耗时：{:.2f} 秒".format(
        usage["prompt_tokens"], usage["completion_tokens"], elapsed))
    print("估算成本：{}".format(format_cost(estimate_cost(usage["prompt_tokens"], usage["completion_tokens"]))))
    print("→ 注意：它的回答里如果出现订单状态，那是编的 —— 它没有查订单的能力。")
    return {"calls": 1, "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"]}


# ============================================================================
# 实验 B：手工模拟 Agent 循环 —— 这段代码里没有模型，但循环的形状是真的
# ============================================================================

FAKE_ORDERS = {
    "DP1001": {"status": "已发货", "days": 2},
    "DP1002": {"status": "未发货", "days": 6},
}
FAKE_POLICY = {"未发货超过 5 天": "可以无条件退款，无需客户举证"}


def tool_search_order(order_id: str) -> dict:
    """工具 1：查订单。真实项目里这是一次 HTTP 请求。"""
    return FAKE_ORDERS.get(order_id, {"error": "订单不存在"})


def tool_get_policy(topic: str) -> dict:
    """工具 2：查政策。真实项目里这是知识库检索（第 08 章）。"""
    return FAKE_POLICY.get(topic, {"error": "未收录该政策"})


TOOLS = {"search_order": tool_search_order, "get_policy": tool_get_policy}


def fake_llm(messages: list) -> dict:
    """假模型：用规则替我们「想」。真模型在这里是 llm.chat(messages, tools=...)。

    它只看**工具返回的观察结果**（role == "tool" 的消息），不看用户原话 ——
    否则用户消息里恰好出现的关键词会让它「假装已经知道了」。
    """
    observed = "\n".join(str(m["content"]) for m in messages if m.get("role") == "tool")
    if "status" not in observed:
        return {"thought": "还不知道订单状态，先查订单", "tool": "search_order",
                "args": {"order_id": "DP1002"}}
    if "未发货" in observed and "退款" not in observed:
        return {"thought": "未发货已 6 天，需要确认退款政策", "tool": "get_policy",
                "args": {"topic": "未发货超过 5 天"}}
    return {"thought": "事实齐了，可以写答复", "tool": None, "args": {}}


def experiment_b(max_steps: int = 5) -> dict:
    section("实验 B：手工模拟的 Agent 循环（纯 Python，没有模型，只有循环骨架）")
    messages = [{"role": "user", "content": QUESTION}]
    steps = 0
    for step in range(1, max_steps + 1):
        print("── 第 {} 轮 ─────────────────────────".format(step))
        decision = fake_llm(messages)                                   # think
        print("[think] " + decision["thought"])
        if decision["tool"] is None:                                    # 停止条件
            print("[done ] 模型不再需要工具，任务结束")
            steps = step
            break
        result = TOOLS[decision["tool"]](**decision["args"])            # act
        print("[act  ] {}({}) → {}".format(decision["tool"], decision["args"], result))
        # observe：把行动结果作为新的观察拼回上下文，进入下一轮
        messages.append({"role": "tool", "name": decision["tool"], "content": str(result)})
        steps = step
    else:
        print("[stop ] 达到 {} 轮上限，强制结束（防止无限循环）".format(max_steps))
    print("这一段没有调用任何模型，但 observe → think → act → observe 的形状和真 Agent 完全一样。")
    return {"steps": steps, "messages": messages}


# ============================================================================
# 实验 C：把一个真模型接到同一个循环上 —— 循环的形状不变，只是决策者换了
# ============================================================================

def build_order_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add(
        "search_order",
        "按订单号查询订单状态，返回状态与已下单天数。当用户提到具体订单号时需要它。",
        {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "订单号，格式 DP+4 位数字，例如 DP1002"}},
         "required": ["order_id"]},
        lambda order_id: json.dumps(FAKE_ORDERS.get(order_id, {"error": "订单不存在"}), ensure_ascii=False),
    )
    registry.add(
        "get_policy",
        "查询售后政策。当需要确认「能不能退款」「多久到账」这类规则时使用。",
        {"type": "object", "properties": {
            "topic": {"type": "string", "description": "政策主题，例如 未发货超过 5 天"}},
         "required": ["topic"]},
        lambda topic: FAKE_POLICY.get(topic, json.dumps({"error": "未收录该政策"}, ensure_ascii=False)),
    )
    return registry


def assistant_message(response: dict) -> dict:
    """
    把统一格式的响应转成 API 原生的 assistant 消息。

    两个必须记住的点：① 这条消息要**原样加回历史**，否则模型看不到自己上轮说过什么；
    ② arguments 在消息历史里必须是 JSON 字符串（协议如此），不是对象。
    """
    calls = []
    for call in response.get("tool_calls") or []:
        calls.append({
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"],
                         "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False)},
        })
    return {"role": "assistant", "content": response.get("content") or "", "tool_calls": calls}


def experiment_c(max_steps: int = 6) -> dict:
    section("实验 C：把同一个循环接到真模型上（离线模式用预设决策代替模型）")
    registry = build_order_registry()
    print("工具清单（模型看到的就是这些）：")
    print(registry.describe())

    if is_mock_mode():
        # 离线模式没有「真模型」，用 set_mock_script 预设三步决策，
        # 这样循环、工具执行、token 统计走的都是真实代码路径。
        set_mock_script([
            {"content": "先查订单真实状态。",
             "tool_calls": [{"id": "c1", "name": "search_order", "arguments": {"order_id": "DP1002"}}]},
            {"content": "未发货 6 天了，需要确认退款政策。",
             "tool_calls": [{"id": "c2", "name": "get_policy", "arguments": {"topic": "未发货超过 5 天"}}]},
            {"content": "事实齐了：未发货 6 天可以无条件退款，草稿如下……", "tool_calls": []},
        ])
        print("\n[离线] 模型的三步决策由 set_mock_script 预设；在线模式下这三步是它自己决定的。")

    llm = get_llm(temperature=0.2, system="你是客服处理助手。先查事实，再写草稿；查不到就转人工。")
    messages = [{"role": "user", "content": QUESTION}]
    tools_schema = registry.to_openai_schema()
    calls = 0

    for step in range(1, max_steps + 1):
        response = llm.chat(messages, tools=tools_schema)
        calls += 1
        tool_calls = response["tool_calls"]
        if not tool_calls:
            print("── 第 {} 轮：模型不再请求工具，输出最终答复".format(step))
            print("最终答复：{}".format((response["content"] or "").replace("\n", " ")[:120]))
            break
        print("── 第 {} 轮：模型 → tool_calls({})".format(
            step, ", ".join(c["name"] for c in tool_calls)))
        messages.append(assistant_message(response))
        for call in tool_calls:
            result = registry.execute(call["name"], call["arguments"])
            print("[act] {}({}) → {}".format(call["name"], call["arguments"], str(result)[:70]))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})

    usage = {"prompt_tokens": llm.total_prompt_tokens, "completion_tokens": llm.total_completion_tokens}
    print("调用次数：{}    累计输入 token：{}（每轮都要重发历史）    输出 token：{}".format(
        calls, usage["prompt_tokens"], usage["completion_tokens"]))
    print("估算成本：{}".format(format_cost(estimate_cost(usage["prompt_tokens"], usage["completion_tokens"]))))
    return {"calls": calls, "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"]}


def print_compare_table(result_a: dict, result_c: dict) -> None:
    section("对比表：一次问答 vs 一个循环")
    rows = [
        ("模型调用次数", str(result_a["calls"]), str(result_c["calls"])),
        ("累计输入 token", str(result_a["prompt_tokens"]), str(result_c["prompt_tokens"])),
        ("累计输出 token", str(result_a["completion_tokens"]), str(result_c["completion_tokens"])),
        ("拿到真实订单状态", "不能（只能编）", "能（先查再说）"),
        ("下一步由谁决定", "人", "模型"),
        ("成本与延迟", "低、稳定", "高、不定（token 是问答的数倍）"),
    ]
    print("{:<18}| {:<16}| {}".format("维度", "一次问答", "一个循环"))
    print("-" * 64)
    for row in rows:
        print("{:<18}| {:<16}| {}".format(row[0], row[1], row[2]))
    print("-" * 64)
    print("结论：循环的价值 = 让模型能拿到它不知道的事实；循环的代价 = 成本与延迟成倍增加。")


# ============================================================================
# 四形态判断：把正文第 4.1 节的决策清单写成可运行的小函数
# ============================================================================

#: 六道闸门：命中任何一条，就不该上 Agent（顺序即优先级）
CHECKLIST = [
    ("steps_enumerable", "步骤能提前画成一张没有回头箭的流程图吗？", "Workflow",
     "分支多也不怕，用 dict 路由即可"),
    ("rules_in_code", "规则能写成 if / 正则 / SQL 吗？", "普通代码",
     "别引入模型，又快又准又免费"),
    ("need_100_percent", "需要 100% 一致吗（对账、扣款、删数据、发通知）？", "代码 + 人工确认",
     "模型最多做建议，执行必须由代码兜住"),
    ("latency_critical", "延迟必须小于 1 秒吗？", "规则 / 缓存 / 小模型",
     "Agent 的多轮往返做不到"),
    ("high_cost_of_error", "出错代价高且没有人工审核环节吗？", "不要自动化",
     "或者只产出草稿，让有副作用的那一步停在这里"),
    ("cannot_define_done", "说不清「什么算做完」吗？", "先写验收标准",
     "验收标准比架构更靠前"),
]


def choose_form(task: dict) -> dict:
    """按正文 4.1 的清单逐条判断，返回推荐形态与理由。"""
    for key, question, form, note in CHECKLIST:
        if task.get(key):
            return {"form": form, "reason": question + " → " + note}
    if task.get("branches_unpredictable") and task.get("high_value_per_task"):
        return {"form": "Agent", "reason": "步骤取决于中间结果、依赖组合事先说不清 → 把决策权交给模型"}
    return {"form": "Chatbot", "reason": "步骤简单、人愿意自己动手 → 不要过度设计"}


def demo_four_forms() -> None:
    section("四形态判断：给一个真实需求，先问「该不该用 Agent」")
    tasks = [
        {"name": "每天 300 封客户邮件：读邮件 → 分类 → 按模板生成草稿 → 人工确认",
         "steps_enumerable": True},
        {"name": "每月对账：金额必须分毫不差，不能有模型自由发挥",
         "need_100_percent": True},
        {"name": "实时报价接口：要求 200 毫秒内返回",
         "latency_critical": True},
        {"name": "调研一个新方向：搜到什么决定下一步搜什么，单份报告价值 50 元",
         "branches_unpredictable": True, "high_value_per_task": True},
    ]
    for task in tasks:
        picked = choose_form(task)
        print("  · 需求：{}".format(task["name"]))
        print("    推荐形态：{}".format(picked["form"]))
        print("    理由：{}".format(picked["reason"]))

    print("\n四种形态速查（判断标准只有一个：下一步做什么由谁决定）：")
    table = [
        ("形态", "谁决定下一步", "单封邮件调用次数", "什么时候选它"),
        ("Chatbot", "人", "1", "人愿意自己动手、量不大"),
        ("Workflow", "代码", "2（固定）", "步骤能画成流程图"),
        ("Agent", "模型", "3-10（不定）", "分支说不清、依赖中间结果"),
        ("Multi-Agent", "多个模型各管一段", "6-20", "职责边界真的冲突"),
    ]
    for row in table:
        print("  {:<12}{:<18}{:<18}{}".format(*row))

    workflow_cost = estimate_cost(700, 400)
    agent_cost = estimate_cost(4250, 1000)
    print("\n成本对照（示例价表，单封邮件）：Workflow（2 次调用）{}  vs  Agent（5 轮）{}".format(
        format_cost(workflow_cost), format_cost(agent_cost)))
    print("→ 选错架构的代价是几周时间，选错参数的代价是几块钱。")


def main() -> int:
    load_env()
    print_banner("第 01 章 · 一次问答 vs 一个循环")

    result_a = experiment_a()
    experiment_b()
    result_c = experiment_c()
    print_compare_table(result_a, result_c)
    demo_four_forms()

    section("本章要点回顾")
    print("  1. 四种形态的区别不在模型强弱，而在「下一步做什么」由谁决定：人 / 代码 / 模型 / 多模型。")
    print("  2. Agent 不是更聪明的 Workflow，它是把决策权交出去，用不确定性换灵活性。")
    print("  3. 循环四步：observe（代码）→ think（模型）→ act（代码）→ observe（代码）。")
    print("  4. 模型只负责「想」：它的行动只是一张申请表，真正执行的是你的代码（安全边界就在这）。")
    print("  5. 五个部件缺一不可：模型、工具、循环、状态、停止条件；最常被省掉的是停止条件。")
    print("  6. 判断顺序：先问「这件事需要模型做决定吗」，再问「怎么把它做得更强」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
