"""
第 02 章 · 大模型基础 配套代码

本章学到什么：
  - 按 Token 计费，不按字数：中文近乎一字一 Token，英文 4 个字符才 1 个
  - 上下文窗口 = 输入 + 输出；max_tokens 设太小会静默截断（JSON 直接残缺）
  - Agent 循环每轮都要重发全部历史，成本随轮数近似平方增长

怎么跑：
    cd code
    AGENT_MOCK=1 python ch02_tokens_cost.py     # 离线模式，不需要 API Key
    python ch02_tokens_cost.py                  # 在线模式，需要 .env 里配好 Key
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
    PRICE_TABLE_CNY,
    count_tokens_approx,
    estimate_cost,
    format_cost,
    get_llm,
    is_mock_mode,
    load_env,
    print_banner,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

#: 正文 4.1 节的示例价（元 / 百万 token），用来和正文里的绝对金额对账；
#: 本项目 common/llm.py 的价格表可能已随服务商调价更新，所以两个都算给你看。
DOC_PRICE_IN, DOC_PRICE_OUT = 2.0, 8.0

#: 循环放大的三个参数，取自正文 4.3 节：首轮输入 350、每轮新增 250、每轮输出 200
FIRST_PROMPT, STEP_INCREMENT, OUTPUT_PER_STEP = 350, 250, 200


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def pad(text: str, width: int) -> str:
    """按「中文算两列」补齐宽度，让含中文的表格也能对齐。"""
    shown = sum(2 if "\u4e00" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def table(header: tuple, rows: list, widths: tuple) -> None:
    print("  " + " ".join(pad(str(cell), w) for cell, w in zip(header, widths)))
    print("  " + "-" * (sum(widths) + len(widths)))
    for row in rows:
        print("  " + " ".join(pad(str(cell), w) for cell, w in zip(row, widths)))


def demo_token_density() -> None:
    section("第 1 步：Token 估算 —— 同样的意思，中文和英文差多少")
    sample_zh = "请帮我查一下订单 DP1002 的物流状态，谢谢。"
    sample_en = "Please check the shipping status of order DP1002."
    sample_code = (
        "def add(a, b):\n"
        "    \"\"\"返回两个数的和。\"\"\"\n"
        "    return a + b\n"
    )
    sample_json = json.dumps(
        {"order_id": "DP1002", "customer": "张三", "amount": 199, "currency": "CNY"},
        ensure_ascii=False,
    )
    # 一封约 500 字的客户邮件：用同一段话重复，保证字数是可控的
    mail_line = "尊敬的客服您好，我在贵店购买的订单 DP1002 已经下单六天，物流信息一直没有更新，我希望能尽快处理退款，谢谢。"
    sample_mail = mail_line * 9

    samples = [
        ("中文短句", sample_zh),
        ("英文同义句", sample_en),
        ("Python 代码", sample_code),
        ("JSON 片段", sample_json),
        ("中文长邮件", sample_mail),
    ]
    rows = []
    print("  估算函数：count_tokens_approx（中文 1 字≈1 token，其余 3.5 字符≈1 token，刻意偏高估）\n")
    for label, text in samples:
        tokens = count_tokens_approx(text)
        chars = len(text)
        rows.append((label, chars, tokens, "{:.2f}".format(chars / float(tokens))))
    table(("样本", "字符数", "token(估)", "字符/token"), rows, (14, 8, 10, 12))

    zh_tokens = count_tokens_approx(sample_zh)
    en_tokens = count_tokens_approx(sample_en)
    print("\n  同一句意思：中文 {} 字符 / {} token，英文 {} 字符 / {} token".format(
        len(sample_zh), zh_tokens, len(sample_en), en_tokens))
    print("  → 中文字符数只有英文的 {:.0%}，token 数却是英文的 {:.2f} 倍。".format(
        len(sample_zh) / float(len(sample_en)), zh_tokens / float(en_tokens)))
    print("  → 原因：中文几乎一字一个 token，英文 3.5-4 个字符才一个。")
    print("  → 工程折中：面向用户的内容用中文，内部的指令与 Schema 尽量用英文短标识符。")
    print("\n  经验换算（正文 1.2 节）：中文 1 字 ≈ 0.6-1 token；英文 1 token ≈ 4 字符；代码/JSON 1 token ≈ 3 字符。")


def cost_line(prompt_tokens: int, completion_tokens: int) -> str:
    """同时给出本项目价格表和正文示例价的两个数字，方便对照。"""
    project = format_cost(estimate_cost(prompt_tokens, completion_tokens))
    doc = (prompt_tokens * DOC_PRICE_IN + completion_tokens * DOC_PRICE_OUT) / 1_000_000.0
    return "{}（按正文示例价 ¥2/¥8 算是 {}）".format(project, format_cost(doc))


def simulate_loop(rounds: int) -> dict:
    """
    模拟 Agent 循环的 token 消耗：**每一轮都要把全部历史重新发一遍**。

    第 n 轮的输入 = 首轮输入 + 每轮新增 × (n−1)，累计输入是它的和 —— 对 n 而言是二次函数。
    返回每一步的明细，供打印和计算成本使用。
    """
    rows = []
    total_prompt = 0
    for round_index in range(1, rounds + 1):
        prompt_tokens = FIRST_PROMPT + STEP_INCREMENT * (round_index - 1)
        total_prompt += prompt_tokens
        rows.append((round_index, prompt_tokens, total_prompt))
    return {"rows": rows, "prompt": total_prompt, "completion": OUTPUT_PER_STEP * rounds}


def demo_cost_scenarios() -> None:
    section("第 2 步：成本计算 —— 四个真实场景")
    print("  本项目内置价格表（元 / 百万 token，仅用于建立量级直觉）：")
    for name in ("deepseek-chat", "qwen-plus", "glm-4-flash", "gpt-4o-mini"):
        price_in, price_out = PRICE_TABLE_CNY[name]
        print("    {:<16} 输入 {:<6} 输出 {}".format(name, price_in, price_out))

    rounds_5 = simulate_loop(5)
    rounds_20 = simulate_loop(20)
    scenarios = [
        ("一次问答（输入 350 / 输出 200）", 350, 200, 1),
        ("Workflow：分类+写草稿（2 次调用）", 700, 400, 1),
        ("Agent 5 轮（每轮重发历史）", rounds_5["prompt"], rounds_5["completion"], 1),
        ("20 轮对话 / Agent 20 轮", rounds_20["prompt"], rounds_20["completion"], 1),
        ("一次问答 × 1000 次（小服务）", 350, 200, 1000),
    ]
    rows = []
    for label, prompt_tokens, completion_tokens, times in scenarios:
        total = format_cost(estimate_cost(prompt_tokens, completion_tokens) * times)
        rows.append((label, prompt_tokens, completion_tokens, total))
    print("")
    table(("场景", "累计输入", "累计输出", "估算成本"), rows, (36, 10, 10, 12))
    print("\n  注意三点：① 输出单价是输入的 3-4 倍；② 循环场景的输入是「重发历史」堆出来的；")
    print("  ③ 按正文 4.2 节的示例价：Agent 5 轮约 ¥0.0165，是 Workflow（¥0.0046）的 3.6 倍。")
    print("  每天 300 封邮件 ≈ ¥5/天 ≈ ¥150/月；如果是每天 10 万条日志，架构选型直接决定成本能不能活下来。")


def demo_loop_amplification() -> None:
    section("第 3 步：Agent 循环的 token 放大效应（本步重点）")
    result_5 = simulate_loop(5)
    print("  假设：首轮输入 {} token，每轮新增 {} token（模型输出+工具结果），每轮输出 {} token。".format(
        FIRST_PROMPT, STEP_INCREMENT, OUTPUT_PER_STEP))
    print("  每一轮请求都要重发全部历史，所以第 n 轮的输入 = {} + {} × (n−1)\n".format(
        FIRST_PROMPT, STEP_INCREMENT))
    table(("轮次", "本轮输入 token", "累计输入 token", "对比：独立单轮只花"),
          [(r[0], r[1], r[2], FIRST_PROMPT * r[0]) for r in result_5["rows"]],
          (8, 16, 18, 24))

    result_20 = simulate_loop(20)
    print("\n  5 轮：累计输入 {}，输出 {} → 成本 {}".format(
        result_5["prompt"], result_5["completion"],
        cost_line(result_5["prompt"], result_5["completion"])))
    print("  20 轮：累计输入 {}，输出 {} → 成本 {}".format(
        result_20["prompt"], result_20["completion"],
        cost_line(result_20["prompt"], result_20["completion"])))
    ratio_tokens = result_20["prompt"] / float(result_5["prompt"])
    cost_5 = estimate_cost(result_5["prompt"], result_5["completion"])
    cost_20 = estimate_cost(result_20["prompt"], result_20["completion"])
    print("  → token 是 5 轮版的 {:.1f} 倍，成本是 {:.1f} 倍（轮数翻 4 倍，代价远不止 4 倍）。".format(
        ratio_tokens, cost_20 / cost_5 if cost_5 else 0.0))
    print("  → 公式：N 轮累计输入 ≈ N × 首轮 + 每轮增量 × N(N−1)/2 —— 随轮数**平方增长**。")
    print("  同一件事如果 10 次独立单轮：{} token；走 10 轮循环：{} token。".format(
        FIRST_PROMPT * 10, simulate_loop(10)["prompt"]))
    print("  这就是「给 Agent 设步数上限」的经济学理由：上限不是防死循环的补丁，而是成本闸门。")


def demo_temperature() -> None:
    section("第 4 步：temperature 对输出的影响")
    question = "给一家卖手冲咖啡的小店起 3 个名字，只输出名字。"
    if is_mock_mode():
        print("  离线模式下这条实验**没有意义**（Mock 不看 temperature，它按规则返回同一段文本）：")
        llm = get_llm()
        for temperature in (0.0, 0.9):
            reply = llm.chat([{"role": "user", "content": question}], temperature=temperature)["content"]
            print("    temperature={:<4} → {}".format(temperature, reply.replace("\n", " ")[:50]))
        print("  请在线跑一次并记录下面三件事：")
        print("    ① temperature=0   连续 3 次，输出是否完全一样；")
        print("    ② temperature=0.9 连续 3 次，输出是否更长、更发散、格式更不稳；")
        print("    ③ 抽取/分类/JSON 任务把 temperature 压到 0-0.3，文案创作才用 0.7-1.0。")
        print("  提醒：temperature=0 也不等于确定 —— 浮点累加、动态批处理、MoE 路由、副本差异、")
        print("  服务商悄悄更新模型版本，都会让两次输出不同。")
        return

    llm = get_llm()
    for temperature in (0.0, 0.9):
        collected = []
        for _ in range(3):
            collected.append(llm.chat([{"role": "user", "content": question}],
                                      temperature=temperature)["content"].strip())
        print("  temperature={:<4} 3 次输出中不同版本数：{}/3，平均长度 {} 字".format(
            temperature, len(set(collected)), sum(len(x) for x in collected) // 3))
    print("  → 温度低更稳定但可能死板；温度高更多样但格式更飘。Agent 的工具选择一律用 0-0.3。")


def cut_to_tokens(text: str, max_tokens: int) -> str:
    """反向使用估算函数：截出「恰好这么多 token」的前缀，模拟服务端的截断行为。"""
    for index in range(1, len(text) + 1):
        if count_tokens_approx(text[:index]) > max_tokens:
            return text[:index - 1]
    return text


def demo_max_tokens() -> None:
    section("第 5 步：max_tokens 截断 —— finish_reason 变成 length 时必须警觉")
    order_json = json.dumps(
        {"order_id": "DP1002", "customer": "张三", "amount": 199, "currency": "CNY",
         "note": "客户要求尽快退款，已经等待六天，希望优先处理，谢谢配合。"},
        ensure_ascii=False,
    )

    if is_mock_mode():
        print("  离线模式下 Mock 不做截断，所以这里先看一眼「真实调用的字段长什么样」：")
        llm = get_llm()
        response = llm.chat([{"role": "user", "content": "介绍一下你自己。"}], max_tokens=20)
        print("    max_tokens=20 → finish_reason={!r}，completion_tokens={}".format(
            response["finish_reason"], response["usage"]["completion_tokens"]))
        print("    → 仍为 stop，说明这个实验必须在线跑（Mock 只负责把代码路径跑通）。")
        print("\n  下面用本地模拟演示「服务端会怎么截断」，以及截断对 JSON 的致命影响：")

    limit = 20
    truncated = cut_to_tokens(order_json, limit)
    print("    max_tokens={} 时，服务端最多只给这么多内容：".format(limit))
    print("      {}".format(truncated))
    print("    被切断、永远拿不到的部分：{}".format(order_json[len(truncated):]))
    print("    finish_reason = length（模型没说完就被切断），completion_tokens = {}".format(limit))
    try:
        json.loads(truncated)
        print("    json.loads 居然成功了？（正常情况这里应该报错）")
    except ValueError as error:
        print("    json.loads(截断内容) → {}: {}".format(type(error).__name__, error))
    print("\n  工程结论：要求输出 JSON 时，max_tokens 要按「实测输出 × 1.5」来设，别抠；")
    print("  解析层必须包住 json.loads（第 03 章给完整的 safe_parse_json + 错误回灌重试）。")


def demo_streaming() -> None:
    section("第 6 步：流式 vs 非流式 —— 总时长不变，主观等待从 8 秒变成 0.6 秒")
    llm = get_llm()
    question = [{"role": "user", "content": "用三句话介绍 Python。"}]

    started = time.time()
    response = llm.chat(question)
    non_stream_elapsed = time.time() - started
    print("  非流式：等待 {:.2f} 秒后一次性拿到 {} 字".format(
        non_stream_elapsed, len(response["content"])))

    started = time.time()
    first_char_at = None
    chunks = 0
    for piece in llm.stream_chat(question):
        if first_char_at is None:
            first_char_at = time.time() - started
        chunks += 1
    total_elapsed = time.time() - started
    print("  流式  ：第 1 个字符出现在 {:.0f} 毫秒（共 {} 块），全部结束 {:.2f} 秒".format(
        (first_char_at or 0.0) * 1000, chunks, total_elapsed))
    print("  → 两个指标要分开看：TTFT（首字延迟）决定「卡不卡」，总延迟决定「读完要多久」。")
    print("  → 离线模式的耗时是本地模拟出来的；真实差异（网络 + 服务端排队）请在线对比。")


def main() -> int:
    load_env()
    print_banner("第 02 章 · Token / 成本 / 参数 / 流式")

    demo_token_density()
    demo_cost_scenarios()
    demo_loop_amplification()
    demo_temperature()
    demo_max_tokens()
    demo_streaming()

    section("本章要点回顾")
    print("  1. 按 Token 计费，不按字数：中文 1 字≈0.6-1 token，英文 1 token≈4 字符，代码/JSON≈3 字符。")
    print("  2. 上下文窗口 = 输入 + 输出；输入预算 = 窗口 − max_tokens − 安全余量，超了报 400。")
    print("  3. finish_reason 必须看：length = 被截断（JSON 会残缺），stop = 正常结束。")
    print("  4. 采样参数只调一个：抽取/分类 temperature=0，文案 0.7-1.0，top_p 一般不动。")
    print("  5. 输出单价是输入的 3-4 倍，让模型「少说废话」是真省钱。")
    print("  6. Agent 循环每轮重发历史，成本近似平方增长 —— 所以步数上限是成本闸门。")
    print("  7. 幻觉的根因是「知识缺失 + 没有出口」：给材料、要求引用并校验、事实走工具、")
    print("     给「不知道」的出口、后校验降级。")
    print("  8. 长文档必须切片：装不下、贵 60 倍、中段信息还会被忽略。")
    print("  9. 流式只改感知：TTFT 降到一秒内，总时长与费用都不变。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
