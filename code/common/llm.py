"""
统一的大模型调用封装（本项目所有章节共用）。

设计目标（很重要，决定了为什么长这样）：
  1. 零第三方依赖 —— 只用标准库 urllib，装了 openai SDK 也走同一条路，减少"装不上包"的挫败感
  2. 双模式 —— 有 Key 就真实调用；没有 Key 或 AGENT_MOCK=1 就用内置规则模拟。
     离线模式不是"假的"，它真的走完你的循环、真的解析 tool_call、真的执行你的工具函数，
     只是"想"的部分由规则替代。用来调试代码逻辑和验证自己的理解完全够用。
  3. 多服务商通用 —— 只要兼容 OpenAI 接口规范，改 3 个环境变量就能换服务商

统一响应格式（所有章节代码都按这个格式取数据）：
    {
        "content":    str,      # 文本内容，可能为空字符串
        "tool_calls": [         # 模型请求调用的工具，没有则为空列表
            {"id": str, "name": str, "arguments": dict}
        ],
        "usage": {              # token 用量，用于成本统计
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_tokens": int,
        },
        "mock": bool,           # 本次响应是否来自离线模拟
    }

用法：
    from common.llm import load_env, get_llm, chat, is_mock_mode

    load_env()
    print(chat("用一句话解释什么是 Agent"))
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

# ============================================================================
# 一、环境变量加载（手写 .env 解析，不依赖 python-dotenv）
# ============================================================================

#: .env 文件里支持的全部键，及其中文说明（用于报错提示）
ENV_KEYS = {
    "OPENAI_API_KEY": "API 密钥",
    "OPENAI_BASE_URL": "服务商接口地址（务必以 /v1 结尾，各服务商不同）",
    "OPENAI_MODEL": "模型名（必须与服务商匹配）",
    "AGENT_MOCK": "设为 1 时强制离线 Mock 模式",
    "AGENT_TEMPERATURE": "默认温度",
    "AGENT_MAX_TOKENS": "默认最大输出 token 数",
    "AGENT_TIMEOUT": "单次请求超时秒数",
    "AGENT_BUDGET_CNY": "单次任务成本上限（人民币元）",
}

_ENV_LOADED = False


def load_env(path: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """
    读取 .env 文件并写入环境变量。

    查找顺序（找到第一个就用）：
        1. 显式传入的 path
        2. 当前工作目录下的 .env
        3. 本文件上级目录（也就是 code/）下的 .env

    已存在的环境变量默认**不覆盖** —— 这样你可以在命令行临时
    `set OPENAI_MODEL=xxx` 做实验，而不必改文件。

    返回实际读到的键值对。
    """
    global _ENV_LOADED

    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(os.getcwd(), ".env"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    found: Dict[str, str] = {}
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("export "):
                        line = line[7:].strip()
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    # 去掉成对的引号
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    if not key:
                        continue
                    found[key] = value
                    if override or key not in os.environ or os.environ.get(key) == "":
                        os.environ[key] = value
        except OSError as exc:
            print("[警告] 读取 {} 失败：{}".format(candidate, exc))
            continue
        _ENV_LOADED = True
        break

    _ENV_LOADED = True
    return found


def _get_config() -> Dict[str, str]:
    """收集当前生效的配置。未调用过 load_env() 时先自动加载一次。"""
    if not _ENV_LOADED:
        load_env()
    return {
        "api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
        "base_url": (os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini",
        "mock": os.environ.get("AGENT_MOCK", "").strip(),
        "timeout": float(os.environ.get("AGENT_TIMEOUT", "60") or 60),
    }


def is_mock_mode() -> bool:
    """
    当前是否为离线 Mock 模式。

    判定顺序：
        1. AGENT_MOCK == "1"  → 强制离线（哪怕配了 Key）
        2. 没有 OPENAI_API_KEY → 自动离线
        3. 否则 → 在线
    """
    config = _get_config()
    if config["mock"] == "1":
        return True
    return not config["api_key"]


def describe_mode() -> str:
    """返回一行人类可读的当前模式说明，用于脚本开头的横幅。"""
    config = _get_config()
    if is_mock_mode():
        reason = "AGENT_MOCK=1" if config["mock"] == "1" else "未检测到 OPENAI_API_KEY"
        return "[离线 Mock 模式]（原因：{}）—— 不联网，模型响应由内置规则模拟".format(reason)
    return "[在线模式] {} @ {}".format(config["model"], config["base_url"])


def print_banner(title: str) -> None:
    """脚本开头统一打印的分隔线横幅。"""
    line = "=" * 64
    print(line)
    print("  {}".format(title))
    print("  {}".format(describe_mode()))
    print(line)


# ============================================================================
# 二、Token 估算与成本计算
# ============================================================================

_CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def count_tokens_approx(text: str) -> int:
    """
    粗略估算一段文本的 token 数（不调用任何接口，纯规则）。

    经验规则（与主流 tokenizer 的平均偏差约 ±30%）：
        中文/全角字符：约 1 字符 = 1 token
        英文/数字：   约 4 字符 = 1 token（这里取 3.5，偏保守）
        代码/符号：   约 3 字符 = 1 token

    ⚠️ 这是估算值，用于学习和成本量级判断，**不能用于对账**。
    精确值请读响应里的 usage 字段。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 1.0 + other / 3.5) + 1


