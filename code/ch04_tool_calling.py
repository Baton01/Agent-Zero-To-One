"""
第 04 章 · 工具调用 配套代码

本章学到什么：
  - 工具调用是「填表 → 代跑 → 回报」的循环：模型只填申请表，执行永远发生在你的代码里
  - description 写得好不好，直接决定模型会不会用、用得对不对
  - 健壮性的核心：异常不是给用户的，是给模型的 —— 把失败翻译成它能行动的一句话喂回去
  - 安全由代码强制，不由提示词建议：危险工具必须人工确认，且要展示完整参数

怎么跑：
    cd code
    AGENT_MOCK=1 python ch04_tool_calling.py     # 离线模式，不需要 API Key
    python ch04_tool_calling.py                  # 在线模式，需要 .env 里配好 Key
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

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
from common.tools import Tool, ToolRegistry  # noqa: E402
from common.trace import Trace  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

#: 所有文件操作都被限制在这个临时目录里。工具是模型的「手」，手必须有活动范围。
SANDBOX_DIR = os.path.join(tempfile.gettempdir(), "agent_zto_ch04_sandbox")

SYSTEM_PROMPT = (
    "你是一个会使用工具的助手。需要精确计算、当前时间、本地文件时，必须调用工具，"
    "不要凭记忆回答。拿到工具结果后，用中文把结论告诉用户。"
)


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def pad(text: str, width: int) -> str:
    """按「中文算两列」补齐宽度，让含中文的列表也能对齐。"""
    shown = sum(2 if "\u4e00" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def short(text, limit: int = 110) -> str:
    """打印工具结果时截断，避免刷屏；真实项目里这也是为了不让观察结果撑爆上下文。"""
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# ============================================================================
# 一、沙箱与工具：模型能做的事，全在这个清单里
# ============================================================================

def resolve_in_sandbox(path: str) -> str:
    """
    把相对路径解析到沙箱目录内。

    安全要点：用 realpath 归一化后再比对前缀，否则 ../../.env 这类路径穿越会直接读到
    沙箱外面的文件。**提示词负责减少错误，代码负责守住底线** —— 这里就是底线。
    """
    cleaned = str(path).strip().lstrip("./")
    if cleaned.startswith("sandbox/"):
        cleaned = cleaned[len("sandbox/"):]
    root = os.path.realpath(SANDBOX_DIR)
    candidate = os.path.realpath(os.path.join(root, cleaned))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise PermissionError(
            "路径越界：只允许访问 sandbox 目录下的文件（收到 {!r}）。请改用目录内的文件名。".format(path))
    return candidate


_ALLOWED_EXPR = re.compile(r"^[0-9+\-*/(). %]+$")


def calculator(expression: str) -> str:
    """白名单校验字符 + 空内置环境求值：绝不要 eval 裸字符串（那是模型给的内容）。"""
    expression = str(expression).strip()
    if not _ALLOWED_EXPR.match(expression):
        raise ValueError(
            "表达式含非法字符：{!r}。只能用数字和 + - * / ( ) . ** % 运算符，例如 1234*5678".format(expression))
    return str(eval(expression, {"__builtins__": {}}, {}))


def get_current_time(timezone_name: str = "local") -> str:
    """返回当前时间。真实项目里这就是 time.time()，不需要外部服务。"""
    if str(timezone_name).lower() in ("utc", "gmt"):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_file(path: str, max_chars: int = 500) -> str:
    """读沙箱内的文件，超长自动截断，并告诉模型「怎么拿到剩下的」。"""
    target = resolve_in_sandbox(path)
    if not os.path.isfile(target):
        existing = ", ".join(sorted(os.listdir(SANDBOX_DIR))) or "（目录为空）"
        raise FileNotFoundError(
            "文件 {!r} 不存在。沙箱目录下现有文件：{}。请从这些文件里选一个，或先用 write_file 创建".format(
                path, existing))
    with open(target, "r", encoding="utf-8") as handle:
        content = handle.read()
    limit = int(max_chars)
    if len(content) <= limit:
        return content
    return "{}\n\n[内容已截断：原文 {} 字符，只显示前 {} 字符。想看更多请把 max_chars 调大。]".format(
        content[:limit], len(content), limit)


def write_file(path: str, content: str) -> str:
    """写入沙箱内的文件（L1 可逆写：路径白名单 + 覆盖可控）。"""
    target = resolve_in_sandbox(path)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(str(content))
    return json.dumps({"ok": True, "path": os.path.basename(target), "chars": len(str(content))},
                      ensure_ascii=False)


def delete_file(path: str) -> str:
    """删除沙箱内的文件（L2 不可逆：做错了 5 秒内撤不回来，必须人工确认）。"""
    target = resolve_in_sandbox(path)
    if not os.path.isfile(target):
        raise FileNotFoundError("文件 {!r} 不存在，没有可删除的内容".format(path))
    os.remove(target)
    return json.dumps({"ok": True, "deleted": os.path.basename(target)}, ensure_ascii=False)


def build_registry() -> ToolRegistry:
    """
    先发放三个 L0 只读工具（不改状态，可以放心让模型随便调）。

    注意写操作工具（write_file / delete_file）**不在这里** —— 它们是 L1/L2，会改状态甚至不可逆，
    按权限分批发放：第 2/3 部分只用只读工具，第 4 部分才把写工具交给 Agent。
    真实项目里也该这么做：工具清单 = 这一步允许它做的事。
    """
    registry = ToolRegistry()
    registry.add(
        "calculator",
        "计算一个纯数学表达式并返回精确结果。当用户需要加减乘除、乘方等数值计算时使用。"
        "不要用它做单位换算、日期计算或字符串处理。",
        {"type": "object", "properties": {"expression": {
            "type": "string",
            "description": "数学表达式，只允许数字和 + - * / ( ) . ** % 运算符，例如 1234*5678"}},
         "required": ["expression"]},
        calculator,
    )
    registry.add(
        "get_current_time",
        "获取当前日期和时间。当用户问「现在几点」「今天几号」，或需要以当前时间为基准计算时使用。"
        "它不是计时器，不能做倒计时。返回 YYYY-MM-DD HH:MM:SS 格式字符串。",
        {"type": "object", "properties": {"timezone_name": {
            "type": "string", "description": "时区，local 或 UTC，默认 local", "enum": ["local", "UTC"]}},
         "required": []},
        get_current_time,
    )
    registry.add(
        "read_file",
        "读取沙箱目录下某个文件的内容。当用户想「看看文件里写了什么」时使用。"
        "它返回的是**文件内容**，需要文件名列表请直接说明，不要用本工具。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件名，相对沙箱目录，例如 notes.md"},
            "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 500"}},
         "required": ["path"]},
        read_file,
    )
    return registry


def register_write_tools(registry: ToolRegistry) -> None:
    """第 4 部分才发放的写工具：L1 写文件（可逆）与 L2 删文件（不可逆，必须人工确认）。"""
    registry.add(
        "write_file",
        "把内容写入沙箱目录下的文件（覆盖同名文件）。当用户明确要求「保存/记录下来」时使用。"
        "只写不改：要修改已有内容，先 read_file 看清楚再整体重写。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件名，相对沙箱目录，例如 result.txt"},
            "content": {"type": "string", "description": "要写入的完整文本内容"}},
         "required": ["path", "content"]},
        write_file,
    )
    registry.register(Tool(
        name="delete_file",
        description="删除沙箱目录下的文件，不可恢复。仅当用户明确要求删除某个文件时使用。",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "要删除的文件名，相对沙箱目录"}},
            "required": ["path"]},
        func=delete_file,
        requires_confirmation=True,      # 声明它是危险工具；拦截逻辑在循环里
    ))


def prepare_sandbox() -> None:
    """准备演示用的沙箱目录与文件。每次运行都重建，保证结果可复现。"""
    if os.path.isdir(SANDBOX_DIR):
        shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    with open(os.path.join(SANDBOX_DIR, "notes.md"), "w", encoding="utf-8") as handle:
        handle.write("# ReAct 学习笔记\n\nReAct = Reasoning + Acting。\n每步先想再做的关键，是让思考留在上下文里。\n")


# ============================================================================
# 二、工具循环：本章的核心骨架（所有 Agent 框架的内核都是它）
# ============================================================================

def assistant_message(response: dict) -> dict:
    """
    把统一格式的响应转成 API 原生的 assistant 消息。

    两个不能省的细节：① 这条消息必须原样加回历史，否则模型看不到自己申请过什么；
    ② 消息历史里的 arguments 必须是 JSON 字符串（协议要求），不是对象。
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


