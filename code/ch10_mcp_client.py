"""
第 10 章 · MCP 与通信协议 配套代码

本章学到什么：
  - MCP 把「工具 × 应用」的适配从 M × N 压成 M + N：工具写一次 Server，应用实现一次 Client
  - 三方角色与权力结构：Host 拿权限、Client 管通信、Server 只声明能力；危险动作的许可在 Host
  - 调用流程四步：initialize 握手 → tools/list 列能力 → tools/call 调用 → 收结果，错误也走协议
  - 三种能力分清决策者：Tools（模型提议）、Resources（应用加载）、Prompts（用户选用）
  - 接上 MCP 之后，Agent 的工具箱是从 Server 的 tools/list 动态来的，不是写死在代码里的

怎么跑：
    cd code
    AGENT_MOCK=1 python ch10_mcp_client.py           # 离线：同进程 mock Server，完整三段流程 + 错误分支
    AGENT_MOCK=1 python ch10_mcp_client.py --live    # 用子进程 + stdio 真跑一遍（Windows 下也能跑）
    python ch10_mcp_client.py --serve                # 只当 Server：可以填进 Cursor / Claude Desktop 的配置

    把这一份文件填进客户端的 mcp.json：
    {"mcpServers": {"azto-demo": {"command": "python",
                                  "args": ["ch10_mcp_client.py", "--serve"],
                                  "env": {"PROTOCOL_VERSION": "2025-06-18"}}}}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

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

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "azto-demo-server"
SERVER_VERSION = "1.0.0"
CLIENT_NAME = "azto-demo-client"

#: 危险动作清单 —— 判断"能不能执行"的权力在 Host（也就是这份客户端代码），不在 Server
DANGEROUS_TOOLS = {"delete_file"}


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def dump(label: str, payload) -> None:
    """打印一条 JSON-RPC 报文（真实报文是一行 JSON，这里为了可读做了缩进）。"""
    print("  {} {}".format(label, json.dumps(payload, ensure_ascii=False)[:400]))


def log_to_stderr(message: str) -> None:
    """
    MCP 铁律：stdout 只放协议报文，日志一律写 stderr。

    原因很直接：stdio 传输下 stdout 是"一行一条 JSON"，任何 print 都会把日志混进报文流，
    客户端解析失败 → -32700 Parse error。这个坑几乎每个人都踩过一次。
    """
    sys.stderr.write("[server] {}\n".format(message))
    sys.stderr.flush()


# ============================================================================
# 一、一个最小的 MCP Server（能力表 + 方法分发 + 传输）
# ============================================================================

class MCPError(Exception):
    """协议错误：带标准错误码。错误也走协议，而不是抛给调用方崩掉。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


#: 演示用的虚拟文件系统：不碰真实磁盘，这样 Server 只能在"授权范围内"活动 —— 最小权限原则
VIRTUAL_FS = {
    "README.md": "# 演示项目\n这是一个用于学习 MCP 的最小项目，入口是 main.py。\n",
    "config.json": '{"model": "demo-model", "max_steps": 8}\n',
    "docs/travel.md": "# 差旅制度\n海外出差住宿单晚不超过等值 8000 日元。\n",
    "src/main.py": "def main():\n    print('hello mcp')\n",
}

TOOLS = [
    {
        "name": "read_file",
        "description": "读取虚拟文件系统里某个文件的完整内容。参数 path 是相对路径，如 README.md",
        "inputSchema": {"type": "object",
                        "properties": {"path": {"type": "string", "description": "文件相对路径"}},
                        "required": ["path"]},
    },
    {
        "name": "list_dir",
        "description": "列出虚拟文件系统里的文件清单，可指定子目录。用于了解项目结构",
        "inputSchema": {"type": "object",
                        "properties": {"subdir": {"type": "string", "description": "子目录，默认根目录"}},
                        "required": []},
    },
    {
        "name": "search_docs",
        "description": "在全部文件里搜索关键词，返回命中的文件名与行内容",
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string", "description": "要搜索的关键词"}},
                        "required": ["query"]},
    },
    {
        "name": "delete_file",
        "description": "删除虚拟文件系统里的文件。这是危险动作，需要宿主人工确认",
        "inputSchema": {"type": "object",
                        "properties": {"path": {"type": "string", "description": "文件相对路径"}},
                        "required": ["path"]},
    },
]