def count_message_tokens(messages: List[Dict[str, Any]]) -> int:
    """估算一整组 messages 的 token 数（含每条消息的固定开销）。"""
    total = 0
    for message in messages:
        total += 4  # 每条消息的格式开销
        content = message.get("content")
        if isinstance(content, str):
            total += count_tokens_approx(content)
        # 工具调用也要计入
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            total += count_tokens_approx(function.get("name", ""))
            total += count_tokens_approx(function.get("arguments", ""))
    return total + 3


#: 常见模型的每 100 万 token 价格（人民币）。价格会变，这里是教学用估值。
#: 格式：模型名 -> (输入价, 输出价)
PRICE_TABLE_CNY = {
    "deepseek-chat": (0.5, 8.0),
    "deepseek-reasoner": (1.0, 16.0),
    "qwen-plus": (0.8, 2.0),
    "qwen-turbo": (0.3, 0.6),
    "glm-4-flash": (0.0, 0.0),
    "glm-4-plus": (5.0, 5.0),
    "moonshot-v1-8k": (12.0, 12.0),
    "gpt-4o-mini": (1.1, 4.3),
    "gpt-4o": (18.0, 72.0),
    "_default": (1.0, 4.0),
}


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: Optional[str] = None) -> float:
    """
    估算一次调用的成本，单位：人民币元。

    输入输出分开计价（输出通常贵几倍）。模型名不在价格表里时用保守的默认价。
    价格会变，这里只用于建立"成本量级"的直觉。
    """
    name = (model or _get_config()["model"]).strip()
    price = PRICE_TABLE_CNY.get(name)
    if price is None:
        # 模糊匹配：deepseek-chat-xxx 也能匹配到 deepseek-chat
        for known, value in PRICE_TABLE_CNY.items():
            if known != "_default" and known in name:
                price = value
                break
    if price is None:
        price = PRICE_TABLE_CNY["_default"]
    input_price, output_price = price
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000.0


def format_cost(amount_cny: float) -> str:
    """把金额格式化成人类友好的字符串。"""
    if amount_cny < 0.0001:
        return "¥{:.6f}".format(amount_cny)
    if amount_cny < 1:
        return "¥{:.4f}".format(amount_cny)
    return "¥{:.2f}".format(amount_cny)


# ============================================================================
# 三、离线 Mock 引擎
# ============================================================================
#
# 它不是"随便返回一句话"，而是按三个规则模拟一个讲道理的模型：
#   1. 有还没用过的、和用户问题相关的工具 → 调用它
#   2. 已经有工具结果，且没有更多相关工具 → 基于工具结果给最终答案
#   3. 没有工具 → 返回一段结构化的文本
#
# 这样 Agent 循环、工具解析、错误处理这些"代码逻辑"全都能被真实执行到。

