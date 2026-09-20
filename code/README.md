# code · 配套代码使用说明

> 《Agent Zero To One · 从零开始学 AI Agent》的**可运行**代码目录。
> **没有 API Key 也能跑完所有章节**：`AGENT_MOCK=1` 离线模式下不联网、不花钱，循环、工具调用、检索、校验全部真实执行，只有"想"的那部分由内置规则代替。

> 💡 **不想记路径？** 项目根目录有启动器，在根目录直接跑：
> ```bash
> python run.py 12          # 跑第 12 章
> python run.py 5 --part 3  # 带参数跑第 05 章
> python run.py             # 列出全部章节
> ```
> 它在 `code/` 目录下执行脚本，并自动处理 Windows 中文编码。下面的说明都是"在 code/ 目录内"的写法，两种方式等价。

---

## 一、先离线跑通，再上真模型

唯一推荐的上手顺序（反了的话，遇到问题你分不清是环境、是 Key、还是自己的代码）：

```bash
cd code
AGENT_MOCK=1 python ch12_eval.py      # 第 1 步：离线跑通，看流程与指标
python ch12_eval.py                   # 第 3 步：配好 .env 后，同一份代码打真模型
```

离线模式是**真的在跑**：`llm.py` 按规则决定"这一步模型想调用哪个工具"，然后真的执行你注册的 Python 函数、真的回灌结果、真的解析 tool_call、真的记 token 与成本 —— 调试控制流、验证理解、做完所有动手练习都够用。**唯一的区别**：离线测的是"你的代码逻辑"，不是"模型行为"；判定器、指标、权限门、降级逻辑可以离线测，**模型效果必须在线跑一次**。

## 二、环境配置（三步，5 分钟）

**第 1 步：确认 Python 版本**（需要 3.9 及以上）：`python --version`

**第 2 步：配置 Key**（可选，不配就自动走离线模式）

```bash
cd code
cp .env.example .env        # Windows CMD 用：copy .env.example .env
```

编辑 `code/.env` 的三个必填项：

| 字段 | 说明 | 常见错误 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 服务商的 Key | 前后有多余空格或引号 |
| `OPENAI_BASE_URL` | 接口地址，**必须以 `/v1` 结尾** | 漏 `/v1` → 404 |
| `OPENAI_MODEL` | 模型名，**必须与服务商匹配** | 填了别家的模型名 → 404 |

`.env.example` 里列好了 DeepSeek / 通义千问 / 智谱 / Kimi / 硅基流动 / OpenAI 的常见取值。`load_env()` 的查找顺序：当前目录的 `.env` → `code/.env`。
**第 3 步：安装可选依赖**（不装也能跑，装了有增强）：`pip install -r requirements.txt`（国内慢加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）

| 依赖 | 服务于 | 不装会怎样 |
| --- | --- | --- |
| `openai` | 官方 SDK | 不影响：默认用标准库 `urllib` 直连 |
| `numpy` / `jieba` | 第 08 章向量检索与分词 | 降级为纯 Python 实现，结果一致、速度慢 |
| `fastapi` / `uvicorn` | 第 13 章 Web 骨架 | 跳过 Web 部分，其余功能照常 |

**验证**：`python -c "import sys; sys.path.insert(0,'.'); from common.llm import load_env, describe_mode; load_env(); print(describe_mode())"` —— 打印 `[在线模式] ...` 说明读到 Key，打印 `[离线 Mock 模式]` 说明没读到（或设了 `AGENT_MOCK=1`），一样能跑。

## 三、17 个脚本清单

| 脚本名 | 对应章节 | 一句话功能 | 可离线跑 |
| --- | --- | --- | --- |
| `ch00_env_check.py` | 00 环境准备 | 不装包，用标准库亲手发一次请求，看懂每个字段 | 是 |
| `ch01_first_call.py` | 01 认识 AI Agent | 分清 Chatbot / Workflow / Agent，知道何时**不该**用 Agent | 是 |
| `ch02_tokens_cost.py` | 02 大模型基础 | 会数 Token、会算成本、会调参数、会判断幻觉 | 是 |
| `ch03_prompt_structured.py` | 03 Prompt 与结构化输出 | 稳定输出可解析 JSON，并用校验与重试兜住失败 | 是 |
| `ch04_tool_calling.py` | 04 工具调用 | 从零手写不依赖框架的工具调用循环 | 是 |
| `ch05_react_loop.py` | 05 ReAct 最小 Agent Loop | 50 行写出会思考、会选工具、会纠错、会停下的 Agent | 是 |
| `ch06_paradigms.py` | 06 范式进阶 | CoT / Plan-and-Execute / Reflection 的适用判断 | 是 |
| `ch07_memory.py` | 07 记忆与上下文工程 | 三层记忆 + 选择/压缩/隔离/编排管住上下文预算 | 是 |
| `ch08_rag.py` | 08 RAG 全链路 | 加载→切片→检索→融合→重排→带引用回答→评估 | 是 |
| `ch09_skills.py` | 09 Skills 能力包 | 把流程沉淀成能自动触发的 `SKILL.md`，用 A/B 证明有效 | 是 |
| `ch10_mcp_client.py` | 10 MCP 与通信协议 | 把工具接入的 M×N 变成 M+N，能接也能写最小 Server | 是 |
| `ch11_multi_agent.py` | 11 多智能体协作 | 契约、协调模式与四道保险，把多 Agent 做成可控系统 | 是 |
| `ch12_eval.py` | 12 评估可观测性与安全 | 20 条评测集 + 四级判定器 + pass@k + 8 类失败分类 + 确认门 | 是 |
| `ch13_deploy.py` | 13 部署成本与性能 | 流式 TTFT / 两级缓存 / 重试与抖动 / 三级预算熔断 | 是 |
| `ch14_mini_harness.py` | 14 构建自己的 Agent 框架 | 九模块 mini harness：会话、权限门、子 Agent、trace | 是 |
| `ch15_agent_app.py` | 15 综合实战 | 三个可交付项目（研究助手 / 文档问答 / 代码审查）切换 | 是 |
| `ch16_graduation.py` | 16 毕业设计与面试 | 生成毕设骨架（README 四问 + 评测集 + 配置模板） | 是 |