RESOURCES = [
    {"uri": "file://README.md", "name": "README", "mimeType": "text/markdown"},
    {"uri": "config://project", "name": "项目配置", "mimeType": "application/json"},
]

PROMPTS = [
    {"name": "code_review", "description": "按固定清单审查一段代码",
     "arguments": [{"name": "path", "description": "要审查的文件", "required": True}]},
]


class DemoMcpServer(object):
    """同进程的 mock MCP Server：能力表 + 方法分发。真实 Server 只是把这些换成真实现。"""

    def __init__(self):
        self.requests = []          # 收到的请求（含被拒绝前的），用于验证"拒绝时请求没发出去"
        self.handlers = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "prompts/list": self._prompts_list,
        }

    # ------------------------------------------------------------ 方法实现

    def _initialize(self, params):
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_list(self, _params):
        return {"tools": TOOLS}

    def _resources_list(self, _params):
        return {"resources": RESOURCES}

    def _prompts_list(self, _params):
        return {"prompts": PROMPTS}

    def _check_schema(self, tool, arguments):
        """Server 侧也要校验参数 —— 不能假设调用方是善意的。"""
        schema = tool["inputSchema"]
        for key in schema.get("required", []):
            if key not in arguments:
                raise MCPError(-32602, "缺少必需参数：{}（工具 {}）".format(key, tool["name"]))
        types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                 "array": list, "object": dict}
        for key, spec in (schema.get("properties") or {}).items():
            if key not in arguments:
                continue
            expected = types.get(spec.get("type"))
            value = arguments[key]
            if expected and (not isinstance(value, expected) or isinstance(value, bool) and expected is not bool):
                raise MCPError(-32602, "参数 {} 类型不对：期望 {}，收到 {}".format(
                    key, spec.get("type"), type(value).__name__))

    def _tools_call(self, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = next((item for item in TOOLS if item["name"] == name), None)
        if tool is None:
            raise MCPError(-32602, "工具不存在：{}（用 tools/list 看有哪些）".format(name))
        if not isinstance(arguments, dict):
            raise MCPError(-32602, "arguments 必须是对象")
        self._check_schema(tool, arguments)

        if name == "read_file":
            path = str(arguments["path"])
            if path not in VIRTUAL_FS:
                raise MCPError(-32002, "文件不存在：{}".format(path))
            return {"content": [{"type": "text", "text": VIRTUAL_FS[path]}], "isError": False}
        if name == "list_dir":
            subdir = str(arguments.get("subdir", "") or "")
            names = [path for path in sorted(VIRTUAL_FS) if path.startswith(subdir)]
            return {"content": [{"type": "text", "text": "\n".join(names) or "（空目录）"}], "isError": False}
        if name == "search_docs":
            query = str(arguments["query"])
            lines = []
            for path in sorted(VIRTUAL_FS):
                for number, line in enumerate(VIRTUAL_FS[path].splitlines(), start=1):
                    if query in line:
                        lines.append("{}:{}: {}".format(path, number, line.strip()))
            return {"content": [{"type": "text",
                                 "text": "\n".join(lines) if lines else "（没有命中：{}）".format(query)}],
                    "isError": False}
        if name == "delete_file":
            path = str(arguments["path"])
            if path not in VIRTUAL_FS:
                raise MCPError(-32002, "文件不存在：{}".format(path))
            del VIRTUAL_FS[path]
            return {"content": [{"type": "text", "text": "已删除 {}".format(path)}], "isError": False}
        raise MCPError(-32603, "未实现的工具：{}".format(name))

    # ------------------------------------------------------------ 分发

    def handle(self, request):
        """
        JSON-RPC 2.0 的全部规则都在这 20 行里。

        要点：① 缺 jsonrpc/method → -32600；② 没有 id 的是通知，不响应；
             ③ 方法不存在 → -32601；④ 自定义错误码走 MCPError。
        """
        self.requests.append(request)
        if request.get("jsonrpc") != "2.0" or "method" not in request:
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32600, "message": "Invalid Request"}}
        if "id" not in request:
            log_to_stderr("收到通知：{}（通知不响应）".format(request["method"]))
            return None
        handler = self.handlers.get(request["method"])
        if handler is None:
            return {"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32601, "message": "Method not found: {}".format(request["method"])}}
        try:
            result = handler(request.get("params") or {})
        except MCPError as err:
            return {"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": err.code, "message": err.message}}
        except Exception as exc:  # noqa: BLE001 - 服务端内部错误也要变成协议错误，而不是崩掉连接
            return {"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32603, "message": "Internal error: {}".format(exc)}}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    # ------------------------------------------------------------ stdio 传输

    def serve_stdio(self):
        """stdio 传输：一行一条 JSON，读到 EOF 退出。日志一律走 stderr。"""
        log_to_stderr("{} v{} 已启动，等待客户端请求……（stdout 只放协议报文）".format(SERVER_NAME, SERVER_VERSION))
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32700, "message": "Parse error"}}) + "\n")
                sys.stdout.flush()
                continue
            response = self.handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        log_to_stderr("stdin 读到 EOF，退出。")


