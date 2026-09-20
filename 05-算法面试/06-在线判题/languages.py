"""
多语言判题后端。

设计取舍（为什么长这样）
------------------------
Python 判题靠"驱动脚本 import 用户代码"，但 C++ 没有动态能力，两条路可选：

    A. 把用例值烧进源码 —— 每个用例都要重编译。一次提交 6~8 次 g++（每次 ~2 秒），
       学员提交一次要等十几秒，不可接受。
    B. 二进制运行时读用例 —— 需要 JSON 解析。

选 B：**一份源码编译一次，二进制从 stdin 读用例 JSON、把结果打成 JSON 输出**。
JSON 解析器是手写的最小实现，在 cpp_runtime.py 里（用户看不到）。

代价是每种语言都要带一份运行时；好处是编译只发生一次，
而且此后每个用例的执行开销都只有进程启动（几毫秒）。

新增一门语言的成本：写一份 `xxx_runtime.py`（类型定义 + 输入解析 + 结果序列化），
再在下面加一个 Backend 子类。Java / Go 都是这个套路。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from cpp_runtime import (  # noqa: E402
    CPP_COMPILE_FLAGS,
    CPP_LINK_FLAGS,
    CPP_MAIN_FUNCTION,
    CPP_MAIN_OPERATIONS,
    CPP_MEM_FN,
    CPP_PRELUDE,
    line_offset as cpp_line_offset,
)

#: 用户程序 stdout 上限，防止 print 刷屏把内存打爆
MAX_OUTPUT = 200_000
#: 编译超时（秒）—— 编译比运行宽松，但也不能让它挂死
COMPILE_TIMEOUT = 60.0

RESULT_MARK = "AZTO_RESULT"
MEM_MARK = "AZTO_MEM"


# ---------------------------------------------------------------------------
# 语言清单与可用性探测
# ---------------------------------------------------------------------------

LANGUAGE_LABELS = {
    "python": "Python 3",
    "cpp": "C++17",
}


def _probe_python():
    return True, "%d.%d.%d" % sys.version_info[:3], ""


def _probe_cpp():
    exe = shutil.which("g++") or shutil.which("clang++")
    if not exe:
        return False, "", "没找到 g++ 或 clang++。装一个 MinGW-w64 或 LLVM 就行。"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or "").split("\n")[0].strip()
        version = first.replace(" (", " | ").split("|")[0][:60] if first else exe
        return True, version, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", "g++ 无法执行：%s" % exc


PROBES = {
    "python": _probe_python,
    "cpp": _probe_cpp,
}

#: 探测结果缓存（探测要起子进程，不能每次请求都跑）
_PROBE_CACHE = {}


def language_status(force: bool = False):
    """返回 {语言: {available, label, version, reason}}，用于 /status 与网页上的语言选择器。"""
    result = {}
    for name, label in LANGUAGE_LABELS.items():
        if not force and name in _PROBE_CACHE:
            result[name] = _PROBE_CACHE[name]
            continue
        try:
            available, version, reason = PROBES[name]()
        except Exception as exc:                       # noqa: BLE001
            available, version, reason = False, "", "探测失败：%s" % exc
        entry = {"available": available, "label": label, "version": version, "reason": reason}
        _PROBE_CACHE[name] = entry
        result[name] = entry
    return result


# ---------------------------------------------------------------------------
# C++ 类型推导
# ---------------------------------------------------------------------------

INT_MIN, INT_MAX = -2147483648, 2147483647

#: C++ 类型 → 从 JVal 取值的转换表达式
CPP_CONVERT = {
    "int": "toInt",
    "long long": "toLong",
    "double": "toDouble",
    "bool": "toBool",
    "string": "toStr",
    "vector<int>": "toIntVec",
    "vector<long long>": "toLongVec",
    "vector<double>": "toDoubleVec",
    "vector<bool>": "toBoolVec",
    "vector<string>": "toStrVec",
    "vector<vector<int>>": "toIntMat",
    "vector<vector<string>>": "toStrMat",
    "vector<vector<char>>": "toCharMat",
    "ListNode*": "mklist",
    "TreeNode*": "mktree",
}


def cpp_type_of(value, hint=""):
    """
    从真实的用例值推出 C++ 类型。

    为什么要推而不是让题目元数据写死：98 道题 × 每个参数都手写类型标注，
    既容易错又难维护；而用例值本身已经把类型信息带全了。
    `hint`（题目元数据里的 types 字段）只在值表达不出来时兜底，
    比如链表/树在 JSON 里就是普通数组，跟 vector<int> 长得一样。
    """
    if hint in ("ListNode", "TreeNode"):
        return hint + "*"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if INT_MIN <= value <= INT_MAX else "long long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if not value:
            return "vector<int>"                    # 空数组只能默认 int
        if any(item is None for item in value):
            return "vector<int>"                    # 含 null 的数组应是树，靠 hint 兜
        inner = [cpp_type_of(item) for item in value]
        unique = set(inner)
        if len(unique) == 1:
            inner_type = inner[0]
            if inner_type in ("int", "long long", "double", "string", "bool"):
                # 全是单字符串的嵌套数组，多半是字符网格（岛屿数量这类题）
                return "vector<%s>" % inner_type
            return "vector<%s>" % inner_type
        # int 和 double 混在一起 → 提升成 double
        if unique <= {"int", "long long", "double"}:
            return "vector<double>"
        return "vector<int>"
    return "int"


def cpp_param_types(problem, case):
    """按参数名给出本题这一用例里每个参数的 C++ 类型。"""
    hints = problem.get("types") or {}
    overrides = problem.get("cppTypes") or {}
    types = {}
    for name in problem.get("params", []):
        if name in overrides:
            types[name] = overrides[name]
            continue
        types[name] = cpp_type_of(case["input"].get(name), hints.get(name, ""))
    return types


def cpp_char_matrix_hint(problem, case):
    """字符网格的特例：嵌套数组 + 全是长度 1 的字符串 → vector<vector<char>>。"""
    hints = dict(problem.get("types") or {})
    for name in problem.get("params", []):
        value = case["input"].get(name)
        if (isinstance(value, list) and value and isinstance(value[0], list)
                and value[0] and all(isinstance(x, str) and len(x) == 1 for x in value[0])):
            hints[name] = "charmat"
    return hints


def cpp_literal(value):
    """把一个简单标量写成 C++ 字面量（类设计题的操作参数用）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return '"%s"' % escaped
    if value is None:
        return "0"
    if isinstance(value, list):
        return "{" + ", ".join(cpp_literal(item) for item in value) + "}"
    return str(value)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class Backend:
    name = ""
    label = ""
    extension = ""
    compiles = False

    def prepare(self, code, problem, workdir):
        """写源文件；编译型语言在这里完成编译。返回 {'ok', 'error', 'detail'}"""
        raise NotImplementedError

    def run_case(self, workdir, problem, case, case_index, timeout, memory_limit_kb):
        """跑一个用例，返回 {'status', 'actual', 'expected', 'ms', 'memoryKB', 'error'}"""
        raise NotImplementedError