def coerce_value(value, declared: str):
    if declared == "integer":
        return int(value)
    if declared == "number":
        return float(value)
    if declared == "boolean" and isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是")
    if declared == "string":
        return value if isinstance(value, str) else str(value)
    return value


def validate_args(tool: Tool, args) -> tuple:
    """
    执行前按 schema 校验参数。返回 (修正后的参数, 错误文本)。

    为什么要在执行前做：让错误**可读且可行动**（缺什么、期望什么类型、示例是什么），
    模型下一轮才有机会改对。只回一句「参数错误」，它会接着错。
    """
    if not isinstance(args, dict):
        return args, "[工具错误:BadArguments] 参数必须是 JSON 对象，实际收到 {}。".format(type(args).__name__)

    properties = tool.parameters.get("properties") or {}
    missing = [key for key in (tool.parameters.get("required") or []) if key not in args]
    if missing:
        lines = ["[工具错误:BadArguments] 参数缺失：{}。".format("、".join(missing))]
        for key in missing:
            info = properties.get(key) or {}
            lines.append("  - {}（{}）：{}".format(key, info.get("type", "string"), info.get("description", "")))
        lines.append("请补充参数后重新调用。")
        return args, "\n".join(lines)

    fixed = dict(args)
    for key, value in args.items():
        declared = (properties.get(key) or {}).get("type")
        if not declared or declared in ("array", "object"):
            continue
        if declared == "string" and isinstance(value, str):
            continue
        if declared == "integer" and isinstance(value, int) and not isinstance(value, bool):
            continue
        if declared == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            continue
        if declared == "boolean" and isinstance(value, bool):
            continue
        try:
            fixed[key] = coerce_value(value, declared)
        except (TypeError, ValueError):
            return args, ("[工具错误:BadArguments] 参数 {} 期望 {}，实际收到 {!r}（{}）。请改正类型后重试。"
                          .format(key, declared, value, type(value).__name__))
    return fixed, None