# ============================================================================
# 二、Host 侧的 MCP Client
# ============================================================================

class McpClient(object):
    """
    Host 里的连接器：负责握手、收发、权限门。一个 Server 对应一个 Client。

    transport 有两种：同进程（直接调 server.handle，教学/测试用）与 stdio（真起子进程）。
    """

    def __init__(self, server=None, process=None, auto_approve=False, echo=True):
        self.server = server
        self.process = process
        self.auto_approve = auto_approve
        self.echo = echo
        self.next_id = 1
        self.server_info = {}
        self.tools = []
        self.audit = []

    # ------------------------------------------------------------ 传输层

    def _read_line(self, timeout=20.0):
        """
        带超时地读一行。

        Windows 下没法用 select 监视管道，所以这里用一个读线程 + join(timeout) 兜底：
        子进程卡死时给出「可诊断的报错」，而不是让整条命令挂在那里等到天荒地老。
        """
        holder = {}

        def reader():
            try:
                holder["line"] = self.process.stdout.readline()
            except Exception as exc:  # noqa: BLE001 - 读失败也要变成可读的错误
                holder["error"] = exc

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError("Server 在 {} 秒内没有响应（可能卡死或没按行输出）；检查它的 stderr".format(timeout))
        if "error" in holder:
            raise RuntimeError("读取 Server 输出失败：{}".format(holder["error"]))
        return holder.get("line", "")

    def _send(self, request, expect_response=True):
        if self.process is not None:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            if not expect_response:
                # 通知没有 id，服务端按协议**不会**回应 —— 在这里等响应就会一直等下去
                return None
            line = self._read_line()
            if not line:
                raise RuntimeError("Server 没有响应就退出了（看它的 stderr）")
            return json.loads(line)
        return self.server.handle(request)

    def _call(self, method, params=None):
        """发一条请求，返回 (result, error)。echo=True 时把报文打出来 —— 这是本章最该看的东西。"""
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            request["params"] = params
        self.next_id += 1
        if self.echo:
            dump("--> ", request)
        response = self._send(request)
        if self.echo:
            dump("<-- ", response)
        self.audit.append({"request": request, "response": response})
        return response.get("result"), response.get("error")

    # ------------------------------------------------------------ 三段流程

    def initialize(self):
        result, error = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {}, "sampling": {}},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0.0"},
        })
        if error:
            raise RuntimeError("握手失败：{}".format(error))
        self.server_info = result.get("serverInfo", {})
        # 握手之后还有一条 notifications/initialized（没有 id 就是通知，服务端不回应）
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        if self.echo:
            dump("--> ", notification)
        self._send(notification, expect_response=False)
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            print("  [版本警告] 服务端协议版本 {} 与客户端 {} 不一致，应明确报错而不是'试着用用看'".format(
                result.get("protocolVersion"), PROTOCOL_VERSION))
        return result

    def list_tools(self):
        result, error = self._call("tools/list", {})
        if error:
            raise RuntimeError("列能力失败：{}".format(error))
        self.tools = result.get("tools", [])
        return self.tools

    def call_tool(self, name, arguments):
        """
        宿主侧权限门：危险工具没批准，**连请求都不发出去**。

        这条设计是本章的重点：Server 无权决定自己能被怎么用，"要不要发这个调用"由 Host 判断。
        """
        if name in DANGEROUS_TOOLS and not self.auto_approve:
            error = {"code": -32001, "message": "宿主策略拒绝：{} 需要人工确认后再调用".format(name)}
            if self.echo:
                print("  [权限门] 拒绝 {} —— 请求没有发给 Server（它压根看不到这次调用）".format(name))
                dump("<-- ", {"jsonrpc": "2.0", "id": self.next_id, "error": error})
            self.audit.append({"request": {"method": "tools/call", "params": {"name": name}},
                               "response": {"error": error}, "blocked": True})
            return None, error
        result, error = self._call("tools/call", {"name": name, "arguments": arguments})
        return result, error

    def close(self):
        if self.process is not None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 关不掉就算了，不要影响主流程
                self.process.kill()