def _read_last_marked(text, mark):
    """取标记之后的最后一个值。用户代码可能也 print，所以按标记找而不是按行号找。"""
    index = text.rfind(mark)
    if index < 0:
        return None
    rest = text[index + len(mark):]
    return rest.split("\n")[0].strip()


def _classify_failure(returncode, stdout, stderr):
    """把进程异常退出归类成给学员看的原因"""
    if returncode == 0:
        return None
    low = (stderr or "").lower()
    if "recursion" in low or "stack overflow" in low or returncode in (0xC00000FD, 3221225725):
        return "递归深度超限（栈溢出）—— 检查递归的终止条件，或者深度太大时改用迭代"
    if returncode in (0xC0000094, 3221225620):
        return "除零错误（Integer division by zero）"
    if returncode in (0xC0000005, 3221225477):
        return "段错误（非法内存访问）—— 常见原因：数组越界、访问空指针、迭代器失效"
    if "memory" in low or returncode == 0xC0000017:
        return "内存分配失败"
    signal_names = {6: "Aborted（abort/断言失败）", 11: "段错误（Segmentation fault）",
                    8: "浮点异常", 9: "被强制终止"}
    if returncode < 0:
        return signal_names.get(-returncode, "被信号 %d 终止" % -returncode)
    detail = (stderr or "").strip()
    if detail:
        return "程序异常退出（exit code %s）：%s" % (returncode, detail[:400])
    return "程序异常退出（exit code %s）" % returncode


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