def run_tool_loop(question: str, registry: ToolRegistry, trace: Trace, llm,
                  tools: list = None, max_steps: int = 5, label: str = "") -> str:
    """工具调用的完整循环。三个分支：模型不要工具了 / 要继续用工具 / 步数用完。"""
    messages = [{"role": "user", "content": question}]
    schemas = tools if tools is not None else registry.to_openai_schema()

    for step in range(1, max_steps + 1):
        response = llm.chat(messages, tools=schemas)
        trace.llm_call(step, response, note=label)
        tool_calls = response["tool_calls"]

        # 分支 A：模型不再申请工具 → 它给了最终答案，循环结束
        if not tool_calls:
            print("  [第 {} 步] 模型不再需要工具，给出最终答案".format(step))
            print("  [回答] {}".format(short(response["content"], 160)))
            trace.log("finish", step=step, reason="final_answer")
            return response["content"]

        print("  [第 {} 步] 模型决定调用工具".format(step))
        if response["content"]:
            print("  [思考] {}".format(short(response["content"], 90)))
        messages.append(assistant_message(response))

        # 分支 B：逐个执行并回填。无论成功失败，每个 call 都必须有一条 tool 消息回复
        for call in tool_calls:
            name, raw_args = call["name"], call["arguments"]
            tool = registry.get(name)
            if tool is None:
                args, error = raw_args, None            # 交给 registry 产出 NotFound
            else:
                args, error = validate_args(tool, raw_args)
            started = time.time()
            result = error if error else registry.execute(name, args)
            elapsed = time.time() - started
            trace.tool_call(step, name, args, result, elapsed)
            print("  [act ] {}({})".format(name, json.dumps(args, ensure_ascii=False)))
            print("  [obs ] {}".format(short(result)))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})

    # 分支 C：兜底。返回一句可用的兜底结果，而不是抛异常
    print("  [停止] 达到最大步数 {}，任务未完成".format(max_steps))
    trace.log("stop", reason="max_steps", max_steps=max_steps)
    return "已达到最大步数 {}，任务未完成，转人工处理。".format(max_steps)