def result_text(result) -> str:
    """把 tools/call 的返回体拼成纯文本（content 可能是多段）。"""
    if not result:
        return ""
    parts = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


# ============================================================================
# 三、把 MCP 工具接进 ToolRegistry，然后跑一个 Agent 循环
# ============================================================================

def tools_to_registry(client, prefix="mcp_", only=None) -> ToolRegistry:
    """
    能力发现的结果直接变成 Agent 的工具箱：Server 加了工具，客户端不用改一行代码。

    四个工程细节：
      ① 加前缀避免多 Server 之间的重名（两个 Server 都有 search 会让模型乱调）；
      ② 参数 schema 直接用 Server 给的 inputSchema，不要自己重写；
      ③ 工具错误照样返回字符串（喂回模型让它自己纠错），不要让异常冲出循环；
      ④ **只开需要的工具**（only=）：工具 schema 常驻上下文，开一堆用不上的只会烧 token 并干扰判断。
    """
    registry = ToolRegistry()
    for tool in client.tools:
        if only is not None and tool["name"] not in only:
            continue

        def _call(name=tool["name"], **kwargs):
            result, error = client.call_tool(name, kwargs)
            if error:
                return "[工具错误:{}] {}".format(error["code"], error["message"])
            return result_text(result) or "（工具返回空内容）"

        registry.add(
            name=prefix + tool["name"],
            description=tool["description"],
            parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
            func=_call,
            requires_confirmation=tool["name"] in DANGEROUS_TOOLS,
        )
    return registry