PY_DRIVER = r'''# -*- coding: utf-8 -*-
"""判题驱动（由 harness.py 自动生成，不要手改）"""
import json
import sys
import time


# ---- LeetCode 的两个基础数据结构 ------------------------------------------
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

    def __repr__(self):
        return "ListNode(%r)" % (self.val,)


class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

    def __repr__(self):
        return "TreeNode(%r)" % (self.val,)


def _build_list(values):
    """[1,2,3] -> 链表。空列表返回 None。支持带环：值里出现 {"cycle": i} 时在尾部接回第 i 个节点。"""
    if values is None:
        return None
    cycle_at = None
    if values and isinstance(values[-1], dict) and "cycle" in values[-1]:
        cycle_at = values[-1]["cycle"]
        values = values[:-1]
    if not values:
        return None
    nodes = [ListNode(v) for v in values]
    for i in range(len(nodes) - 1):
        nodes[i].next = nodes[i + 1]
    if cycle_at is not None and 0 <= cycle_at < len(nodes):
        nodes[-1].next = nodes[cycle_at]
    return nodes[0]


def _dump_list(node, with_cycle=False):
    """链表 -> [1,2,3]；最多走 20000 步，防止环形链表把判题挂住"""
    out = []
    seen = set()
    while node is not None and len(out) < 20000:
        if with_cycle:
            if id(node) in seen:
                return {"values": out, "cycle_at": out.index(node.val) if out else 0}
            seen.add(id(node))
        out.append(node.val)
        node = node.next
    return out


def _build_tree(values):
    """LeetCode 层序格式：[1,None,2,3] -> 二叉树"""
    if not values:
        return None
    root = TreeNode(values[0])
    queue = [root]
    i = 1
    while queue and i < len(values):
        node = queue.pop(0)
        if i < len(values) and values[i] is not None:
            node.left = TreeNode(values[i])
            queue.append(node.left)
        i += 1
        if i < len(values) and values[i] is not None:
            node.right = TreeNode(values[i])
            queue.append(node.right)
        i += 1
    return root


def _dump_tree(root):
    """二叉树 -> LeetCode 层序格式（去掉尾部多余的 None）"""
    if root is None:
        return []
    out = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        if node is None:
            out.append(None)
            continue
        out.append(node.val)
        queue.append(node.left)
        queue.append(node.right)
    while out and out[-1] is None:
        out.pop()
    return out


def _build_graph(adjacency):
    """邻接表 -> {val: [邻居节点]}，用于图论题"""
    if adjacency is None:
        return None
    nodes = {}
    for i, neighbours in enumerate(adjacency):
        node = type("Node", (), {})()
        node.val = i + 1
        node.neighbors = []
        nodes[i + 1] = node
    for i, neighbours in enumerate(adjacency):
        nodes[i + 1].neighbors = [nodes[j] for j in neighbours]
    return nodes[1] if nodes else None


def _dump_graph(node):
    if node is None:
        return []
    seen = {}
    order = []
    stack = [node]
    while stack:
        cur = stack.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen[id(cur)] = len(order)
        order.append(cur)
        for nb in getattr(cur, "neighbors", []) or []:
            if id(nb) not in seen:
                stack.append(nb)
    result = [[] for _ in order]
    for cur in order:
        for nb in getattr(cur, "neighbors", []) or []:
            if id(nb) in seen:
                result[seen[id(cur)]].append(seen[id(nb)] + 1)
    return result


BUILDERS = {
    "ListNode": _build_list,
    "TreeNode": _build_tree,
    "GraphNode": _build_graph,
}
DUMPERS = {
    "ListNode": _dump_list,
    "TreeNode": _dump_tree,
    "GraphNode": _dump_graph,
}


def decode(value, type_name):
    if type_name in BUILDERS:
        return BUILDERS[type_name](value)
    return value


def encode(value, type_name):
    if type_name in DUMPERS:
        return DUMPERS[type_name](value)
    if isinstance(value, ListNode):
        return _dump_list(value)
    if isinstance(value, TreeNode):
        return _dump_tree(value)
    return value


def peak_memory_kb():
    """
    本进程的峰值内存（KB）。

    不用 tracemalloc —— 它对每次分配都要记账，会明显拖慢用户代码，把"内存测量"
    变成"超时来源"。直接问操作系统要峰值工作集：Windows 走 psapi，类 Unix 走
    getrusage。两者都是标准库，零开销。
    """
    try:
        if sys.platform.startswith("win"):
            import ctypes
            import ctypes.wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.wintypes.DWORD),
                            ("PageFaultCount", ctypes.wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.windll.kernel32
            # 必须声明返回类型：不声明的话 ctypes 按 c_int 处理，
            # 64 位下 GetCurrentProcess() 的伪句柄会被截断，
            # 后续调用直接 ERROR_INVALID_HANDLE(6) 返回 0。
            kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE

            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None) \
                or ctypes.windll.psapi.GetProcessMemoryInfo
            get_info.restype = ctypes.wintypes.BOOL
            get_info.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_PMC),
                                 ctypes.wintypes.DWORD]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize // 1024)
        else:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux 的 ru_maxrss 单位是 KB，macOS 是字节
            return int(usage // 1024) if sys.platform == "darwin" else int(usage)
    except Exception:
        pass
    return 0


def main():
    # payload 从 stdin 读，不走 argv —— Windows 命令行有 32767 字符上限，
    # 大用例（上万个元素）走 argv 会直接 FileNotFoundError: WinError 206。
    payload = json.loads(sys.stdin.read())
    solution_path = payload["solution"]
    entry = payload["entry"]
    kind = payload.get("kind", "function")
    params = payload.get("params", [])
    types = payload.get("types", {})
    return_type = payload.get("returnType", "")

    started = time.time()

    # 载入用户代码。把数据结构注入它的命名空间，这样用户可以直接写 ListNode(...)
    namespace = {"ListNode": ListNode, "TreeNode": TreeNode, "__name__": "solution"}
    try:
        with open(solution_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        exec(compile(source, "solution.py", "exec"), namespace)
    except SyntaxError as exc:
        print(json.dumps({"ok": False, "kind": "syntax", "error": "第 %s 行语法错误：%s" % (exc.lineno, exc.msg)}))
        return
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": "load", "error": "%s: %s" % (type(exc).__name__, exc)}))
        return

    # ---- 模式二：设计题（LRU 缓存 / Trie / 最小栈这类"对一系列操作给出输出"的题）----
    if kind == "operations":
        if entry not in namespace:
            available = [k for k, v in namespace.items()
                         if not k.startswith("_") and k not in ("ListNode", "TreeNode")]
            print(json.dumps({"ok": False, "kind": "entry",
                              "error": "代码里找不到类 %s。当前可用的名字：%s" % (entry, available or "无")}))
            return
        cls = namespace[entry]
        ops = payload["ops"]
        outputs = []
        obj = None
        index = 0
        op = ""
        try:
            for index, item in enumerate(ops):
                op = item[0]
                args = item[1] if len(item) > 1 else []
                if index == 0:
                    obj = cls(*args)
                    outputs.append(None)
                else:
                    method = getattr(obj, op, None)
                    if method is None:
                        print(json.dumps({"ok": False, "kind": "entry",
                                          "error": "第 %d 个操作调用了 %s，但类里没有这个方法" % (index + 1, op)}))
                        return
                    outputs.append(encode(method(*args), ""))
        except Exception as exc:
            print(json.dumps({"ok": False, "kind": "runtime",
                              "error": "第 %d 个操作（%s）抛出 %s: %s" % (
                                  index + 1, op, type(exc).__name__, exc)}))
            return
        elapsed = (time.time() - started) * 1000
        print(json.dumps({"ok": True, "actual": outputs, "ms": round(elapsed, 2),
                          "memory": peak_memory_kb()}))
        return

    # ---- 模式一：普通函数题 ----
    args = payload["args"]

    # 找入口：先找模块级函数，再找 class Solution 的方法
    func = namespace.get(entry)
    if func is None:
        solution_cls = namespace.get("Solution")
        if solution_cls is not None and hasattr(solution_cls, entry):
            try:
                func = getattr(solution_cls(), entry)
            except Exception as exc:
                print(json.dumps({"ok": False, "kind": "load",
                                  "error": "无法实例化 class Solution：%s" % exc}))
                return
    if func is None:
        available = [k for k, v in namespace.items()
                     if callable(v) and not k.startswith("_") and k not in ("ListNode", "TreeNode")]
        print(json.dumps({"ok": False, "kind": "entry",
                          "error": "代码里找不到函数 %s。当前可用的函数：%s" % (entry, available or "无")}))
        return

    # 组装参数（按 params 声明的顺序，用名字从 args 取）
    try:
        call_args = [decode(args[name], types.get(name, "")) for name in params]
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": "input", "error": "构造输入失败：%s" % exc}))
        return

    inplace = payload.get("inplace")
    try:
        result = func(*call_args)
    except RecursionError:
        print(json.dumps({"ok": False, "kind": "runtime",
                          "error": "递归深度超限（RecursionError）—— 递归解法请检查终止条件"}))
        return
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": "runtime",
                          "error": "%s: %s" % (type(exc).__name__, exc)}))
        return

    elapsed = (time.time() - started) * 1000
    try:
        # 原地修改题（移动零 / 轮转数组 / 矩阵置零 / 旋转图像）判的是**被改动的参数**，
        # 不是返回值 —— LeetCode 上这类题约定返回 None，只比返回值会把正确解法判错。
        if inplace:
            target = payload.get("compareArg") or params[0]
            encoded = encode(call_args[params.index(target)], types.get(target, ""))
        else:
            encoded = encode(result, return_type)
        json.dumps(encoded)  # 先序列化一次，确认能序列化
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": "output",
                          "error": "返回值无法序列化：%s" % exc}))
        return
    print(json.dumps({"ok": True, "actual": encoded, "ms": round(elapsed, 2),
                      "memory": peak_memory_kb()}))


if __name__ == "__main__":
    main()
'''