def call(name: str, arguments: dict) -> dict:
    """构造一条预设的工具调用（离线演示用）。"""
    return {"id": "mock_{}_{}".format(name, json.dumps(arguments, ensure_ascii=False)[:12]),
            "name": name, "arguments": arguments}


def tool_response(thought: str, *calls: dict) -> dict:
    """构造一条预设的模型响应：content 放思考，tool_calls 放它这一轮要申请的工具。"""
    result = {"content": thought, "tool_calls": list(calls)}
    if not calls:
        result["tool_calls"] = []
    return result


def run_scenario(title: str, question: str, script: list, registry: ToolRegistry,
                 trace: Trace, llm, expect: str = "", label: str = "") -> None:
    """离线演示：用 set_mock_script 预设模型的每一步决策，把某条分支稳定地跑出来。"""
    section(title)
    print("  用户：{}".format(question))
    if is_mock_mode():
        set_mock_script(script)
        print("  [离线] 模型的每一步决策由 set_mock_script 预设；在线模式下它自己决定。")
    start = len(trace.entries)
    run_tool_loop(question, registry, trace, llm, max_steps=5, label=label)
    if expect:
        observed = [str(e.get("result", "")) for e in trace.entries[start:] if e["event"] == "tool_call"]
        hit = any(expect in item for item in observed)
        print("  自检：期望看到 {} → {}".format(expect, "已出现" if hit else "未出现（请看上面的轨迹）"))


# ============================================================================
# 三、五个部分
# ============================================================================

def part1_one_tool(trace: Trace, llm) -> None:
    section("第 1 部分：只有 1 个工具时的最简循环")
    minimal = ToolRegistry()
    minimal.add("calculator", "计算一个纯数学表达式并返回精确结果。",
                {"type": "object", "properties": {"expression": {"type": "string"}},
                 "required": ["expression"]}, calculator)
    print("  注册的工具：{}（这个注册表只服务本部分）".format(minimal.names()))
    run_tool_loop("帮我算一下 1234*5678 等于多少", minimal, trace, llm, label="part1")
    print("  先跑通结构，再谈工具数量：循环体不变，变的只是「模型能看见几个工具」。")


def part2_tool_selection(trace: Trace, llm, registry: ToolRegistry) -> None:
    section("第 2 部分：多工具时的选择 —— 模型选了哪个，为什么")
    print("  模型能看到的工具清单：")
    print(registry.describe())
    print("\n  选择依据（离线是 mock 的规则匹配，在线是模型自己判断）：")
    print("    · 离线规则：工具名/描述里的关键词是否出现在问题里，再看「参数能不能从问题里填出来」；")
    print("    · 在线模型：它在所有工具描述上做语义比较，所以 description 写得越清楚越准。")

    for question in [
        "帮我算一下 1234*5678 等于多少",
        "现在几点了",
        "帮我读一下 notes.md 里写了什么",
    ]:
        print("\n  ── 问题：{}".format(question))
        start = len(trace.entries)
        run_tool_loop(question, registry, trace, llm, max_steps=3, label="part2")
        picked = [e["tool"] for e in trace.entries[start:] if e["event"] == "tool_call"]
        print("  [结果] 本次调用了：{}".format("、".join(picked) if picked else "（没有调用工具）"))
    print("\n  工具越少，选择越准；两个工具语义重叠（read_file / list_files）时，")
    print("  在 description 里写清「不用于什么」并互相指路，是提升准确率最省力的一招。")