def run_agent(question: str, registry: ToolRegistry, max_steps: int = 4):
    """最小 ReAct 循环：模型提议工具 → Host 放行 → 执行 → 结果喂回。用的工具全部来自 MCP。"""
    llm = get_llm(system="你是项目助手，需要读文件或搜索时调用工具，最后给出简短结论。", temperature=0.2)
    trace = Trace("mcp_agent")
    messages = [{"role": "user", "content": question}]
    for step in range(1, max_steps + 1):
        response = llm.chat(messages, tools=registry.to_openai_schema())
        trace.llm_call(step, response)
        calls = response.get("tool_calls") or []
        text = (response.get("content") or "").strip()
        if text:
            print("  [第 {} 步·模型] {}".format(step, text[:70].replace("\n", " ")))
        if not calls:
            trace.log("stop", reason="模型给出最终答案")
            return text, trace
        messages.append({"role": "assistant", "content": text or "", "tool_calls": [
            {"id": call["id"], "type": "function",
             "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
            for call in calls]})
        for call in calls:
            print("  [第 {} 步·工具] {}({})".format(step, call["name"],
                                                    json.dumps(call["arguments"], ensure_ascii=False)))
            result = registry.execute(call["name"], call["arguments"])
            trace.tool_call(step, call["name"], call["arguments"], result, 0.0)
            print("  [第 {} 步·观察] {}".format(step, result[:90].replace("\n", " ")))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    trace.log("stop", reason="达到步数上限")
    return "（达到步数上限，未得出结论）", trace


# ============================================================================
# 四、各段演示
# ============================================================================

def demo_handshake(client: McpClient):
    section("第 3 节 · 三段流程：initialize → tools/list → tools/call")
    print("  [第 1 步] 握手：协商协议版本与双方能力（唯一一次自报家门）")
    info = client.initialize()
    print("  → 服务端：{} v{}，协议 {}，声明能力：{}".format(
        client.server_info.get("name"), client.server_info.get("version"),
        info.get("protocolVersion"), list(info.get("capabilities", {}).keys())))

    print("\n  [第 2 步] 列能力：客户端不需要预先知道服务端有什么（能力发现）")
    tools = client.list_tools()
    for tool in tools:
        params = list((tool.get("inputSchema") or {}).get("properties", {}).keys())
        print("     · {}({}) —— {}".format(tool["name"], ", ".join(params), tool["description"][:36]))
    schema_text = json.dumps(tools, ensure_ascii=False)
    print("  → 共 {} 个工具，tools/list 报文 {} 字节（约 {} token，**每次请求都常驻上下文**）".format(
        len(tools), len(schema_text), count_tokens_approx(schema_text)))
    print("     所以只开需要的工具：一个 Agent 接 2~3 个 Server 是常见配置，接 10 个是给自己找麻烦。")

    print("\n  [第 3 步] 调用工具")
    client.echo = True
    result, error = client.call_tool("read_file", {"path": "README.md"})
    print("  → 返回内容：{}".format(result_text(result).replace("\n", " ")[:60]))

    print("\n  [第 4 步] 还有两种能力：Resources（数据，应用/用户选择加载）与 Prompts（模板，用户选用）")
    for method, label in (("resources/list", "Resources"), ("prompts/list", "Prompts")):
        payload, _error = client._call(method, {})
        items = payload.get("resources") or payload.get("prompts") or []
        print("     {}：{}".format(label, ", ".join(item.get("uri") or "/" + item.get("name") for item in items)))
    print("     决策者不同：Tools 由模型提议、Resources 由应用加载、Prompts 由用户主动选用。")


def demo_errors(client: McpClient):
    section("第 3.4 节 · 错误也必须走协议")
    cases = [
        ("调用未知方法", lambda: client._call("tools/delete_everything", {})),
        ("调用不存在的工具", lambda: client.call_tool("rename_file", {"path": "README.md"})),
        ("参数不合法（缺 path）", lambda: client.call_tool("read_file", {})),
        ("参数类型不对（path 传数字）", lambda: client.call_tool("read_file", {"path": 123})),
        ("业务错误（文件不存在）", lambda: client.call_tool("read_file", {"path": "不存在的文件.md"})),
        ("宿主策略拒绝（危险工具未确认）", lambda: client.call_tool("delete_file", {"path": "config.json"})),
    ]
    for label, action in cases:
        print("\n  [场景] {}".format(label))
        client.echo = False
        _result, error = action()
        client.echo = True
        if error:
            print("     错误码 {}：{}".format(error["code"], error["message"]))
    print("\n  标准错误码：-32700 报文不是合法 JSON ｜ -32600 缺字段 ｜ -32601 方法不存在")
    print("              -32602 参数不合法/工具不存在 ｜ -32603 服务端内部错误 ｜ -32001 宿主策略拒绝")
    print("  最重要的一条：危险工具被拒绝时，请求**根本没发给 Server**。用 Server 收到的请求验证一下：")
    if client.server is not None:
        blocked = [item for item in client.server.requests
                   if (item.get("params") or {}).get("name") == "delete_file"]
        print("     Server 侧收到的 delete_file 请求数 = {}（Host 拒绝 → 发都不发，Server 看不到这次调用）".format(
            len(blocked)))
        print("     这说明权限门在 Host：Server 无权决定自己能被怎么用。")


def demo_agent_via_mcp(client: McpClient):
    section("第 5 节 · 最有价值的一段：接上 MCP，Agent 就多了一批工具")
    registry = tools_to_registry(client, only=["read_file"])
    skipped = [tool["name"] for tool in client.tools if tool["name"] != "read_file"]
    print("  [转换] tools/list → ToolRegistry：{}".format(", ".join(registry.names())))
    print("  [工具开关] 本次只开 read_file（最小必需）；先不开：{}".format(", ".join(skipped)))
    print("             理由：工具 schema 会常驻上下文，开一堆用不上的既烧 token 又干扰模型判断。")
    print(registry.describe())
    question = "请读取 README.md 这个文件的内容，告诉我这个演示项目叫什么名字"
    print("\n  [Agent 循环] 问题：{}".format(question))
    answer, trace = run_agent(question, registry)
    print("\n  [最终回答] {}".format((answer or "").replace("\n", " ")[:120]))
    print("  [工具统计] {}".format(registry.stats().replace("\n", " ")))

    print("\n  [权限门演示] 如果把危险工具 delete_file 开给模型，Host 的确认门会拦住它：")
    full = tools_to_registry(client)
    refused = []

    def confirmer(tool, arguments):
        refused.append(tool.name)
        print("     宿主确认门：拒绝执行 {}（参数 {}）".format(tool.name, json.dumps(arguments, ensure_ascii=False)))
        return False

    full.set_confirmer(confirmer)
    output = full.execute("mcp_delete_file", {"path": "config.json"})
    print("     工具返回给模型的内容：{}".format(output[:80]))
    print("     → 模型拿到的是「用户拒绝执行」这个观察结果，它会改口说明而不是硬干（安全默认值是拒绝，不是放行）")
    print("\n  [说明] 这些工具的名字、描述、参数 schema 全部来自 Server 的 tools/list：")
    print("         Server 加一个工具，客户端不用改一行代码 —— 这就是能力发现（Capability Discovery）的价值。")
    return trace


def demo_live():
    """--live：把自己作为 Server 子进程拉起来，走真正的 stdio 传输。"""
    section("--live 模式 · 子进程 + stdio 的真实传输")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"     # Windows 下不做这件事，中文会变乱码或抛编码异常
    process = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=None, env=env, text=True, encoding="utf-8", bufsize=1)
    client = McpClient(process=process)
    try:
        client.initialize()
        tools = client.list_tools()
        print("  [live] 从子进程拿到 {} 个工具：{}".format(len(tools), ", ".join(t["name"] for t in tools)))
        result, error = client.call_tool("read_file", {"path": "README.md"})
        print("  [live] read_file → {}".format(result_text(result).replace("\n", " ")[:70]))
        _result, error = client.call_tool("read_file", {"path": "不存在.md"})
        print("  [live] 错误分支 → {}".format(error))
    finally:
        client.close()
    print("  [live] 传输方式：Host 用 command + args 拉起子进程，之后一行一条 JSON 走 stdin/stdout。")
    print("         本机资源用 stdio；要被多人/多机器共享才上 HTTP（代价是鉴权、抖动、数据出境）。")