说明：上表是全书 17 章的配套脚本清单（章节说明见 `docs/README.md`）；本目录已提供其中绝大部分，它们共用同一套底座与同一个离线开关，用法一致。带参数的几个：

```bash
python ch12_eval.py --report out/eval.json --check out/eval.json   # 评测报告 + CI 门禁
python ch13_deploy.py --serve                                      # 打印 Web 骨架启动方式
python ch14_mini_harness.py --debug                                # 每步打印调试信息
python ch15_agent_app.py --mode research|qa|review|all             # 三个项目切换
python ch16_graduation.py --init my-project --dry-run              # 看会生成哪些文件
```

---

## 四、四种运行方式

**1. 离线模式**（第一步，零成本零风险）

```bash
AGENT_MOCK=1 python ch12_eval.py                            # 基本用法
AGENT_MOCK=1 EVAL_FLAKY=1 python ch12_eval.py               # 第 12 章：mock 按固定种子抖动，验证 pass@k 口径
AGENT_MOCK=1 AGENT_DRY_RUN=1 python ch14_mini_harness.py    # 第 14 章：工具只返回模拟结果，无副作用
```

**2. 在线模式**（需要 `.env`；Agent 循环会放大 token 消耗，先用小模型和短 prompt 调）

```bash
python ch12_eval.py
```

**3. 直接运行**（每个脚本都有 `if __name__ == "__main__"`、中文输出与关键指标）：`cd code && AGENT_MOCK=1 python ch15_agent_app.py --mode all`

**4. 导入成模块用**（把它们当零件组装自己的东西）

```python
import sys
sys.path.insert(0, "code")            # 换成你机器上的实际路径
from common.llm import load_env, get_llm
from common.tools import ToolRegistry

load_env()
registry = ToolRegistry()
registry.tool(description="计算数学表达式，返回结果")(lambda expression: str(eval(expression)))
llm = get_llm(system="你是一个严谨的助手")
reply = llm.chat([{"role": "user", "content": "算一下 128 * 3.5 + 42"}], tools=registry.to_openai_schema())
for call in reply["tool_calls"]:
    print(registry.execute(call["name"], call["arguments"]))     # 工具由你执行，模型只提请求
print(llm.usage_summary())
```

---

## 五、`common/` 三个模块 API 速查

### `common/llm.py` —— 大模型调用（唯一的服务商边界）

```python
load_env(path=None, override=False) -> dict      # 读 .env（当前目录 → code/.env）
is_mock_mode() -> bool                           # True = 离线规则模拟
describe_mode() -> str                           # 一行说明当前模式
print_banner(title) -> None                      # 横幅：标题 + 当前模式
get_llm(system=None, temperature=0.7, **kw) -> LLM   # 传 system 返回新实例，否则复用默认实例
chat(prompt, system=None, temperature=0.7, max_tokens=None) -> str   # 最简一问一答
LLM.chat(messages, tools=None, temperature=None, max_tokens=None, **extra) -> dict
    # {"content": str, "tool_calls": [{"id","name","arguments"}], "usage": {...}, "mock": bool}
LLM.stream_chat(messages, ...) -> Iterator[str]  # 逐块 yield；流式下拿不到 tool_calls
LLM.usage_summary() -> str / LLM.reset_usage() -> None
count_tokens_approx(text) -> int                 # 粗略估算（±30%），不能用于对账
count_message_tokens(messages) -> int
estimate_cost(prompt_tokens, completion_tokens, model=None) -> float   # 人民币元
format_cost(amount_cny) -> str                   # 友好格式：¥0.0012
PRICE_TABLE_CNY                                  # 常见模型每百万 token 价格（教学估值）
set_mock_script(responses) / reset_mock() / get_mock_call_count()      # 离线确定性测试
```

