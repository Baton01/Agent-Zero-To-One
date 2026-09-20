"""
工具注册表：把你写的 Python 函数变成模型可以调用的工具。

核心思想（第 04 章的重点）：
    模型**从不执行你的代码**。它只做一件事 —— 输出"我要调哪个函数、参数是什么"。
    真正执行的是你。这个模块负责：
        1. 把函数包装成工具（含给模型看的说明书 schema）
        2. 把注册表翻译成服务商要求的 tools 参数格式
        3. 执行工具，并把**异常也变成字符串返回**（而不是让程序崩溃）
           —— 因为错误要作为观察结果喂回模型，让它自己纠错

用法：
    from common.tools import ToolRegistry

    registry = ToolRegistry()

    @registry.tool(description="计算数学表达式，返回计算结果")
    def calculator(expression: str) -> str:
        return str(eval(expression))          # 教学示例，生产环境别用 eval

    schema = registry.to_openai_schema()      # 喂给 llm.chat(tools=schema)
    print(registry.execute("calculator", {"expression": "1+1"}))
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional


class Tool:
    """
    一个可被模型调用的工具。

    参数：
        name                  工具名（模型看到的名字，建议英文小写下划线）
        description           给模型看的说明书 —— **写得清不清楚直接决定调用准确率**
        parameters            JSON Schema 格式的参数定义
        func                  实际执行的 Python 函数，返回值会被转成字符串喂回模型
        requires_confirmation 危险工具标记为 True，执行前会请求人工确认
        timeout               预期执行耗时上限（秒），仅用于记录与告警
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        func: Callable[..., Any],
        requires_confirmation: bool = False,
        timeout: Optional[float] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.func = func
        self.requires_confirmation = requires_confirmation
        self.timeout = timeout
        self.call_count = 0
        self.error_count = 0

    def to_openai_schema(self) -> Dict[str, Any]:
        """翻译成服务商要求的 tools 参数格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __repr__(self) -> str:
        flag = " [需确认]" if self.requires_confirmation else ""
        return "<Tool {}{}>".format(self.name, flag)


#: 生成 JSON Schema 时，Python 类型 → JSON Schema 类型的映射
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


class ToolRegistry:
    """
    工具注册表：集中管理所有工具，并对外提供两个能力 —— 给模型的 schema、给代码的执行入口。
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        #: 人工确认回调。签名 (tool: Tool, arguments: dict) -> bool，返回 True 表示允许执行
        self._confirmer: Optional[Callable[[Tool, Dict[str, Any]], bool]] = None
        #: 调用日志：每次执行追加一条，第 12 章的 Trace 会用到
        self.call_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ 注册

    def register(self, tool: Tool) -> Tool:
        """注册一个已经构造好的 Tool 对象。"""
        if tool.name in self._tools:
            raise ValueError("工具名重复：{}".format(tool.name))
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        func: Callable[..., Any],
        requires_confirmation: bool = False,
        timeout: Optional[float] = None,
    ) -> Tool:
        """用一个函数直接注册工具。"""
        return self.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                requires_confirmation=requires_confirmation,
                timeout=timeout,
            )
        )

    def tool(
        self,
        name: Optional[str] = None,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        requires_confirmation: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        装饰器写法（推荐，写起来最省事）：

            @registry.tool(description="查询指定城市的实时天气")
            def get_weather(city: str) -> str:
                ...

        不传 parameters 时会**自动从函数签名的类型注解生成**（只有 string/number 两类）。
        想让参数约束更严（枚举、必填、描述），就显式传 parameters。
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or func.__name__
            tool_parameters = parameters or self._infer_parameters(func)
            self.add(
                name=tool_name,
                description=description or (func.__doc__ or "").strip().split("\n")[0],
                parameters=tool_parameters,
                func=func,
                requires_confirmation=requires_confirmation,
            )
            return func

        return decorator

    @staticmethod
    def _infer_parameters(func: Callable[..., Any]) -> Dict[str, Any]:
        """从函数签名推断参数 schema。无法推断的类型一律当字符串处理。"""
        import inspect

        signature = inspect.signature(func)
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param_name, param in signature.parameters.items():
            if param_name in ("self", "cls") or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            annotation = param.annotation
            json_type = _TYPE_MAP.get(annotation, "string")
            if annotation is inspect.Parameter.empty:
                json_type = "string"
            properties[param_name] = {"type": json_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        return {"type": "object", "properties": properties, "required": required}

    # ------------------------------------------------------------ 产出

    def to_openai_schema(self) -> List[Dict[str, Any]]:
        """全部工具的 schema 列表，直接喂给 llm.chat(tools=...)。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def describe(self) -> str:
        """给人看的工具清单（调试用）。"""
        if not self._tools:
            return "（没有注册任何工具）"
        lines = []
        for tool in self._tools.values():
            params = list((tool.parameters.get("properties") or {}).keys())
            flag = " [需确认]" if tool.requires_confirmation else ""
            lines.append("  · {}({}){} —— {}".format(tool.name, ", ".join(params), flag, tool.description))
        return "\n".join(lines)

    # ------------------------------------------------------------ 执行

    def set_confirmer(self, confirmer: Callable[[Tool, Dict[str, Any]], bool]) -> None:
        """
        设置人工确认回调。危险工具执行前会调用它，返回 False 就拒绝执行。

        默认（不设置时）在交互式终端里用 input() 询问，非交互环境一律拒绝 —— 
        安全默认值是"拒绝"，不是"放行"。
        """
        self._confirmer = confirmer

    def _confirm(self, tool: Tool, arguments: Dict[str, Any]) -> bool:
        if self._confirmer is not None:
            return bool(self._confirmer(tool, arguments))

        # ⚠️ 确认界面必须展示**完整参数**，否则用户是在盲点"同意"
        print("\n" + "!" * 60)
        print("[需要人工确认] 即将执行危险操作：{}".format(tool.name))
        print("说明：{}".format(tool.description))
        print("参数：{}".format(json.dumps(arguments, ensure_ascii=False)))
        print("!" * 60)
        import sys

        if not sys.stdin.isatty():
            print("[自动拒绝] 非交互环境，未配置确认回调时默认拒绝执行危险工具。")
            return False
        answer = input("确认执行？输入 yes 继续，其他任何输入都会取消：").strip().lower()
        return answer == "yes"

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        """
        执行工具。**永远返回字符串，永远不抛异常。**

        这是本模块最重要的设计：工具失败不是程序崩溃，而是要作为"观察结果"
        喂回模型，让它决定重试、换工具还是放弃。异常类型也在字符串里标出来，
        方便模型和你的日志区分。
        """
        started = time.time()
        record: Dict[str, Any] = {"tool": name, "arguments": arguments}

        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self.names()) or "（无）"
            result = "[工具错误:NotFound] 不存在名为 '{}' 的工具。可用工具：{}".format(name, available)
            record.update({"ok": False, "result": result, "elapsed": 0.0})
            self.call_log.append(record)
            return result

        if not isinstance(arguments, dict):
            result = "[工具错误:BadArguments] 参数必须是 JSON 对象，实际收到 {}。".format(type(arguments).__name__)
            tool.error_count += 1
            record.update({"ok": False, "result": result, "elapsed": 0.0})
            self.call_log.append(record)
            return result

        if tool.requires_confirmation and not self._confirm(tool, arguments):
            result = "[工具错误:Rejected] 用户拒绝执行 '{}'。请不要重试，改为向用户说明并给出替代方案。".format(name)
            record.update({"ok": False, "result": result, "elapsed": time.time() - started})
            self.call_log.append(record)
            return result

        try:
            raw = tool.func(**arguments)
            result = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
            tool.call_count += 1
            ok = True
        except TypeError as exc:
            # 参数不对：最常见的一类错误
            expected = list((tool.parameters.get("properties") or {}).keys())
            result = (
                "[工具错误:BadArguments] 参数不匹配：{}。该工具期望的参数是 {}，"
                "实际收到 {}。请修正参数后重试。".format(exc, expected, list(arguments.keys()))
            )
            tool.error_count += 1
            ok = False
        except Exception as exc:  # noqa: BLE001 - 故意兜住所有异常，转成给模型看的信息
            result = "[工具错误:{}] {}。请判断是否可以用不同参数重试，或改用其他工具。".format(
                type(exc).__name__, exc
            )
            tool.error_count += 1
            ok = False

        record.update({"ok": ok, "result": result, "elapsed": time.time() - started})
        self.call_log.append(record)

        if tool.timeout and record["elapsed"] > tool.timeout:
            record["timeout_exceeded"] = True

        return result

    def stats(self) -> str:
        """各工具的调用次数与失败次数。"""
        if not self._tools:
            return "（没有注册任何工具）"
        lines = ["工具调用统计："]
        for tool in self._tools.values():
            lines.append(
                "  · {:<20} 调用 {:>3} 次，失败 {:>3} 次".format(tool.name, tool.call_count, tool.error_count)
            )
        return "\n".join(lines)


__all__ = ["Tool", "ToolRegistry"]