def report_config():
    section("第 4 节 · 想接真实 MCP Server 时怎么配")
    config = {"mcpServers": {"azto-demo": {"command": "python",
                                           "args": ["ch10_mcp_client.py", "--serve"],
                                           "env": {"PROTOCOL_VERSION": PROTOCOL_VERSION}}}}
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print("\n  三个字段的含义：command 启动什么进程，args 是启动参数，env 是额外环境变量。")
    print("  GUI 客户端里 `python` 可能不在 PATH 上 —— 配置里写绝对路径（如 {}）。".format(sys.executable))
    print("  换成远程 Server 就是 `url` 字段（HTTP/SSE 传输）：此时数据会离开你的机器，接入前先问三件事：")
    print("     发到哪个域名？存多久？我发过去的字段里有没有密钥？")
    print("\n  本脚本默认**不联网**，只连自己起的子进程/同进程 mock；要接真实 Server 才需要改上面这份配置。")


def report_checklist(trace: Trace):
    section("第 6 节 · 接入前检查清单（接任何第三方 Server 前逐条过）")
    for index, item in enumerate([
        "它有哪些工具？逐个看名字和描述，尤其有没有删除/写入/执行命令这类动作。",
        "权限范围写清楚了吗？文件系统是只读某个目录，还是整个磁盘？",
        "它会把我的数据发到哪里？本地进程还是远程 HTTP？域名是谁的？",
        "工具返回的内容会不会包含指令（Prompt 注入）？返回内容必须当成数据，不是命令。",
        "密钥怎么传？有没有把它写进日志或返回给模型？",
        "能不能限制工具数量？先只开 1-2 个最小必需的，验证过再加。",
        "出错时的行为是什么？超时多久？会不会无限重试？",
        "谁维护的、多久没更新了、有没有人审过源码？",
    ], start=1):
        print("  [ ] {}. {}".format(index, item))
    print("\n  [本次演示的结论] 这个 mock Server 可以接：只读 + 虚拟文件系统 + 危险动作有宿主权限门。")
    print("                  真实第三方 Server 至少要做到「最小权限 + 可审计」才值得接。")
    section("本轮 Trace 摘要（MCP 调用也要留痕，否则审不出'谁在什么时候动了什么'）")
    print(trace.summary())
    calls = [entry for entry in trace.entries if entry["event"] == "tool_call"]
    for entry in calls:
        print("  · 第 {} 步 {}({}) → {}".format(entry.get("step"), entry.get("tool"),
                                                json.dumps(entry.get("arguments", {}), ensure_ascii=False),
                                                str(entry.get("result", ""))[:40].replace("\n", " ")))