def part3_tool_errors(trace: Trace, llm, registry: ToolRegistry) -> None:
    section("第 3 部分：工具出错的四种类型（本章重点）")
    scenarios = [
        ("场景 A：参数缺失 → BadArguments",
         "帮我读一下 note 文件",
         "模型先漏掉了必填参数，校验层把「缺哪个、什么类型、示例」喂回去，它补上后重调。",
         [tool_response("我先试着直接读。", call("read_file", {})),
          tool_response("少填了 path，补上再调一次。", call("read_file", {"path": "notes.md"})),
          tool_response("文件里写的是 ReAct 学习笔记。")],
         "[工具错误:BadArguments] 参数缺失"),

        ("场景 B：参数类型错误 → BadArguments",
         "帮我读 notes.md，只看开头一点",
         "模型把 max_chars 填成了文字。校验层不直接执行，而是要求它改成数字 —— 这才是可行动的报错。",
         [tool_response("先随便填个值试试。",
                        call("read_file", {"path": "notes.md", "max_chars": "一点点"})),
          tool_response("类型错了，改成数字 50。",
                        call("read_file", {"path": "notes.md", "max_chars": 50})),
          tool_response("已经读到文件开头部分。")],
         "[工具错误:BadArguments] 参数 max_chars 期望 integer"),

        ("场景 C：工具内部抛异常 → FileNotFoundError",
         "帮我读一下 report.md",
         "工具真的失败了。注意错误信息里带着「目录下现有文件」，模型据此换了个路径。",
         [tool_response("按用户给的路径读。", call("read_file", {"path": "report.md"})),
          tool_response("这个文件不存在，改用目录里实际有的 notes.md。",
                        call("read_file", {"path": "notes.md"})),
          tool_response("report.md 不存在，我改读 notes.md 了。")],
         "[工具错误:FileNotFoundError]"),

        ("场景 D：工具不存在 → NotFound",
         "帮我查一下北京现在的气温",
         "模型叫了一个不存在的工具名。registry 把「可用工具清单」喂回去，它自己改名重调。",
         [tool_response("先查气温。", call("get_temperature", {"city": "北京"})),
          tool_response("没有这个工具，改用 get_current_time 回答时间部分。",
                        call("get_current_time", {"timezone_name": "local"})),
          tool_response("气温我查不到（没有这个工具），但当前时间是有的。")],
         "[工具错误:NotFound]"),
    ]

    for title, question, note, script, expect in scenarios:
        print("")
        run_scenario(title, question, script, registry, trace, llm, expect=expect, label="part3")
        print("  [讲解] {}".format(note))

    print("\n  四条规则：① 异常不许冒泡到用户；② 错误信息要写清「是什么错 + 可用替代 + 下一步做什么」；")
    print("  ③ 每个 tool_call 都必须有一条 tool 消息回复（拒绝、失败、超时也算回复）；")
    print("  ④ 空 except 是 Agent 代码里最危险的一行 —— 模型会基于「空结果」继续推理。")