class PythonBackend(Backend):
    name = "python"
    label = "Python 3"
    extension = ".py"
    compiles = False

    def prepare(self, code, problem, workdir):
        with open(os.path.join(workdir, "solution.py"), "w", encoding="utf-8") as handle:
            handle.write(code)
        with open(os.path.join(workdir, "driver.py"), "w", encoding="utf-8") as handle:
            handle.write(PY_DRIVER)
        return {"ok": True, "error": "", "detail": ""}

    def _payload(self, workdir, problem, case):
        payload = {
            "solution": os.path.join(workdir, "solution.py"),
            "entry": problem["entry"],
            "kind": problem.get("kind", "function"),
            "params": problem.get("params", []),
            "types": problem.get("types", {}),
            "returnType": problem.get("returnType", ""),
        }
        if problem.get("kind") == "operations":
            payload["ops"] = case["ops"]
        else:
            payload["args"] = case["input"]
            if problem.get("inplace"):
                payload["inplace"] = True
                payload["compareArg"] = problem.get("compareArg") or problem["params"][0]
        return payload

    def run_case(self, workdir, problem, case, case_index, timeout, memory_limit_kb):
        started = time.time()
        payload = self._payload(workdir, problem, case)
        try:
            completed = subprocess.run(
                [sys.executable, os.path.join(workdir, "driver.py")],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True, timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "ms": round((time.time() - started) * 1000, 2),
                    "memoryKB": 0,
                    "error": "运行超过 %.1f 秒 —— 检查是不是死循环，或者算法复杂度过高" % timeout}

        stdout = completed.stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT]
        stderr = completed.stderr.decode("utf-8", errors="replace")[:4000]
        elapsed = round((time.time() - started) * 1000, 2)

        line = ""
        for candidate in reversed(stdout.strip().split("\n")):
            if candidate.startswith("{"):
                line = candidate
                break
        if not line:
            return {"status": "error", "ms": elapsed, "memoryKB": 0,
                    "error": "程序没有正常返回结果。" + ("stderr: " + stderr.strip()[:600] if stderr.strip() else "")}
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            return {"status": "error", "ms": elapsed, "memoryKB": 0, "error": "判题驱动输出无法解析"}

        if not result.get("ok"):
            kind = result.get("kind", "error")
            status = "syntax" if kind == "syntax" else ("entry" if kind == "entry" else "error")
            return {"status": status, "ms": elapsed, "memoryKB": 0,
                    "error": result.get("error", "未知错误")}

        return _finish(result.get("actual"), result.get("memory", 0),
                       result.get("ms", elapsed), case, memory_limit_kb)


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------