def main() -> int:
    load_env()
    if "--serve" in sys.argv:
        # 只当 Server：给外部 MCP 客户端用（这两行是 stdio 传输能处理中文的关键）
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        DemoMcpServer().serve_stdio()
        return 0

    print_banner("第 10 章 · MCP 与通信协议：握手 / 列能力 / 调用 / 权限门")
    print("  角色：Host（本脚本）→ Client（McpClient）→ Server（DemoMcpServer）；权限在 Host 手里")
    print("  默认使用同进程 mock Server，保证离线、跨平台、编码稳定；--live 走真子进程 + stdio")

    if "--live" in sys.argv:
        demo_live()

    client = McpClient(server=DemoMcpServer())
    demo_handshake(client)
    demo_errors(client)
    trace = demo_agent_via_mcp(client)
    report_config()
    report_checklist(trace)

    section("本章要点回顾")
    for line in [
        "1. MCP 的贡献是接入标准化：把 M × N 份适配压成 M + N 份；工具签名变更只改 Server 一处。",
        "2. 权力结构：Host 拿权限、Client 管通信、Server 只声明能力；危险动作的许可在 Host。",
        "3. 三种能力分清决策者：Tools 由模型提议、Resources 由应用加载、Prompts 由用户选用。",
        "4. 调用流程四步：握手 → 列能力 → 调工具 → 收结果；通知没有 id，不响应。",
        "5. 错误也走协议：-32601 方法不存在、-32602 参数不合法、-32001 宿主策略拒绝；stdout 只放报文。",
        "6. 能力发现让工具箱动态化：tools/list 的结果直接变成 ToolRegistry 的工具，客户端不用改代码。",
        "7. 安全是自己负责的：最小权限、工具返回当数据、危险动作人工确认、锁版本 + 审计。",
    ]:
        print("  " + line)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