def part4_confirmation(trace: Trace, llm, registry: ToolRegistry) -> None:
    section("第 4 部分：危险工具的人工确认（安全由代码强制）")
    register_write_tools(registry)
    print("  本部分新发放的工具（L1/L2 写操作，会改状态甚至不可逆）：{}".format(
        [name for name in registry.names() if name in ("write_file", "delete_file")]))
    print("  素材准备：直接调 write_file 造一个待删除的文件（工具就是普通函数，也可以不经模型直接调）")
    print("  {}".format(registry.execute("write_file", {"path": "old_draft.md", "content": "这是一份过期草稿。"})))

    question = "帮我把 old_draft.md 删掉"
    delete_script = [tool_response("用户要求删除文件，我来申请这个操作。",
                                   call("delete_file", {"path": "old_draft.md"})),
                     tool_response("该操作需要你的授权，我没有删除，改为向你说明。")]

    # 场景 1：非交互环境下的默认策略 —— 拒绝。这就是 fail-closed：不能确认就不执行
    def deny_confirmer(tool, arguments):
        print("  [确认回调] 收到危险操作申请：{}({})".format(tool.name, json.dumps(arguments, ensure_ascii=False)))
        print("  [确认回调] 非交互环境（CI / 定时任务 / Mock），默认拒绝。")
        return False

    registry.set_confirmer(deny_confirmer)
    run_scenario("拒绝之后 Agent 的行为", question, delete_script, registry, trace, llm,
                 expect="[工具错误:Rejected]", label="part4-deny")
    print("  两个设计点：① 被拒绝也是一次回复 —— 必须回一条 tool 消息，否则下一次请求直接 400；")
    print("  ② 拒绝文案里写了「不要重试」，否则模型会换个参数继续申请删除，进入无意义循环。")

    # 场景 2：允许执行，看看工具真的生效
    print("\n  ── 场景 2：确认回调返回 True（真实交互里，这一步是用户按下确认）")
    registry.set_confirmer(lambda tool, arguments: True)
    print("  {}".format(registry.execute("delete_file", {"path": "old_draft.md"})))
    print("  old_draft.md 还在吗？{}".format(
        "在" if os.path.isfile(os.path.join(SANDBOX_DIR, "old_draft.md")) else "已被删除"))
    print("\n  三个交互设计要点：① 确认界面必须展示**完整参数** —— 提示注入是真实威胁，")
    print("  用户要看到参数全文才能判断；② 拒绝后明确「不要重试」；")
    print("  ③ 路径白名单、额度限制、操作日志这三道护栏同样要写在代码里。")


def part5_stats(trace: Trace, llm, registry: ToolRegistry) -> None:
    section("第 5 部分：工具调用统计与 Trace 摘要")
    print(registry.stats())
    print("\n  失败次数大多是上面故意造出来的 —— 这正是本章想让你看到的：")
    print("  工具失败不是事故，而是 Agent 的日常；关键在于是被正确处理，还是被静默吞掉。")
    section("Trace 摘要（本次运行的完整轨迹）")
    print(trace.summary())
    print("")
    print(llm.usage_summary())


def main() -> int:
    load_env()
    print_banner("第 04 章 · 工具调用（Function Calling）")
    prepare_sandbox()
    print("沙箱目录：{}".format(SANDBOX_DIR))

    trace = Trace("ch04_tool_calling")
    llm = get_llm(temperature=0.2, system=SYSTEM_PROMPT)
    registry = build_registry()

    part1_one_tool(trace, llm)
    part2_tool_selection(trace, llm, registry)
    part3_tool_errors(trace, llm, registry)
    part4_confirmation(trace, llm, registry)
    part5_stats(trace, llm, registry)

    section("本章要点回顾")
    print("  1. 模型只会说，不会做：没有时钟、不会精确计算、读不了文件 —— 这是架构决定的。")
    print("  2. 工具调用 = 填表 → 代跑 → 回报。模型从不执行你的代码，执行永远在你的进程里。")
    print("  3. description 写清「做什么 / 何时用 / 何时不用 / 返回什么」，比换模型有用得多。")
    print("  4. 异常不是给用户的，是给模型的：翻译成一句能行动的话喂回去，Agent 就有了自纠能力。")
    print("  5. 返回值用 JSON 字符串 + {ok, data, error} 统一契约：模型好读、程序好测。")
    print("  6. 安全由代码强制：requires_confirmation + 完整参数展示 + 路径白名单。")
    print("  7. 循环必须有 max_steps：它是唯一与任务形态无关的兜底。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