class CppBackend(Backend):
    name = "cpp"
    label = "C++17"
    extension = ".cpp"
    compiles = True

    # ---------------------------------------------------------------- 生成

    def _generate(self, code, problem, workdir):
        """生成完整 .cpp 源码。返回 (源码, 用户代码起始行号)。"""
        if problem.get("kind") == "operations":
            tail = CPP_MAIN_OPERATIONS.replace("__MEM_FN__", CPP_MEM_FN) \
                                     .replace("__BODY__", self._operations_body(problem))
        else:
            decls, invoke = self._function_body(problem)
            tail = CPP_MAIN_FUNCTION.replace("__MEM_FN__", CPP_MEM_FN) \
                                    .replace("__DECLS__", decls) \
                                    .replace("__INVOKE__", invoke)
        return CPP_PRELUDE + code + "\n" + tail, cpp_line_offset()

    def _function_body(self, problem):
        """
        生成"取参数 + 调用 + 接结果"的代码。

        参数先落到具名变量再传进去，是刻意的：直接传转换函数的返回值是个右值，
        用户如果写 `void f(vector<int>& a)` 就绑不上（右值不能绑非常量引用），
        会报一句让人摸不着头脑的编译错误。具名变量是左值，按值/按引用都能接。
        """
        entry = problem["entry"]
        params = problem.get("params", [])
        cases = problem.get("cases") or []
        first_case = cases[0] if cases else {"input": {}}
        types = cpp_param_types(problem, first_case)

        # 字符网格特例
        hints = cpp_char_matrix_hint(problem, first_case)
        for name in params:
            if hints.get(name) == "charmat":
                types[name] = "vector<vector<char>>"

        decls = []
        args = []
        for index, name in enumerate(params):
            cpp_type = types.get(name, "int")
            convert = CPP_CONVERT.get(cpp_type)
            if convert is None:
                cpp_type, convert = "int", "toInt"
            decls.append("    %s _a%d = %s(_root[\"%s\"]);" % (cpp_type, index, convert, name))
            args.append("_a%d" % index)

        # 用户可能写 class Solution（力扣风格），也可能写自由函数
        uses_solution = bool(re.search(r"\bclass\s+Solution\b", problem.get("_user_code", "")))
        invoke = []
        if uses_solution:
            invoke.append("    Solution _sol;")
            call = "_sol.%s(%s)" % (entry, ", ".join(args))
        else:
            call = "%s(%s)" % (entry, ", ".join(args))
        if problem.get("inplace"):
            # 原地修改题的 C++ 签名是 void（移动零、矩阵置零、旋转图像……），
            # 不能用 auto 接返回值 —— `auto _result = 一个返回 void 的函数()` 会报
            # "'void _result' has incomplete type"。要判的是**被改动的那个参数**。
            target = problem.get("compareArg") or (params[0] if params else "")
            index = params.index(target) if target in params else 0
            invoke.append("    %s;" % call)
            invoke.append("    string _OUT = jout(_a%d);" % index)
        else:
            invoke.append("    auto _result = %s;" % call)
            invoke.append("    string _OUT = jout(_result);")

        return "\n".join(decls), "\n".join(invoke)

    def _operations_body(self, problem):
        """
        类设计题的每个用例展开成一段显式调用代码，用 argv[1] 选跑哪一段。

        C++ 没有反射，没法在运行时"按名字调方法"，所以只能在编译期把
        操作序列写死。好在一个题目只有几个用例、每个用例几十个操作，
        展开出来的源码规模完全可控 —— 换来的是仍然只编译一次。
        """
        entry = problem["entry"]
        cases = problem.get("cases") or []
        lines = []

        for index, case in enumerate(cases):
            lines.append("    %s (_idx == %d) {" % ("if" if index == 0 else "} else if", index))
            ops = case.get("ops") or []
            expected = case.get("expected") or []

            for position, op in enumerate(ops):
                method = op[0]
                args = op[1] if len(op) > 1 else []
                arg_text = ", ".join(cpp_literal(a) for a in args)
                if position == 0:
                    # 空参数列表不能写成 `Trie _obj();` —— 那是**函数声明**不是对象定义
                    # （C++ 的 most vexing parse），后面 _obj.insert(...) 会报
                    # "_obj is of non-class type 'Trie()'"。
                    if arg_text:
                        lines.append("        %s _obj(%s);" % (entry, arg_text))
                    else:
                        lines.append("        %s _obj;" % entry)
                    want = None
                else:
                    want = expected[position] if position < len(expected) else None
                    if want is None:
                        lines.append("        _obj.%s(%s);" % (method, arg_text))

                # 分隔符静态决定，不用运行时标志位。
                # 每个用例块只在 _idx 相等时执行，块内第一条输出一定是该用例的第一条；
                # 用运行时变量反而会把"是不是本用例的第一条"和"是不是整段代码的第一条"
                # 搞混 —— 前一个用例把它置成 false，后面的用例就全带上多余的逗号。
                separator = '""' if position == 0 else '","'
                if want is None:
                    lines.append('        _res += %s; _res += "null";' % separator)
                else:
                    call = "null" if position == 0 else "_obj.%s(%s)" % (method, arg_text)
                    lines.append("        _res += %s; _res += jout(%s);" % (separator, call))

        if cases:
            lines.append("    }")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 运行

    def prepare(self, code, problem, workdir):
        # 生成源码时要看用户有没有写 class Solution，先把代码挂到题目上
        problem = dict(problem)
        problem["_user_code"] = code

        source, offset = self._generate(code, problem, workdir)
        src_path = os.path.join(workdir, "solution.cpp")
        exe_path = os.path.join(workdir, "solution.exe" if os.name == "nt" else "solution")
        with open(src_path, "w", encoding="utf-8") as handle:
            handle.write(source)

        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            return {"ok": False, "error": "没找到 C++ 编译器（g++ / clang++）",
                    "detail": "装一个 MinGW-w64 或 LLVM 就能用 C++ 判题。"}

        flags = CPP_COMPILE_FLAGS + ["-o", exe_path, src_path] + CPP_LINK_FLAGS
        try:
            completed = subprocess.run([compiler] + flags, capture_output=True,
                                       text=True, timeout=COMPILE_TIMEOUT, cwd=workdir)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "编译超时（超过 %.0f 秒）" % COMPILE_TIMEOUT, "detail": ""}
        except OSError as exc:
            return {"ok": False, "error": "无法执行编译器：%s" % exc, "detail": ""}

        if completed.returncode != 0:
            return {"ok": False, "error": self._clean_compile_error(completed.stderr, offset),
                    "detail": completed.stderr}
        return {"ok": True, "error": "", "detail": ""}

    @staticmethod
    def _clean_compile_error(stderr, offset):
        """
        把编译错误里的行号映射回用户代码。

        g++ 报的是"生成文件"的行号，而生成文件前面有 400 多行运行时。
        不减去偏移的话，学员会看到 "solution.cpp:437: 错误"，可他的代码只有 12 行 ——
        完全不知道去改哪里。
        """
        out = []
        pattern = re.compile(r"^(.*?):(\d+):(\d+):\s*(error|warning|note):\s*(.*)$")
        for raw in (stderr or "").split("\n"):
            line = raw.rstrip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                _, lineno, col, level, message = match.groups()
                user_line = int(lineno) - offset
                if user_line >= 1:
                    out.append("第 %d 行第 %d 列：%s" % (user_line, int(col), message))
                elif level == "error":
                    # 落在运行时里 —— 一般是用户写的函数签名和调用对不上
                    out.append("（第 %d 行是判题运行时，不是你写的代码）%s" % (int(lineno), message))
                continue
            if "error:" in line or line.startswith("collect2"):
                out.append(line.strip())
        text = "\n".join(out[:20])
        return text or (stderr or "")[:1500]

    def run_case(self, workdir, problem, case, case_index, timeout, memory_limit_kb):
        exe_path = os.path.join(workdir, "solution.exe" if os.name == "nt" else "solution")
        started = time.time()

        # 函数题把用例 JSON 从 stdin 喂进去；设计题按索引选展开好的那段代码
        if problem.get("kind") == "operations":
            command = [exe_path, str(case_index)]
            stdin_data = b""
        else:
            command = [exe_path]
            stdin_data = json.dumps(case["input"], ensure_ascii=False).encode("utf-8")

        try:
            completed = subprocess.run(command, input=stdin_data, capture_output=True,
                                       timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "ms": round((time.time() - started) * 1000, 2),
                    "memoryKB": 0,
                    "error": "运行超过 %.1f 秒 —— 检查是不是死循环，或者算法复杂度过高" % timeout}

        stdout = completed.stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT]
        stderr = completed.stderr.decode("utf-8", errors="replace")[:4000]
        elapsed = round((time.time() - started) * 1000, 2)

        failure = _classify_failure(completed.returncode, stdout, stderr)
        if failure:
            return {"status": "error", "ms": elapsed, "memoryKB": 0, "error": failure}

        raw = _read_last_marked(stdout, RESULT_MARK)
        if raw is None:
            return {"status": "error", "ms": elapsed, "memoryKB": 0,
                    "error": "程序没有输出结果标记 —— 检查你的函数是不是被提前 return 掉了"}
        try:
            actual = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "error", "ms": elapsed, "memoryKB": 0,
                    "error": "结果无法解析：%s" % raw[:200]}

        memory_raw = _read_last_marked(stdout, MEM_MARK)
        try:
            memory_kb = int(memory_raw) if memory_raw else 0
        except ValueError:
            memory_kb = 0

        return _finish(actual, memory_kb, elapsed, case, memory_limit_kb)