### `common/tools.py` —— 把 Python 函数变成模型可调用的工具

```python
registry = ToolRegistry()
registry.add(name, description, parameters, func, requires_confirmation=False, timeout=None) -> Tool
registry.tool(name=None, description="", parameters=None, requires_confirmation=False)  # 装饰器
registry.to_openai_schema() -> list              # 喂给 llm.chat(tools=...)
registry.names() / get(name) / describe() / stats() / call_log
registry.set_confirmer(fn(tool, arguments) -> bool)   # 危险工具的确认回调；默认"拒绝"
registry.execute(name, arguments) -> str         # 永远返回字符串，永远不抛异常
Tool(name, description, parameters, func, requires_confirmation=False, timeout=None)   # 手写版
```

> `execute()` 三条设计：① 参数校验失败 → 返回"该工具期望的参数是 X，你传的是 Y"；② 工具抛异常 → 返回 `[工具错误:类型] 原因`；③ 危险工具被拒绝 → 返回"请不要重试"。**错误要作为观察结果喂回模型，而不是让程序崩。**

### `common/trace.py` —— 记录一次运行的每一步

```python
trace = Trace(name, verbose=False)
trace.log(event, **fields) -> dict               # 自动带 seq / time / elapsed
trace.llm_call(step, response, note="") -> dict   # content / tool_calls / tokens
trace.tool_call(step, name, arguments, result, elapsed) -> dict   # 自动推断 ok
trace.counts() / token_usage() / tool_failures() / find_stop_reason() / summary()
trace.to_dict() / to_json(indent=2) / save(path=None) -> str      # 默认存 ./traces/<名字>_<时间戳>.json
```

---

## 六、常见问题

**1. 中文乱码 / `UnicodeEncodeError`** —— Windows 控制台默认不是 UTF-8。解法：`PYTHONIOENCODING=utf-8 python ch12_eval.py`（临时前缀）、`chcp 65001`（切控制台）、`set PYTHONIOENCODING=utf-8`（永久；PowerShell 用 `$env:PYTHONIOENCODING="utf-8"`）。代码里不用 emoji 就是为了避开这个坑。

**2. `ModuleNotFoundError: No module named 'common'`** —— 本目录脚本已做自动引导（把自身目录插进 `sys.path`），从任意目录用绝对路径运行都可以。自己新建脚本时在开头加上：

```python
import os, sys
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path: sys.path.insert(0, _CODE_DIR)     # 再加 from common.llm import ...
```

**3. `No module named 'fastapi' / 'numpy' / 'jieba'`** —— 这些是**可选**依赖。第 13 章用 `try: import fastapi / except ImportError:` 包住了，没装会跳过那一段继续跑完；第 08 章会降级为纯 Python 实现。要装就 `pip install -r requirements.txt`。

**4. 脚本"没反应" / 卡住 / 输出一半停住** —— ① 第 13 章流式输出是逐块打印的（离线每块 sleep 0.01 秒），跑完要几秒到几十秒，不是卡死；② 在线模式在等网络（`AGENT_TIMEOUT` 默认 60 秒），先 `AGENT_MOCK=1` 跑一遍即可定位是代码问题还是配置问题；③ 确认在 `code/` 目录下运行（`.env` 与 `traces/` 相对当前目录找）；④ Ctrl+C 后先看 `traces/` 里有没有文件——有就说明事件已落盘，能复盘。

**5. 报 `401 / 403 / 404 / 429`** —— 按顺序：① `.env` 是否在 `code/` 下、Key 有没有多余空格引号；② `OPENAI_BASE_URL` 是否以 `/v1` 结尾、`OPENAI_MODEL` 是否匹配该服务商；③ 429 看返回体是 `insufficient_quota`（没钱）还是 `rate_limit_exceeded`（太快）；④ 兜底：`AGENT_MOCK=1` 能跑通 → 配置问题，不能跑通 → 代码问题。`llm.py` 对每个状态码都写了中文提示。

**6. 成本会不会失控** —— 离线成本为 0。在线记住三条：① Agent 循环每迭代一次都要把历史重发一遍，成本是**步数的函数**；② 第 13 章的 `CostGuard` 每一步都检查预算，`AGENT_BUDGET_CNY`（默认 0.5 元）可改；③ 调 prompt 先用小模型、短输入。

---

## 七、最后一句

**这里的代码是教学标本，不是让你直接在上面改的。** 想改造、想加功能、想接自己的业务，请**复制一份到别处再改**（或按第 16 章的脚手架生成自己的项目骨架）。三个理由：① 遇到问题时你还能回到原始版本对照；② 教学示例的注释与结构是刻意设计的，改乱了就失去参考价值；③ 每章代码是"最小可运行"的，你的项目要的是"最小可维护"的，取舍不同。

下一步：打开 `docs/第16章 毕业设计与面试.md`，写一份你自己的需求文档。