#: 可以从用户问题里抽出来的"路径样"字符串
_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MATH_RE = re.compile(r"[\d\.\s\+\-\*/\(\)%]{3,}")


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _used_tool_names(messages: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            if function.get("name"):
                names.append(function["name"])
    return names


def _keywords_of(tool: Dict[str, Any]) -> List[str]:
    """从工具名和描述里抽出可用来匹配的关键词。"""
    function = tool.get("function", {})
    keywords: List[str] = []

    # 英文名按 _ - 空格 拆开（get_weather → get / weather）
    for chunk in re.split(r"[_\-\s]+", function.get("name", "").lower()):
        if len(chunk) >= 3:
            keywords.append(chunk)
    # 工具名里的中文片段
    keywords.extend(re.findall(r"[\u4e00-\u9fff]{2,}", function.get("name", "")))

    description = function.get("description", "")
    # 中文：既要整词，也要所有 2 字滑窗（"计算数学表达式" → 计算/算数/数学/学表/表达/达式）
    keywords.extend(re.findall(r"[\u4e00-\u9fff]{2,4}", description))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", description)
    for run in chinese_runs:
        for index in range(len(run) - 1):
            keywords.append(run[index:index + 2])
    # 英文
    keywords.extend(re.findall(r"[a-z]{4,}", description.lower()))

    # 去重并去掉太通用的词，避免"什么都匹配"
    stopwords = {"一个", "可以", "用于", "返回", "参数", "工具", "使用", "根据", "进行", "这个", "需要"}
    seen = []
    for word in keywords:
        if word in stopwords or word in seen:
            continue
        seen.append(word)
    return seen


def _arg_affinity(tool: Dict[str, Any], user_text: str) -> int:
    """
    按"这个工具的参数能不能从问题里填得出来"打分。

    这是比关键词更强的信号：如果工具要一个数学表达式、而问题里正好有算式，
    那它几乎肯定就是该调用的工具。
    """
    properties = (tool.get("function", {}).get("parameters", {}) or {}).get("properties", {}) or {}
    score = 0

    for key in properties:
        lowered = key.lower()

        if any(hint in lowered for hint in ("expr", "expression", "formula", "算式", "表达式", "计算")):
            match = _MATH_RE.search(user_text)
            if match and any(op in match.group(0) for op in "+-*/%"):
                score += 5

        if any(hint in lowered for hint in ("path", "file", "filename", "路径", "文件")):
            if _PATH_RE.search(user_text):
                score += 5

        if any(hint in lowered for hint in ("city", "location", "城市", "地点", "位置", "地区")):
            if _KNOWN_CITIES.search(user_text):
                score += 5

        if any(hint in lowered for hint in ("code", "diff", "snippet", "patch", "代码")):
            if "```" in user_text or re.search(r"\b(def |class |function |import )", user_text):
                score += 3

        # 通用文本类参数几乎总能填上，给一个弱信号
        if any(hint in lowered for hint in ("text", "content", "query", "question", "topic", "文本", "内容", "问题", "主题")):
            score += 1

    return score


#: 常见城市名，用于判断"问题里提到了地点"
_KNOWN_CITIES = re.compile(
    "北京|上海|广州|深圳|杭州|成都|西安|武汉|南京|重庆|天津|苏州|长沙|青岛|厦门|"
    "Tokyo|Beijing|Shanghai|London|Paris|New York|Singapore"
)


def _tool_score(tool: Dict[str, Any], user_text: str) -> int:
    """
    判断一个工具是否和用户问题相关：关键词命中 + 参数可填性。
    分数 > 0 就认为"相关"。
    """
    haystack = user_text.lower()
    score = _arg_affinity(tool, user_text)
    for word in _keywords_of(tool):
        if word in haystack:
            score += 2 if len(word) >= 3 else 1
    return score


def _pick_tool(tools: List[Dict[str, Any]], user_text: str) -> Optional[Dict[str, Any]]:
    """选一个得分最高的工具；都没命中时返回 None（由调用方决定要不要兜底）。"""
    if not tools:
        return None

    scored = [(index, _tool_score(tool, user_text), tool) for index, tool in enumerate(tools)]
    scored.sort(key=lambda item: (-item[1], item[0]))

    _, best_score, best_tool = scored[0]
    if best_score > 0:
        return best_tool
    return None


def _fill_arguments(tool: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    """
    按 JSON Schema 填参数。规则不完美，但足以让离线 demo 跑出可信的流程。
    """
    parameters = tool.get("function", {}).get("parameters", {}) or {}
    properties = parameters.get("properties", {}) or {}
    required = parameters.get("required") or list(properties.keys())
    arguments: Dict[str, Any] = {}

    for key in required:
        spec = properties.get(key, {}) or {}
        json_type = spec.get("type", "string")

        if spec.get("enum"):
            arguments[key] = spec["enum"][0]
            continue

        if json_type in ("integer", "number"):
            numbers = _NUMBER_RE.findall(user_text)
            if numbers:
                arguments[key] = int(float(numbers[0])) if json_type == "integer" else float(numbers[0])
            else:
                arguments[key] = 1 if json_type == "integer" else 1.0
            continue

        if json_type == "boolean":
            arguments[key] = True
            continue

        if json_type == "array":
            path_match = _PATH_RE.search(user_text)
            arguments[key] = [path_match.group(0)] if path_match else []
            continue

        if json_type == "object":
            arguments[key] = {}
            continue

        # 字符串类型：按参数名猜该填什么
        arguments[key] = _guess_string(key, user_text)

    return arguments


def _guess_string(key: str, user_text: str) -> str:
    lowered = key.lower()

    # 数学表达式：直接从问题里抠出算式
    if any(hint in lowered for hint in ("expr", "expression", "formula", "算式", "表达式")):
        match = _MATH_RE.search(user_text)
        if match:
            candidate = match.group(0).strip()
            if any(op in candidate for op in "+-*/%"):
                return candidate
        numbers = _NUMBER_RE.findall(user_text)
        if len(numbers) >= 2:
            return "{} + {}".format(numbers[0], numbers[1])

    # 文件路径
    if any(hint in lowered for hint in ("path", "file", "filename", "路径", "文件")):
        match = _PATH_RE.search(user_text)
        if match:
            return match.group(0)
        return "README.md"

    # 代码内容
    if any(hint in lowered for hint in ("code", "diff", "snippet", "patch")):
        return user_text

    # 城市 / 地点
    for city in ("北京", "上海", "广州", "深圳", "杭州", "成都", "西安", "武汉", "Tokyo", "Beijing", "Shanghai"):
        if city in user_text:
            return city

    # 查询 / 问题类：直接用用户原话，去掉开头的客套
    cleaned = re.sub(r"^(请|帮我|麻烦|我想|我要|你能|能帮我)+", "", user_text).strip()
    cleaned = cleaned.split("\n")[0].strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned or user_text[:200] or "default"


def _mock_text_answer(user_text: str) -> str:
    preview = user_text.strip().split("\n")[0][:80]
    return (
        "（离线 Mock 模式）我收到了你的问题：「{}」。\n"
        "这里本该是一段真实的模型回答。要看到真回答，请在 code/.env 里配好 "
        "OPENAI_API_KEY，或者去掉 AGENT_MOCK=1 重新运行。\n"
        "不过请放心 —— 这一次调用的**代码路径是真实执行过的**：消息组装、请求发送、"
        "响应解析、用量统计，全部走了一遍。".format(preview)
    )


def _mock_respond(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """离线模式的核心：按规则决定"这一步模型想干什么"。"""
    user_text = _last_user_text(messages)
    used = set(_used_tool_names(messages))
    tool_results = [m for m in messages if m.get("role") == "tool"]

    if tools:
        candidates = [t for t in tools if t.get("function", {}).get("name") not in used]
        picked = _pick_tool(candidates, user_text) if candidates else None

        # 兜底：只有一个候选工具、且问题不是一句寒暄时，先调它 —— 否则"零命中"的
        # 问题会一步就结束，你就看不到循环是怎么转的了。
        # 工具多于一个时不做兜底：宁可直答，也不乱调工具（这也是真实系统的行为）。
        if picked is None and not used and not tool_results:
            if len(candidates) == 1 and len(user_text.strip()) >= 6:
                picked = candidates[0]

        if picked is not None:
            name = picked["function"]["name"]
            arguments = _fill_arguments(picked, user_text)
            return {
                "content": "我需要调用工具 {} 来获取信息。".format(name),
                "tool_calls": [
                    {"id": "mock_call_{}".format(len(used) + 1), "name": name, "arguments": arguments}
                ],
            }

        if tool_results:
            observations = "\n".join(
                str(m.get("content", ""))[:600] for m in tool_results
            )
            return {
                "content": (
                    "（离线 Mock 模式）根据工具返回的结果：\n{}\n\n"
                    "综合以上信息，这就是我能给出的答案。任务完成。".format(observations)
                ),
                "tool_calls": [],
            }

    return {"content": _mock_text_answer(user_text), "tool_calls": []}


class _MockProvider:
    """离线模式的状态容器。支持用 set_script() 预设一串响应做确定性测试。"""

    def __init__(self) -> None:
        self.script: List[Dict[str, Any]] = []
        self.index = 0
        self.calls = 0

    def set_script(self, responses: List[Dict[str, Any]]) -> None:
        """预设响应序列。每调用一次 chat 消耗一条，用完后回落到启发式。"""
        self.script = list(responses)
        self.index = 0

    def reset(self) -> None:
        self.script = []
        self.index = 0
        self.calls = 0

    def next_scripted(self) -> Optional[Dict[str, Any]]:
        if self.index < len(self.script):
            item = self.script[self.index]
            self.index += 1
            return item
        return None


_MOCK = _MockProvider()


def set_mock_script(responses: List[Dict[str, Any]]) -> None:
    """
    预设离线模式的响应序列，用于写确定性的测试。

    用法：
        from common.llm import set_mock_script, reset_mock
        set_mock_script([
            {"content": "", "tool_calls": [{"id": "1", "name": "calc", "arguments": {"expr": "1+1"}}]},
            {"content": "答案是 2", "tool_calls": []},
        ])
        ... 跑你的 Agent ...
        reset_mock()
    """
    _MOCK.set_script(responses)


def reset_mock() -> None:
    """清空预设脚本和计数器。"""
    _MOCK.reset()


def get_mock_call_count() -> int:
    """离线模式下已经"假装"调用了几次。"""
    return _MOCK.calls


# ============================================================================
# 四、HTTP 请求（在线模式）
# ============================================================================

def _raise_readable_http_error(exc: urllib.error.HTTPError, url: str) -> None:
    """把 HTTP 错误翻译成"能指导行动"的中文提示（对应第 00 章的排错小节）。"""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 读 body 失败不该掩盖原始错误
        body = ""

    status = exc.code
    hints = {
        400: "请求格式有问题。常见原因：messages 结构不对、参数名写错、模型不支持某个参数。",
        401: "API Key 无效或没读到。检查 .env 里 OPENAI_API_KEY 是否填了、是否有多余空格引号、"
             "是否把 .env 放在了 code/ 目录下。",
        403: "权限不足或地区限制。检查账号是否开通了该模型、是否需要实名认证。",
        404: "接口地址或模型名不对。最常见的是 OPENAI_BASE_URL 少了 /v1 结尾，"
             "或 OPENAI_MODEL 写成了别家服务商的模型名。",
        429: "被限流或余额不足。先看返回体里的错误码：insufficient_quota 是没钱了，"
             "rate_limit_exceeded 是请求太快。",
        500: "服务商内部错误。等几秒重试通常就好。",
        503: "服务暂时不可用。稍后重试。",
    }
    hint = hints.get(status, "未收录的状态码，请把返回体贴出来查。")

    print("\n" + "!" * 64, file=sys.stderr)
    print("[请求失败] HTTP {}  ←  {}".format(status, url), file=sys.stderr)
    print("原因提示：{}".format(hint), file=sys.stderr)
    if body:
        print("服务端返回：{}".format(body[:800]), file=sys.stderr)
    print("!" * 64 + "\n", file=sys.stderr)


def _post_json(url: str, payload: Dict[str, Any], api_key: str, timeout: float) -> Any:
    """发一个 JSON POST，返回解析后的响应。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(api_key),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _raise_readable_http_error(exc, url)
        raise
    except urllib.error.URLError as exc:
        print("\n" + "!" * 64, file=sys.stderr)
        print("[网络错误] 连不上 {}".format(url), file=sys.stderr)
        print("检查：1) 网络/代理是否正常  2) base_url 是否拼错  3) 是否需要走代理", file=sys.stderr)
        print("原始错误：{}".format(exc.reason), file=sys.stderr)
        print("!" * 64 + "\n", file=sys.stderr)
        raise
    except json.JSONDecodeError as exc:
        print("[响应解析失败] 服务端返回的不是合法 JSON：{}".format(exc), file=sys.stderr)
        raise


def _normalize_response(raw: Dict[str, Any], model: str, mock: bool) -> Dict[str, Any]:
    """把服务商原始响应整理成本项目的统一格式。"""
    choices = raw.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}

    tool_calls: List[Dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {}) or {}
        raw_arguments = function.get("arguments")
        # ⚠️ arguments 是 JSON 字符串，不是对象 —— 这是最常见的坑之一
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {"__parse_error__": raw_arguments}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        tool_calls.append(
            {
                "id": call.get("id") or "call_{}".format(len(tool_calls) + 1),
                "name": function.get("name", ""),
                "arguments": arguments,
            }
        )

    usage = raw.get("usage") or {}
    if not usage:
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    return {
        "content": message.get("content") or "",
        "tool_calls": tool_calls,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        },
        "finish_reason": (choices[0].get("finish_reason") if choices else None),
        "model": raw.get("model", model),
        "mock": mock,
        "raw": raw,
    }


# ============================================================================
# 五、对外主类
# ============================================================================

class LLM:
    """
    与大模型对话的客户端。所有章节都用它。

    最常用的两个方法：
        llm.chat(messages)                → 拿统一格式的响应 dict
        llm.chat(messages, tools=[...])   → 带上工具，模型可能返回 tool_calls
        llm.stream_chat(messages)         → 逐块 yield 文本（第 13 章流式输出）
    """

    def __init__(
        self,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        config = _get_config()
        self.system = system
        self.temperature = float(os.environ.get("AGENT_TEMPERATURE", "") or temperature)
        env_max = os.environ.get("AGENT_MAX_TOKENS", "").strip()
        self.max_tokens = max_tokens if max_tokens is not None else (int(env_max) if env_max else None)
        self.model = model or config["model"]
        self.verbose = verbose

        # 累计用量，方便第 02 / 13 章做成本统计
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0

    # ---------------------------------------------------------------- 内部

    def _build_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """如果是纯文本对话且设了 system，就自动补上 system 消息。"""
        prepared = list(messages)
        if self.system and not any(m.get("role") == "system" for m in prepared):
            prepared.insert(0, {"role": "system", "content": self.system})
        return prepared

    def _record_usage(self, usage: Dict[str, int]) -> None:
        self.total_prompt_tokens += usage.get("prompt_tokens", 0)
        self.total_completion_tokens += usage.get("completion_tokens", 0)
        self.call_count += 1

    def _mock_chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        _MOCK.calls += 1
        scripted = _MOCK.next_scripted()
        if scripted is not None:
            response = {
                "content": scripted.get("content", ""),
                "tool_calls": scripted.get("tool_calls", []),
            }
        else:
            response = _mock_respond(messages, tools)

        prompt_tokens = count_message_tokens(messages)
        completion_tokens = count_tokens_approx(response.get("content", "")) + sum(
            count_tokens_approx(json.dumps(c.get("arguments", {}), ensure_ascii=False))
            for c in response.get("tool_calls", [])
        )
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        normalized = {
            "content": response.get("content", ""),
            "tool_calls": [
                {
                    "id": c.get("id") or "mock_call_{}".format(i + 1),
                    "name": c.get("name", ""),
                    "arguments": c.get("arguments", {}),
                }
                for i, c in enumerate(response.get("tool_calls", []))
            ],
            "usage": usage,
            "finish_reason": "tool_calls" if response.get("tool_calls") else "stop",
            "model": "mock",
            "mock": True,
            "raw": None,
        }
        self._record_usage(usage)
        if self.verbose:
            print("[mock] tokens: {}".format(usage))
        return normalized

    # ---------------------------------------------------------------- 对外

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **extra: Any
    ) -> Dict[str, Any]:
        """
        发一次对话请求，返回统一格式的响应 dict。

        参数：
            messages     OpenAI 格式的消息列表
            tools        工具 schema 列表（由 ToolRegistry.to_openai_schema() 生成）
            temperature  覆盖默认温度
            max_tokens   覆盖默认输出上限
            **extra      其他要透传给服务商的参数（如 response_format）

        返回：
            {"content": str, "tool_calls": [...], "usage": {...}, "mock": bool, ...}
        """
        prepared = self._build_messages(messages)

        if is_mock_mode():
            return self._mock_chat(prepared, tools)

        config = _get_config()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": prepared,
            "temperature": self.temperature if temperature is None else temperature,
        }
        resolved_max = self.max_tokens if max_tokens is None else max_tokens
        if resolved_max:
            payload["max_tokens"] = resolved_max
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(extra)

        url = "{}/chat/completions".format(config["base_url"])
        raw = _post_json(url, payload, config["api_key"], config["timeout"])
        result = _normalize_response(raw, self.model, mock=False)
        self._record_usage(result["usage"])
        return result

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **extra: Any
    ) -> Iterator[str]:
        """
        流式对话：逐块 yield 文本增量。

        ⚠️ 注意：流式模式下拿不到工具调用（大多数服务商如此）。
        需要工具调用就用 chat()。第 13 章会讲怎么同时支持两者。
        """
        prepared = self._build_messages(messages)

        if is_mock_mode():
            full = _mock_respond(prepared, None).get("content", "")
            # 按 3-8 个字符一块吐，模拟真实的首字延迟和逐字出现
            step = 4
            for index in range(0, len(full), step):
                yield full[index:index + step]
                time.sleep(0.01)
            return

        config = _get_config()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": prepared,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": True,
        }
        resolved_max = self.max_tokens if max_tokens is None else max_tokens
        if resolved_max:
            payload["max_tokens"] = resolved_max
        payload.update(extra)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "{}/chat/completions".format(config["base_url"]),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(config["api_key"]),
                "Accept": "text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        event = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except urllib.error.HTTPError as exc:
            _raise_readable_http_error(exc, "{}/chat/completions".format(config["base_url"]))
            raise

    # ---------------------------------------------------------------- 统计

    def usage_summary(self) -> str:
        """本客户端累计用量的可读摘要。"""
        total_cost = estimate_cost(self.total_prompt_tokens, self.total_completion_tokens, self.model)
        return (
            "调用次数 {} ｜ 输入 {} tokens ｜ 输出 {} tokens ｜ 估算成本 {}"
            .format(
                self.call_count,
                self.total_prompt_tokens,
                self.total_completion_tokens,
                format_cost(total_cost),
            )
        )

    def reset_usage(self) -> None:
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0


# ============================================================================
# 六、便捷函数（给"只要一句话回答"的场景用）
# ============================================================================

_DEFAULT_LLM: Optional[LLM] = None


def get_llm(system: Optional[str] = None, temperature: float = 0.7, **kwargs: Any) -> LLM:
    """
    拿一个 LLM 客户端。

    传了 system 时每次返回新实例（避免 system 串味）；不传时复用同一个默认实例，
    这样用量统计是累计的。
    """
    global _DEFAULT_LLM
    if system is not None or kwargs:
        return LLM(system=system, temperature=temperature, **kwargs)
    if _DEFAULT_LLM is None:
        _DEFAULT_LLM = LLM(temperature=temperature)
    return _DEFAULT_LLM


def chat(
    prompt: str,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> str:
    """
    最简调用：给一句话，拿一句话回来。

        print(chat("用一句话解释什么是 Agent"))
    """
    llm = LLM(system=system, temperature=temperature, max_tokens=max_tokens)
    response = llm.chat([{"role": "user", "content": prompt}])
    return response["content"]


__all__ = [
    "LLM",
    "ENV_KEYS",
    "PRICE_TABLE_CNY",
    "chat",
    "count_message_tokens",
    "count_tokens_approx",
    "describe_mode",
    "estimate_cost",
    "format_cost",
    "get_llm",
    "get_mock_call_count",
    "is_mock_mode",
    "load_env",
    "print_banner",
    "reset_mock",
    "set_mock_script",
]
