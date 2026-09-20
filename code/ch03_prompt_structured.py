"""
第 03 章 · Prompt 工程与结构化输出 配套代码

本章学到什么：
  - 分界线：输出必须能被代码解析 —— 「人看得懂」对程序等于 0 分
  - 六段式 Prompt：角色 / 任务 / 约束 / 输入 / 输出格式 / 示例（示例里必须有边界情况）
  - 校验层不能省：剥代码块 → 括号配对截断 → 引号/逗号修复 → 字段与类型校验
  - 重试要回灌错误：上次原文 + 具体错误信息，比「用同样的 prompt 再问一遍」有效得多

怎么跑：
    cd code
    AGENT_MOCK=1 python ch03_prompt_structured.py     # 离线模式，不需要 API Key
    python ch03_prompt_structured.py                  # 在线模式，需要 .env 里配好 Key
"""

from __future__ import annotations

import json
import os
import re
import sys

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (  # noqa: E402
    get_llm,
    is_mock_mode,
    load_env,
    print_banner,
    set_mock_script,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

EMAIL = "你好，订单 DP1002 麻烦退一下，一共 199 元，谢谢。"

#: 抽取任务的契约：哪些字段必须有、字段该是什么类型
REQUIRED_FIELDS = ["order_id"]
FIELD_TYPES = {"order_id": str, "customer": str, "amount": (int, float), "currency": str}


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def pad(text: str, width: int) -> str:
    """按「中文算两列」补齐宽度，让含中文的列表也能对齐。"""
    shown = sum(2 if "\u4e00" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" else 1 for ch in text)
    return text + " " * max(0, width - shown)


# ============================================================================
# 一、六段式 Prompt：每一段都在堵一个具体的失败
# ============================================================================

def build_prompt(email_text: str) -> str:
    """
    六段式模板。三个细节值得注意：
      ① 示例里故意给了一个 null —— 一个含边界情况的示例比十条约束更有效；
      ② 输入放在最后 —— 模型对靠后的内容注意力更强，而且静态前缀能命中缓存；
      ③ 用 <email> 把数据和指令隔开 —— 最低成本的防注入手段。
    """
    return """你是订单信息抽取器。

【任务】从 <email> 标签内的邮件正文中抽取订单信息。

【约束】
1. 只输出 JSON，不要输出任何解释、前后缀、Markdown 代码块。
2. 邮件里没有的信息填 null，不要猜测、不要编造。
3. amount 必须是数字（不带货币符号），currency 用三位大写字母代码。
4. order_id 保持原样，不要补全、不要改写大小写。

【输出格式】
{{"order_id": "string|null", "customer": "string|null", "amount": number|null, "currency": "string|null"}}

【示例】
输入：<email>你好，订单 DP1002 麻烦退一下，一共 199 元。</email>
输出：{{"order_id": "DP1002", "customer": null, "amount": 199, "currency": "CNY"}}

【输入】
<email>{email}</email>""".format(email=email_text)


def demo_bad_vs_good_prompt() -> None:
    section("第 1 步：坏 Prompt 的三种真实输出 vs 六段式好 Prompt")
    print("  坏 Prompt：帮我从这封邮件里提取订单信息：{}".format(EMAIL))
    print("  它可能给你这三种东西（人看得懂，程序全崩）：")
    print("    · 好的，我从这封邮件中提取到了以下信息：订单号是 DP1002，客户名称是张三，金额 199 元。")
    print("    · ```json {\"order_id\": \"DP1002\", ...} ```（外面包了代码块）")
    print("    · {\"订单号\": \"DP1002\", \"金额\": \"199\"}（字段名与类型都不对）")
    print("\n  好 Prompt（六段式，下面就是这次要发出去的原文）：")
    print("-" * 64)
    print(build_prompt(EMAIL))
    print("-" * 64)


# ============================================================================
# 二、校验层：safe_parse_json（正文 4.2 节的实现，一行都没简化）
# ============================================================================

class SchemaError(Exception):
    """模型输出能解析成 JSON，但字段不符合约定（缺字段 / 类型不对）。"""


def _strip_code_fence(text: str) -> str:
    """剥掉三个反引号包裹的代码块。用 {3} 写反引号，避免和文档里的代码块冲突。"""
    match = re.search(r"`{3}(?:json)?\s*(.*?)`{3}", text, re.S)
    return match.group(1).strip() if match else text.strip()


def _normalize_quotes(text: str) -> str:
    """中文引号 → 英文引号（最常见的手滑来源）。"""
    return (text.replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2018", "'").replace("\u2019", "'"))


def _cut_outer_braces(text: str) -> str:
    """
    从第一个 { 或 [ 开始，用括号配对找到配对的收尾，丢掉后面的解释文字。
    配对时会跳过字符串内部，避免被字符串里的括号骗到。
    """
    start = -1
    for index, ch in enumerate(text):
        if ch in "{[":
            start = index
            break
    if start < 0:
        return text
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text[start:]        # 没配对上（通常是被截断），交给 json.loads 报错


def safe_parse_json(text: str, required_fields=None, field_types=None) -> dict:
    """
    把模型的自由文本尽量变成 dict。失败抛 SchemaError。

    处理顺序就是正文第 4.1 节那张表的顺序：
      剥代码块外壳 → 中文引号 → 括号配对截断 → 去尾随逗号 → json.loads → 字段与类型校验
    """
    if not text or not text.strip():
        raise SchemaError("模型返回空内容")
    candidate = _cut_outer_braces(_normalize_quotes(_strip_code_fence(text)))
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)      # 去尾随逗号
    try:
        data = json.loads(candidate)
    except ValueError as error:
        raise SchemaError("JSON 解析失败：{} | 原文前 200 字：{}".format(error, candidate[:200]))

    if not isinstance(data, dict):
        raise SchemaError("顶层不是对象，而是 {}".format(type(data).__name__))

    for name in (required_fields or []):
        if name not in data:
            raise SchemaError("缺少必填字段：{}（实际字段：{}）".format(name, sorted(data.keys())))

    for name, wanted in (field_types or {}).items():
        value = data.get(name)
        if value is None:
            continue
        # 容错：数字被写成了字符串，尝试安全转换
        if wanted in ((int, float), (int,), (float,)) and isinstance(value, str):
            try:
                data[name] = float(value) if "." in value else int(value)
                continue
            except ValueError:
                raise SchemaError("字段 {} 应为数字，实际是无法转换的字符串 {!r}".format(name, value))
        if isinstance(value, bool) and wanted in ((int,), (float,), (int, float)):
            raise SchemaError("字段 {} 应为数字，实际是布尔值（bool 是 int 的子类，必须显式排除）".format(name))
        if not isinstance(value, wanted):
            raise SchemaError("字段 {} 应为 {}，实际是 {}".format(name, wanted, type(value).__name__))
    return data


#: 7 个「脏」响应：模拟真实模型会怎么破坏你的 JSON（正文 4.1 节九种现场的常客）
GOOD_JSON = '{"order_id": "DP1002", "customer": "张三", "amount": 199, "currency": "CNY"}'
DIRTY_CASES = [
    ("三个反引号包裹的代码块", "```json\n" + GOOD_JSON + "\n```"),
    ("前置解释文字", "好的，这是抽取结果：" + GOOD_JSON),
    ("尾随逗号", '{"order_id": "DP1002", "amount": 199,}'),
    ("中文引号", '{\u201corder_id\u201d: \u201cDP1002\u201d, "amount": 199}'),
    ("数字变成了字符串", '{"order_id": "DP1002", "amount": "199"}'),
    ("类型错误：布尔冒充数字", '{"order_id": "DP1002", "amount": true}'),
    ("缺字段：字段名漂移", '{"alert": "x", "amount": 199}'),
]


def demo_dirty_inputs() -> None:
    section("第 2 步：safe_parse_json 逐个修复 7 种「脏」输出")
    if not is_mock_mode():
        print("  这一节用 set_mock_script 构造脏响应，只在离线模式（AGENT_MOCK=1）下演示。")
        print("  在线模式下这些情况会偶发出现 —— 所以校验层必须一直在。")
        return

    llm = get_llm()
    # 把 7 个脏响应预设成「模型这 7 次调用的回答」，这样离线也能完整演示修复过程
    set_mock_script([{"content": text, "tool_calls": []} for _, text in DIRTY_CASES])

    fixed = 0
    for index, (label, raw) in enumerate(DIRTY_CASES, start=1):
        reply = llm.chat([{"role": "user", "content": "抽取订单信息"}])["content"]
        try:
            data = safe_parse_json(reply, required_fields=REQUIRED_FIELDS, field_types=FIELD_TYPES)
            fixed += 1
            print("  [{}] {} → 通过：{}".format(
                index, pad(label, 24), json.dumps(data, ensure_ascii=False)))
        except SchemaError as error:
            print("  [{}] {} → 拒绝：{}".format(index, pad(label, 24), error))
    print("\n  7 个里 {} 个能救回来，{} 个必须重试 —— 校验层的作用就是「不让错误静默通过」。".format(
        fixed, len(DIRTY_CASES) - fixed))
    print("  提示：尾部被截断的 JSON（max_tokens 太小）救不回来，只能调大 max_tokens 后重试。")


# ============================================================================
# 三、重试：把「上次原文 + 具体错误」一起发回去
# ============================================================================

def extract_order(email_text: str, llm, max_attempts: int = 3) -> dict:
    """
    抽取订单信息：解析失败就把错误回灌给模型重试。

    重试不是「用同样的 prompt 再问一遍」—— 那样只是碰运气。有效的做法是把
    上一次的**原文**和**具体错误**一起发回去，等于给模型一条明确的修改指令。
    """
    prompt = build_prompt(email_text)
    last_raw = ""
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": last_raw},          # 模型上次的原话
                {"role": "user", "content":
                 "上一次的输出无法被解析，错误信息：{}\n"
                 "请重新只输出一个 JSON 对象，第一个字符必须是 {{，不要任何其他文字。".format(last_error)},
            ]
        # 首次求稳用 temperature=0；重试时略微放开到 0.2，避免卡在同一个错法上
        last_raw = llm.chat(messages, temperature=0 if attempt == 1 else 0.2)["content"]
        try:
            data = safe_parse_json(last_raw, required_fields=REQUIRED_FIELDS, field_types=FIELD_TYPES)
            print("  [第 {} 次尝试] 成功：{}".format(attempt, json.dumps(data, ensure_ascii=False)))
            return data
        except SchemaError as error:
            last_error = str(error)
            print("  [第 {} 次尝试] 失败：{}".format(attempt, last_error))
    raise SchemaError("重试 {} 次仍失败，最后一次错误：{}".format(max_attempts, last_error))


def demo_retry() -> None:
    section("第 3 步：校验失败后带着错误重试")
    llm = get_llm()
    if is_mock_mode():
        set_mock_script([
            {"content": '{"订单号": "DP1002"}', "tool_calls": []},   # 第 1 次：字段名漂移，必然失败
            {"content": GOOD_JSON, "tool_calls": []},                # 第 2 次：模型按错误提示改对了
        ])
        print("  [离线] 两次响应由 set_mock_script 预设：第一次故意给错字段名，第二次改对。")
    try:
        extract_order(EMAIL, llm)
    except SchemaError as error:
        print("  最终失败：{}".format(error))

    print("\n  回灌时发给模型的最后一条 user 消息长这样：")
    print("    上一次的输出无法被解析，错误信息：缺少必填字段：order_id（实际字段：['订单号']）")
    print("    请重新只输出一个 JSON 对象，第一个字符必须是 {，不要任何其他文字。")
    print("  → 错误信息越具体，第 2 次成功率越高；只写「格式不对」，模型只能猜。")
    print("  → 重试有边界：最多 2-3 次，仍失败就返回 None + 记日志，别让异常打断整批任务。")


def chat_json(llm, messages, **kwargs):
    """
    带降级的 JSON mode 调用：服务商不支持 response_format 时自动退回纯 Prompt 约束。

    为什么需要它：JSON mode 只是「免费提升可靠性」，不是所有服务商/模型都支持，
    传了不支持的参数会直接 400。工程上必须留一条退路。
    """
    try:
        return llm.chat(messages, response_format={"type": "json_object"}, **kwargs)
    except RuntimeError as error:                 # 服务商不支持该参数
        print("  [warn] 该服务不支持 response_format，已退回纯 Prompt 约束：{}".format(str(error)[:80]))
        return llm.chat(messages, **kwargs)


def demo_three_schemes() -> None:
    section("第 4 步：三种结构化输出方案对比")
    llm = get_llm()
    response = chat_json(llm, [{"role": "user", "content": build_prompt(EMAIL)}], temperature=0)
    print("  刚才这次调用走的是「JSON mode + 降级」的封装（离线模式由 Mock 返回）：")
    print("    返回内容前 80 字：{}".format((response["content"] or "").replace("\n", " ")[:80]))
    print("    （离线时这段是 Mock 写的；在线时它是模型按六段式 prompt 抽出的真实 JSON）")

    print("\n  三种方案怎么选：")
    table = [
        ("维度", "纯 Prompt 约束", "JSON mode", "Function Calling"),
        ("靠什么保证", "提示词+示例", "API 层保证是合法 JSON", "用工具 schema 约束参数"),
        ("字段名/类型对吗", "不保证", "不保证（可能字段名全错）", "相对更稳，仍需校验"),
        ("能强制枚举值吗", "靠 prompt", "部分支持 JSON Schema 模式", "支持（写进 schema）"),
        ("服务商支持", "全部支持", "多数支持，版本不一", "主流支持"),
        ("适合什么", "快速原型、单字段", "要稳定产出 JSON 的首选", "要模型自己决定调哪个工具"),
    ]
    for row in table:
        print("  " + " ".join(pad(str(cell), 26) for cell in row))
    print("\n  三条结论：")
    print("    ① 能开 JSON mode 就开，它是免费的可靠性提升；")
    print("    ② 要「模型自己决定调什么工具」时才上 Function Calling（第 04 章），")
    print("       它解决的是「选哪个动作」，不只是「输出什么格式」；")
    print("    ③ 任何一层都不能省掉代码校验 —— prompt 层负责提高成功率，代码层负责不把错误放行。")


def main() -> int:
    load_env()
    print_banner("第 03 章 · Prompt 结构化与 JSON 校验")

    demo_bad_vs_good_prompt()
    demo_dirty_inputs()
    demo_retry()
    demo_three_schemes()

    section("在线模式怎么验证")
    if is_mock_mode():
        print("  配好 code/.env 后再跑一次本脚本（不带 AGENT_MOCK），就会看到真实的抽取结果。")
        print("  也可以做这个小实验：把同一段 prompt 跑 20 次，统计「JSON 合法率」和「字段正确率」，")
        print("  把自己实测的数字记进笔记 —— 它比任何教程里的百分比都可信。")
    else:
        print("  你正在在线模式：上面的 chat_json 调用已经真的发了出去。")
        print("  建议把同一段 prompt 跑 20 次，统计 JSON 合法率与字段正确率，记进笔记。")
    print("  另外可以试：把 amount 的校验换成自己的业务规则（比如 0 < amount < 100000）。")
    print("  业务规则要写在**调用方**，不要塞进 safe_parse_json —— 它只管格式与类型。")

    section("本章要点回顾")
    print("  1. 分界线：输出要能被代码解析。「人看得懂」对程序等于 0 分。")
    print("  2. 六段式 Prompt：角色 / 任务 / 约束 / 输入 / 输出格式 / 示例；示例里必须有边界情况。")
    print("  3. 输入放最后：注意力更靠后，而且静态前缀能命中服务商缓存。")
    print("  4. 分隔符包住数据：<email>...</email> 是最低成本的防注入。")
    print("  5. JSON mode 的边界：只保证「是合法 JSON」，不保证字段名和类型。")
    print("  6. 校验顺序：剥壳 → 括号截断 → 引号/逗号修复 → 字段与类型校验，一步都不能少。")
    print("  7. 重试要回灌错误，且有边界：2-3 次，失败降级为 None + 日志。")
    print("  8. 三方案取舍：能开 JSON mode 就开；要选工具才上 Function Calling；代码校验永远保留。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