# ---------------------------------------------------------------------------
# 结果判定：所有语言共用
# ---------------------------------------------------------------------------

def _finish(actual, memory_kb, elapsed, case, memory_limit_kb):
    """统一做内存检查与输出比对（各语言后端共用的收尾逻辑）。"""
    if memory_kb and memory_kb > memory_limit_kb:
        return {"status": "memory", "actual": None,
                "expected": case.get("expected", case.get("output")),
                "ms": elapsed, "memoryKB": memory_kb,
                "error": "峰值内存 %d MB，超过上限 %d MB —— 检查是不是存了不必要的中间结果" % (
                    memory_kb // 1024, memory_limit_kb // 1024)}

    expected = case.get("expected", case.get("output"))
    mode = case.get("compare", "exact")
    passed = _compare(actual, expected, mode)
    return {"status": "pass" if passed else "fail", "actual": actual, "expected": expected,
            "ms": elapsed, "memoryKB": memory_kb, "error": ""}


def _normalize(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    return value


def _unordered_key(value):
    if isinstance(value, (list, tuple)):
        return sorted(_unordered_key(v) for v in value)
    if isinstance(value, dict):
        return sorted((k, _unordered_key(v)) for k, v in value.items())
    return value


def _compare(actual, expected, mode="exact"):
    if mode == "any_of":
        return any(_compare(actual, candidate, "exact") for candidate in expected)
    a, b = _normalize(actual), _normalize(expected)
    if mode == "unordered":
        try:
            return _unordered_key(a) == _unordered_key(b)
        except TypeError:
            return a == b
    return a == b


# ---------------------------------------------------------------------------
# 解题模板生成（网页上"新建代码"用的骨架）
# ---------------------------------------------------------------------------

def _cpp_scalar_type(values):
    """从一组期望值推出 C++ 返回类型。全是 null 就是 void。"""
    kinds = set()
    for value in values:
        if value is None:
            kinds.add("void")
        elif isinstance(value, bool):
            kinds.add("bool")
        elif isinstance(value, int):
            kinds.add("int")
        elif isinstance(value, float):
            kinds.add("double")
        elif isinstance(value, str):
            kinds.add("string")
        elif isinstance(value, list):
            inner = _cpp_scalar_type(value)
            kinds.add("vector<%s>" % inner)
        else:
            kinds.add("int")
    kinds.discard("void")
    if not kinds:
        return "void"
    if len(kinds) == 1:
        return kinds.pop()
    if kinds <= {"int", "double"}:
        return "double"
    return "int"


def cpp_starter(problem):
    """
    生成 C++ 解题骨架。

    为什么放在 Python 这边生成：返回类型没法凭空知道 —— 要从期望值反推
    （`get` 的期望是数字就是 int，`put` 的期望是 null 就是 void）。
    把这些逻辑放在判题器里，网页端只负责显示。
    """
    entry = problem.get("entry", "Solution")
    cases = problem.get("cases") or []

    if problem.get("kind") == "operations":
        ops = cases[0].get("ops") or [] if cases else []
        lines = ["class %s {" % entry, "public:"]
        if ops:
            ctor_args = ops[0][1] if len(ops[0]) > 1 else []
            params = ", ".join("%s arg%d" % (_cpp_scalar_type([a]), i)
                               for i, a in enumerate(ctor_args))
            lines.append("    %s(%s) {" % (entry, params))
            lines.append("        // 初始化你的数据结构")
            lines.append("    }")
            # 每个方法：参数类型取首次调用的实参，返回类型看全部期望值
            seen = []
            for op in ops[1:]:
                if op[0] not in [x[0] for x in seen]:
                    seen.append((op[0], op[1] if len(op) > 1 else []))
            for name, args in seen:
                # ops 与 expected 是平行数组：第 i 个操作的返回值就是 expected[i]
                returns = []
                for case in cases:
                    case_ops = case.get("ops") or []
                    case_expected = case.get("expected") or []
                    for i, item in enumerate(case_ops):
                        if item[0] == name and i < len(case_expected):
                            returns.append(case_expected[i])
                params = ", ".join("%s arg%d" % (_cpp_scalar_type([a]), i)
                                   for i, a in enumerate(args))
                lines.append("")
                lines.append("    %s %s(%s) {" % (_cpp_scalar_type(returns), name, params))
                lines.append("        // 在这里实现 %s" % name)
                lines.append("    }")
        lines.append("};")
        return "\n".join(lines) + "\n"

    params = problem.get("params", [])
    first_case = cases[0] if cases else {"input": {}}
    types = cpp_param_types(problem, first_case)
    hints = cpp_char_matrix_hint(problem, first_case)
    for name in params:
        if hints.get(name) == "charmat":
            types[name] = "vector<vector<char>>"

    returns = [c.get("expected", c.get("output")) for c in cases]
    if problem.get("inplace"):
        return_type = "void"
    elif problem.get("returnType") in ("ListNode", "TreeNode"):
        return_type = problem["returnType"] + "*"
    else:
        return_type = _cpp_scalar_type(returns)

    def with_ref(cpp_type):
        # 只有容器按引用；标量与指针按值 —— 与力扣 C++ 签名一致
        return cpp_type + "&" if cpp_type.startswith("vector<") else cpp_type

    arg_text = ", ".join("%s %s" % (with_ref(types.get(n, "int")), n) for n in params)
    lines = ["class Solution {", "public:",
             "    %s %s(%s) {" % (return_type, entry, arg_text),
             "        // 在这里写你的解法",
             "    }",
             "};"]
    return "\n".join(lines) + "\n"


BACKENDS = {
    "python": PythonBackend(),
    "cpp": CppBackend(),
}


def get_backend(name):
    if name not in BACKENDS:
        raise ValueError("不支持的语言：%s（可选：%s）" % (name, "、".join(BACKENDS)))
    return BACKENDS[name]
