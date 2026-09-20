---
title: ReAct是怎么让模型自己纠错的？50行写一个会思考、会选工具、会停下来的Agent
description: 工具循环明明能算"1234 乘 5678"，换成"找出目录里最大的 md 文件"就开始编文件名——模型缺的不是能力，是"中间状态"。本文从第 04 章的多轮工具调用讲起，用 50 行手写一个 ReAct 最小 Agent Loop，讲透显式 Thought 为什么能让模型自我纠正、文本格式为什么会一上线就碎、五种停止条件怎么拦住死循环，帮 0 基础同学写出一个能思考、会选工具、会自己停下来的 Agent。
keywords: ["ReAct", "ReAct是什么", "Agent Loop", "最小Agent实现", "ReAct和Function-Calling区别", "Thought Action Observation", "Agent死循环怎么停", "五种停止条件", "Function Calling", "AI Agent教程"]
tags: ["AI Agent", "ReAct", "大模型应用"]
---

# 第05章 ReAct 最小 Agent Loop

> **本章目标：** 用 50 行写出一个完整 Agent —— 它会思考、会选工具、会纠错、会自己停下来。
> **预计用时：** 8 小时　**前置章节：** 第04章 工具调用　**配套代码：** `code/ch05_react_loop.py`

---

## 0. 开篇：一个真实的问题

上周有个学员在群里贴了一段日志。他的 Agent 是照第 04 章写的，平时跑得挺顺：

> 他：1234 乘 5678 等于多少？
> 模型：[调用 calculator] → 7006652 → "1234 × 5678 = 7006652"

于是他信心满满地换了个任务：

> 他：看一下 sandbox/notes/ 目录，找里面最大的那个 md 文件，把它前 5 行读出来。
> 模型：我看了 sandbox/notes/，最大的是 react_notes.md，前 5 行是：
>       # ReAct 学习笔记
>       …

文件根本不存在——他打开目录看了一眼，最大的是 meeting.md。他又连着问了两遍，模型每次都换一个"不存在的文件名"，但格式、语气、字段全都挑不出毛病。

> 他：是不是我的工具写错了？temperature 我也设成 0 了，要不要换个更强的模型？
> 我：你的 calculator 一点问题没有。我先问你，模型第一轮决定"调哪个工具、参数填什么"的时候，它手里有什么信息？
> 他：……只有我那一句话。
> 我：那你让它找"最大的那个文件"，它凭什么知道哪个最大？
> 他：它应该先列目录吧。
> 我：对。但你的循环允许它"先列目录"吗？
> 他：……不允许。我让模型每一轮直接给出 Action。

这类"换个任务就崩"的故障很常见：**不是工具错了，是模型缺"中间状态"。** 它在第一轮既不知道目录里有哪些文件（要调 `list_files`），也不知道哪个最大（要比较大小），更不知道文件内容（要调 `read_file`）——信息不在手上，它就只能用最擅长的办法补上：**编**。换成更强的模型也一样。

人类做这件事的顺序不是这样的：先想"我得先看看有哪些文件"，去列目录；看到结果再想"里面最大的是哪个"，去读那个文件。**每一步的"想"都依赖上一步的"看到"。**

所以 ReAct 给模型加了一条**显式的思考通道**，并把这个通道的内容留在上下文里，让模型每一步都能看到自己之前想了什么、观察到了什么。模型从此不再是"一问一答"，而是**一个能根据中间结果调整策略的循环体** —— 这就是 Agent。本章用 50 行把这条循环实现出来，再补上真正上线要用的那一半：**怎么让它停下来。**

---

## 1. ReAct 是什么

### 1.1 论文里的三个词

ReAct 出自论文 *ReAct: Synergizing Reasoning and Acting in Language Models*（Yao 等，2022，arXiv:2210.03629，ICLR 2023）。名字是 **Reasoning（推理）+ Acting（行动）** 的缩写。

它的核心结构极其简单 —— **把"想"和"做"交替进行，并且都写进同一个文本流里**：

```text
Question: 除了苹果公司，还有哪家公司在 1990 年代同时做过手机和电脑？
Thought: 我需要先找出 1990 年代做手机的公司，再检查它们是否也做电脑。
Action: search[1990年代 手机制造商]
Observation: 诺基亚、摩托罗拉、爱立信……
Thought: 诺基亚在 1990 年代也做电脑吗？我记得它做过，需要确认。
Action: search[诺基亚 1990年代 电脑]
Observation: 诺基亚在 1990 年代生产过 MikroMikko 系列个人电脑。
Thought: 我找到了：诺基亚。可以给出答案了。
Action: Finish[诺基亚（Nokia），它在 1990 年代生产过 MikroMikko 系列个人电脑，同时也做手机。]
```

论文用这个模式在 HotpotQA（需要多跳检索的问答数据集）上显著超过了"只检索不推理"和"只推理不检索"的基线 —— 因为**推理让行动更有方向，观察让推理不再空转**。

### 1.2 它和"直接 Function Calling"的本质区别

第 04 章的循环也是多轮工具调用，看起来很像。区别在一个地方：**模型有没有一条显式的、留在上下文里的推理轨迹（Thought）。**

| | 第 04 章：直接 Function Calling | 本章：ReAct |
|---|---|---|
| 模型每次输出 | 直接给 tool_calls（参数填好） | 先写 `Thought`（要做什么、为什么），再给 Action |
| 上一轮的推理去哪了 | 模型内部，**你看不到，也留不下** | 明文写在消息里，成为下一轮的输入 |
| 上一步错了会怎样 | 模型可能重蹈覆辙（它看不到自己错在哪） | 它能在 Thought 里写"上次假设错了，因为观察结果是 X" |
| token 消耗 | 较低（少写思考） | 更高（每步多几十到几百 token） |
| 解析难度 | 协议保证结构，**几乎不会解析失败** | 靠文本格式约定，**会解析失败**（第 3 节详述） |
| 可调试性 | 只能看到"它调了什么" | 能看到"它为什么这么调" —— 排查问题快得多 |

一句话总结：**显式的 Thought 是模型能"自我纠正"的物理载体。** 没有它，模型只能靠内部隐状态兜住所有推理，一旦某步跑偏，后面就顺着错下去，你在日志里也看不到任何线索。

代价也要讲清楚：ReAct 每步多写一段思考，token 涨 20%–50%；而且它对模型是否遵守文本格式有强依赖，格式一崩你就得写解析器兜底。

### ⚠️ 常见坑

- **现象**：以为"用了 ReAct 就一定比 Function Calling 强"，换过去之后任务成功率反而下降。**原因**：ReAct 的优势在多步、需要探索的任务上；**单步任务加了思考只是多花钱**。**怎么改**：先用最简单的直接调工具，只有当任务需要 ≥2 步且步骤之间有依赖时，才上 ReAct。
- **现象**：Thought 里模型写的推理是对的，但 Action 还是选了错工具。**原因**：思考通道和执行通道是**两次独立的生成**，"想对了"不代表"做对了"。**怎么改**：别把 Thought 当保证。工具描述、参数约束该做的一样不能少 —— 它们是执行层的护栏。

---

## 2. 50 行实现一个完整 Agent

先写"教学版"。它的目的是让你**一眼看懂骨架**，所以故意不写容错 —— 那些放在第 3、4 节。

### 2.1 系统提示词：定义 Thought / Action 的格式

ReAct 的提示词必须做三件事：**列出工具（名字 + 参数格式）**、**规定输出格式**、**规定结束标志**。

```python
PROMPT = """你是一个会用工具的助手。可用工具：
- calculator(expression)：计算数学表达式，例如 calculator(1234*5678)
- get_current_time(timezone)：查询当前时间，timezone 取 local 或 UTC

请严格按下面的格式回答，每次只输出一轮：

Question: 用户的问题
Thought: 你的思考 —— 现在该做什么，为什么
Action: 工具名(参数)
（写完 Action 后立刻停止，等待系统返回 Observation）

当你已经能回答问题时，输出：
Thought: 我已经知道答案了
Action: Finish[最终答案]

规则：
1. 每次只输出一个 Action，不要一次写多步。
2. 不要自己编造 Observation，它由系统提供。
3. 不要输出除 Thought 和 Action 之外的其他内容。"""
```

**逐条解释这些约束为什么必须写：**

- **`可用工具` 列表必须给出参数格式**（`calculator(expression)` 而不是 `calculator`）。模型是照着这个示例填参数的，你写多细它填多准。
- **`每次只输出一轮`** 是最容易漏的一条。不写的话模型会一口气编出好几轮的 Thought/Action/Observation，**包括伪造 Observation** —— 它会把想象中的工具结果写进去，然后基于幻觉继续推理。
- **`不要自己编造 Observation`** 是防幻觉的关键，但**它只是提醒，靠不住**。真正可靠的做法是解析时只取最后一个 Action，并且**丢弃模型输出里任何自带的 Observation**（见 2.2）。
- **`Finish[最终答案]`** 是终止信号。没有它，循环只能靠"步数上限"结束，那意味着**每次运行都会跑满步数**，成本和延迟双输。

### 2.2 解析 Action 与循环

```python
import re
from common.llm import chat

ACTION_RE = re.compile(r"Action:\s*(\w+)\s*\((.*)\)\s*$", re.M | re.S)
TOOLS = {"calculator": calculator, "get_current_time": get_current_time}

def parse_action(text):
    """从模型输出里解析出 (工具名, 参数字符串)；解析失败返回 (None, None)。"""
    match = ACTION_RE.search(text)
    return (None, None) if not match else (match.group(1), match.group(2).strip())

def run(question, max_steps=6):
    scratchpad = "Question: %s\n" % question
    for step in range(1, max_steps + 1):
        output = chat(PROMPT + "\n\n" + scratchpad, temperature=0)
        scratchpad += output.strip() + "\n"
        name, arg = parse_action(output)

        if name is None:
            return "格式错误，模型没有给出合法的 Action。原始输出：\n" + output
        if name == "Finish":
            return arg                                   # ← 唯一的正常出口

        try:
            observation = TOOLS[name](arg)
        except KeyError:
            observation = "没有名为 %s 的工具。可用工具：%s" % (name, list(TOOLS))
        except Exception as exc:
            observation = "工具执行失败：%s: %s。请尝试其他方法。" % (type(exc).__name__, exc)

        scratchpad += "Observation: %s\n" % observation
        print("第 %d 步 | %s(%s) → %s" % (step, name, arg, str(observation)[:80]))

    return "已达到最大步数 %d，仍未得到答案。" % max_steps
```

解析部分有三个细节：`re.M` 让 `$` 匹配**行尾**而不是整段末尾（多行输出里才能定位到 Action 那一行）；工具名用 `\w+` 匹配，但**解析出来的名字必须查表确认存在**，绝不能直接当函数名调用；参数字符串要先剥引号，并且**先试 `json.loads`、失败再当裸字符串**。

### 2.3 完整代码

把上面三段拼起来就是完整实现 —— 提示词 + `parse_action` + 循环，落到 `code/ch05_react_loop.py` 的 `react_minimal()` 里，**一共 40 行左右**（不依赖任何 Agent 框架，工具执行走第 04 章的 `ToolRegistry.execute(name, arguments)`）。唯一的补充是传对参数名：calculator 传 `{"expression": arg}`、get_current_time 传 `{"timezone": arg}`。

离线跑一遍：

```bash
AGENT_MOCK=1 python code/ch05_react_loop.py
```
```text
【极简版】问题：帮我算一下 1234*5678，再告诉我现在几点
第 1 步 | calculator(1234*5678) → 7006652
第 2 步 | get_current_time(local) → 2026-09-20 15:27:03
第 3 步 | Finish(1234×5678 = 7006652；当前时间 2026-09-20 15:27:03)
最终答案：1234×5678 = 7006652；当前时间 2026-09-20 15:27:03
```

**注意 Mock 模式下发生了什么**：假模型按规则解析你的问题，一步步给出 Action；你的正则、工具执行、Observation 拼接、Finish 检测全都是真的在跑。这就是为什么**先离线跑通**这么重要 —— 流程有问题，你一眼就能看出来，不用怀疑是模型不听话。

### ⚠️ 常见坑

- **现象**：模型在 Thought 里写"我需要先调用 calculator"，然后就结束了，没有 Action 行。**原因**：提示词里的格式示例不够强，或者模型（尤其是小模型）把 Thought 当成了最终输出。**怎么改**：在提示词里补一条"Thought 之后必须紧跟一行 Action，缺一不可"；更稳的办法是给一个完整的 few-shot 示例（一轮输入 + 一轮输出）。
- **现象**：`Action: Finish[答案]` 被解析成工具名 `Finish`、参数 `答案`，代码直接返回了它 —— 但答案里的 `]` 和 `)` 混在一起，解析出来的字符串少了一截。**原因**：`(.*)\)$` 是贪婪匹配，且 `Finish[...]` 用的是方括号不是圆括号。**怎么改**：为 Finish 单独写一条正则 `r"Action:\s*Finish\[(.*)\]\s*$"`，先匹配它再匹配普通工具。
- **现象**：模型输出 `Action: calculator(1+1)`，但 `TOOLS[name]` 抛 `KeyError`。**原因**：模型把工具名大小写写错、或在名字后面加了括号。**怎么改**：查表前先 `name.strip().lower()`，并且用 `TOOLS.get(name)`，为 `None` 时把"可用工具列表"喂回去让它重选 —— **这本身就是一次失败恢复**。
- **现象**：`temperature` 没设成 0，同一个问题跑三次得到三种不同的 Action 格式。**原因**：采样温度高，格式随机性变大。**怎么改**：Agent 循环里**一律 `temperature=0`**（或接近 0）。Agent 要的是稳定可复现，不是创意。

---

## 3. 为什么这个版本不够好

上面的代码在演示里跑得通，但**一上线就碎**。原因只有一个：**你在用正则解析自然语言**。而自然语言是模型生成的，它不保证任何格式。

### 3.1 五个真实故障

| # | 模型实际输出 | 你的正则 | 结果 |
|---|---|---|---|
| 1 | `**Action:** calculator(1+1)` | 找 `Action:\s*` | 匹配失败（多了 `**` 和冒号位置不同） |
| 2 | 输出被包在 markdown 代码块里（Action 那行前后各有一行三反引号） | `$` 行尾锚定 | 匹配失败，或者把反引号那一行当成了 Action |
| 3 | `Action: search_notes({'keyword': 'ReAct'})` | 把参数当字符串 | 参数变成 `{'keyword': 'ReAct'}`，`json.loads` 失败 |
| 4 | `Action: calculator(1 +\n  1)` | `.*` 跨行 | 依赖 `re.S` 才匹配，否则失败 |
| 5 | `我认为应该用 Action: get_current_time` 后面又写了一段解释 | 贪婪/多匹配 | 取到哪一段取决于正则，行为不可预测 |

于是你开始打补丁：先把 `**` 去掉，再把代码块剥掉，再处理单引号……等你处理完第七种情况，你的代码里已经有了**一个 200 行的半吊子自然语言解析器**。而模型下一次更新，格式又变了。

**根本原因：你在用"文本约定"代替"协议"。** 格式约定是**建议**，模型可以遵守也可以不遵守；协议是**结构**，不遵守就根本无法提交。

### 3.2 正确的做法：用原生 Function Calling 做 ReAct

好消息是：第 04 章的 Function Calling 已经提供了**结构化协议**（`tool_calls` 字段是解析好的，参数按 schema 保证）。我们只需要在它之上把 **Thought 找回来**。

做法很简单：**让 `content` 承载 Thought，让 `tool_calls` 承载 Action。**

```python
REACT_SYSTEM = """你是一个会用工具的助手。规则：

1. 每一轮先用一两句话写清楚你的思考：现在要做什么、为什么这么做、有没有别的可能。
2. 如果需要使用工具，紧接着调用它（可以一次调用多个）。
3. 如果信息已经足够，直接用中文给出最终答案，不要再调用工具。
4. 不要在思考里编造工具结果 —— 你没有执行任何工具，结果由系统返回给你。
5. 如果上一次工具调用失败了，先判断原因：是可以改参数重试，还是应该换个方法。
"""
```

解析端因此变得极其干净：

```python
reply = llm.chat(messages, tools=registry.to_openai_schema())
thought = (reply.get("content") or "").strip()        # ← Thought 有了，而且不会解析失败
tool_calls = reply.get("tool_calls") or []            # ← Action 是结构化的，不用正则
if not tool_calls:
    return reply["content"]                           # ← 等价于 Finish[...]
```

**对比一下前后：**

| | 文本 ReAct | 原生 FC + Thought |
|---|---|---|
| Action 解析 | 正则，随时可能失败 | 协议字段，不可能失败 |
| 参数合法性 | 靠猜 | schema 约束 + 代码校验 |
| 多工具并行 | 要自己设计语法 | 协议原生支持 |
| 换模型 | 格式一换就崩 | 只要支持 FC 就不用改 |
| Thought | 强制的（写不出就没有 Action） | 靠提示词引导，**模型可能不写** |

最后一行是唯一的损失：`content` 是模型自愿写的，它可能偷懒不写思考。**这是可以接受的取舍** —— 用"偶尔没有思考"换"永远不会解析失败"，非常划算。想提高思考率，可以在系统提示词里加一句"每次调用工具前必须先说明理由"，并在 trace 里统计 `content` 为空的比例（第 12 章会讲怎么把这种指标纳入评测）。

> 历史视角：文本 ReAct 在 2022 年提出来时，Function Calling 这个协议还不存在，所以论文只能约定文本格式。**今天你实现 ReAct，应该直接用 FC 做执行层，只在提示词层面保留 Thought。** 论文的思想是核心，文本格式只是当年的实现手段。

### ⚠️ 常见坑

- **现象**：改用 FC 版本后，模型几乎不再输出 `content`，Thought 全丢了。**原因**：很多模型在决定调工具时会把全部注意力放在 `tool_calls` 上。**怎么改**：在系统提示词里给出**具体的句子模板**（"因为 A，我需要调用 B 来获取 C"）而不是抽象要求；或者在 user 消息里追加一句"先用一句话说明你的计划，再调用工具"。
- **现象**：文本版和 FC 版混用，`messages` 里同时出现"文本里的 Observation"和 role=tool 的消息，模型开始混乱、重复动作。**原因**：两种模式的消息结构不兼容。**怎么改**：**一个循环里只用一种**。迁移时把 scratchpad 换成真实的 `messages` 列表，不要把老的正则拼接残留下来。
- **现象**：`reply["content"]` 里模型写了"我看到文件内容是 X"（它没读文件，是编的），然后才调用工具。**原因**：这是 ReAct 提示词的经典副作用 —— 论文原格式要求它写 Observation，模型忍不住开始脑补。**怎么改**：系统提示词明确写"你没有执行任何工具，**不要**写 Observation；结果由系统以 tool 消息返回"。并且在解析时**忽略** model 输出里任何形如 `Observation:` 的文本（FC 版里它们只出现在 content 里，不会被误当真实结果 —— 这也是 FC 版更安全的一个理由）。

---

## 4. 停止条件（本章重点）

0 基础的同学写 Agent，**十个里有九个会写出死循环**。这一节把"怎么停"讲透。

### 4.1 死循环是怎么发生的

真实日志（一个忘了加停止条件的 Agent）：

```text
第 3 步 | read_file(sandbox/a.txt) → {"ok": false, "error": "文件不存在"}
第 4 步 | read_file(sandbox/a.txt) → {"ok": false, "error": "文件不存在"}
第 5 步 | read_file(sandbox/a.txt) → {"ok": false, "error": "文件不存在"}
...（一直跑到 API 余额耗尽，或你手动 Ctrl+C）
```

模型不是"故意"卡住的。它在 Thought 里可能写着"文件不存在，我需要换一个路径"，但下一轮它又填了同一个参数 —— 因为**没有任何机制告诉它"这一步你已经做过了、而且没成功"**。循环体本身是无状态的，状态只有你帮它维护。

死循环的三个典型形态：**同动作重复**（参数一模一样）、**换了动作但结果相同**（换工具不换思路）、**空转**（一直在 Thought 里分析，从不产生 Action）。

### 4.2 五种停止条件

```python
class StopReason:
    FINISH = "finish"                 # 模型给了最终答案（唯一正常出口）
    MAX_STEPS = "max_steps"           # 步数上限
    REPEAT_ACTION = "repeat_action"   # 同一动作重复
    NO_PROGRESS = "no_progress"       # 观察结果连续无变化
    BUDGET = "budget"                 # 成本上限
    TIMEOUT = "timeout"               # 墙上时钟超时
    FORMAT_ERROR = "format_error"     # 连续解析失败
```

**① 最大步数（必须有，第一道防线）。** 推荐值 **8–12**：太小（3–5）会让合法的多步任务被腰斩；太大等于没有（12 步的 token 账单已经足够劝退）。先设 8，看你实际用了几步再调。

**② 重复动作检测（性价比最高的一条）。** 把工具名和参数压成一个签名来计数，参数要 `sort_keys` 排序（避免键序不同导致漏判）。**关键设计：第 2 次不是终止，而是"警告 + 喂回"** —— 因为存在合法重试（第一次超时、第二次成功）；第 3 次还没变化，才判定卡死。

**③ 无进展检测（拦住"换工具但不换思路"）。** 不能只看"观察相同"就停 —— 轮询类任务确实需要连续几次相同观察，所以阈值取 3，并且与重复动作**分开统计**（一个管同样的动作，一个管同样的结果）。

**④ 成本上限。** `estimate_cost` 来自 `common/llm.py`。**注意 usage 在 Mock 模式下也要有值**，否则这条在离线调试时形同虚设。

**⑤ 墙上时钟超时（防挂死）。** 防的是**单步特别慢**（网络慢、工具卡住）而不是步数多。它和工具层的 `run_with_timeout`（第 04 章 6.3）是两层防护：一个管单次调用，一个管整个任务。

五条集中写在循环里，总共不到 30 行：

```python
from collections import Counter
import json, time

action_counter, same_count, last_observation, spent = Counter(), 0, None, 0.0
start = time.time()

for step in range(1, max_steps + 1):
    if time.time() - start > max_seconds:                 # ⑤ 超时（例如 120 秒）
        return stop(StopReason.TIMEOUT, "运行超过 %d 秒" % max_seconds)

    reply = llm.chat(messages, tools=schemas)
    usage = reply.get("usage") or {}
    spent += estimate_cost(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    if spent > max_cost:                                  # ④ 成本（例如单任务 0.5 元）
        return stop(StopReason.BUDGET, "已花费 %.4f 元，达到上限 %.2f 元" % (spent, max_cost))

    name, args = pick_first_tool_call(reply)
    sig = "%s|%s" % (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
    action_counter[sig] += 1                              # ② 重复动作：先警告，再终止
    if action_counter[sig] >= 2:
        observation = "你已经用完全相同的参数调用过 %s 并且失败了，请换参数或换工具。" % name
    if action_counter[sig] >= 3:
        return stop(StopReason.REPEAT_ACTION, "工具 %s 被重复调用 %d 次" % (name, action_counter[sig]))

    if observation == last_observation:                   # ③ 无进展：观察连续相同
        same_count += 1
    else:
        same_count, last_observation = 0, observation
    if same_count >= 3:
        return stop(StopReason.NO_PROGRESS, "连续 %d 次观察结果完全相同" % same_count)
else:
    return stop(StopReason.MAX_STEPS, "已达到最大步数 %d" % max_steps)   # ① 兜底
```

### 4.3 该选哪个组合

| 条件 | 拦住的故障 | 误杀风险 | 推荐默认 |
|---|---|---|---|
| 最大步数 | 所有失控（兜底） | 低 | **8**（调节奏用） |
| 重复动作 | 卡在同一个调用 | 中（会拦住合法重试） | **同签名满 3 次** |
| 无进展 | 换汤不换药 | 中（轮询类任务会误伤） | **观察连续相同 3 次** |
| 成本上限 | 烧钱 | 低 | **单任务 ¥0.5** |
| 超时 | 挂死 | 低 | **120 秒** |

**五个全上，成本极低（每步不到 10 行代码），收益是"你的 Agent 永远不会无限跑下去"。** 这是本章最值得抄进自己项目的一段代码。

还有一个不该忘的**软性**停止条件：连续 N 次没有产生任何工具调用、也没有给出答案（模型在 Thought 里空转）。处理方式：把"你必须在本轮给出 Action 或最终答案"作为 user 消息追加进去提醒它一次，再犯就按 `FORMAT_ERROR` 终止。

### ⚠️ 常见坑

- **现象**：Agent 在你的机器上跑 3 步就结束了，答案是"已达到最大步数"。**原因**：`max_steps` 设成了 3，而任务第一步就要列目录、第二步读文件、第三步才能回答。**怎么改**：把步数上限设成 8，**并且把 `step/stop_reason` 打出来**。看到"停在 max_steps"就知道是该调大还是该修提示词。
- **现象**：加了重复动作检测后，一个"重试两次就成功"的任务被判死亡。**原因**：第 2 次就直接终止了。**怎么改**：改成"第 2 次警告并喂回提示，第 3 次才终止"（如上面的代码）。**永远给一次改的机会。**
- **现象**：`same_count` 一直在 0 和 1 之间跳，无进展检测从没生效。**原因**：观察文本里带了时间戳或随机 id，每次都不一样。**怎么改**：比较前先做归一化（去掉时间戳/随机值），或者只比较观察结果的**结构化核心字段**（如 `data.items` 的长度和内容）。
- **现象**：Mock 模式下成本上限从不触发，因为 `usage` 是空的，`spent` 永远是 0。**原因**：假模型没返回 usage。**怎么改**：Mock 实现里补上 `usage`（按字符数估算即可），并在代码里对 `usage` 缺失做兜底：用 `count_tokens_approx` 自己算。

---

## 5. 失败恢复：工具挂了 Agent 应该怎么办

停止条件解决"什么时候停"，失败恢复解决"停之前还能不能救一下"。

### 5.1 先分清错误能不能救

| 错误类型 | 例子 | 可重试？ | 策略 |
|---|---|---|---|
| 参数错误 | 缺参数、类型不对、JSON 不合法 | **可以** | 直接把错误喂回模型，让它改正重发（第 04 章 6.1） |
| 临时性故障 | 429 限流、网络抖动、5xx | **可以** | 退避重试（1s → 2s），最多 2 次，**同样的参数** |
| 资源不存在 | 404、文件不存在、目录为空 | 不可以（同参数重试无意义） | 换路径 / 换工具，把"可用的选项"告诉模型 |
| 权限不足 | 403、路径不在白名单 | 不可以 | **放弃并说明原因**，不要反复撞墙 |
| 超时 | 工具跑了 10 秒没结果 | 视情况 | 缩小范围重试一次；再超时放弃 |
| 结果超长被截断 | 观察到 `[内容已截断]` | 可以（换更精确的查询） | 提示模型"用更精确的参数缩小范围" |

**这张表的核心是"可重试"这一列。** 用同一套逻辑去处理两类错误，要么白等（对 404 重试十次），要么过早放弃（429 直接失败）。

### 5.2 错误信息要写成"说明书"

失败恢复的效果，**一大半取决于你喂回去的那句话**。对比：

```text
# ❌ 模型看到后不知道干什么
"error": "KeyError: 'path'"
"error": "Error"
"error": "失败"

# ✅ 模型知道下一步怎么做
"参数缺失：path。path（string）：文件路径，必须以 sandbox/ 开头，例如 sandbox/notes/a.md"
"工具 read_file 不存在。可用工具：calculator、list_files、read_file、get_current_time"
"文件 sandbox/a.txt 不存在。当前目录下有：a.md、b.txt、notes/。请从这些里选一个。"
"请求被限流（429），请等待 2 秒后重试。如果连续 3 次失败，请改用其他方式获取该信息。"
```

三条写作规则：**说清是什么错 → 给出可用的替代项 → 明确告诉它下一步该做什么（或不要做什么）。**

### 5.3 一个完整的恢复分支

```python
RETRYABLE = ("超时", "限流", "429", "timeout", "ConnectionError")

def call_tool_with_recovery(name, args, attempt_state):
    """执行工具；对可重试错误做退避重试，对不可重试错误返回带行动建议的文本。"""
    for attempt in range(2):
        result = registry.execute(name, args)         # 返回 JSON 字符串
        payload = json.loads(result)
        if payload.get("ok"):
            return payload

        error = payload.get("error") or ""
        if any(k in error for k in RETRYABLE) and attempt == 0:
            time.sleep(1.5)                           # 简单退避；生产环境用指数退避
            continue
        payload["error"] += "（本次已尝试 %d 次，请不要再用相同参数重试。）" % (attempt + 1)
        return payload
    return payload
```

还有一个**比"重试"更重要"的动作：放弃时要给出解释**。

```python
if step == max_steps:
    summary = "我没能完成任务。原因：%s。我已经尝试过：%s。建议：%s" % (
        last_error,
        "、".join("%s(%s)" % (c["name"], json.dumps(c["arguments"], ensure_ascii=False))
                  for c in attempted[-3:]),
        "检查 sandbox/ 目录下是否存在该文件",
    )
```

**这段"放弃说明"不是给人看的，是给用户看的、但由模型组织语言。** 一个只会说"任务失败"的 Agent，和一个能说"我试了 3 次读取该文件都报不存在，目录里实际有 a.md / b.txt，你要找的是不是 a.md？"的 Agent，产品体验是两个物种。这也是第 06 章 Reflection（反思）的天然切入点。

### ⚠️ 常见坑

- **现象**：Agent 在文件不存在时反复重试 5 次，每次都失败。**原因**：把"资源不存在"也当成了可重试错误。**怎么改**：按 5.1 的表分类。判断方式很直接：**换个参数重试有没有可能成功？** 没有，就别重试。
- **现象**：退避重试卡住了 30 秒，用户以为程序死了。**原因**：`time.sleep` 太长且没有输出。**怎么改**：每次重试都打印一行日志（`第 2 次重试，等待 1.5 秒……`）；重试总时长要计入第 4 节的时间预算。
- **现象**：工具失败了，Agent 却继续基于"空结果"往下推理，最后给了一个错误答案。**原因**：代码把失败转成了空字符串 `""` 而不是错误标记。**怎么改**：**永远不要用空值表示失败**。统一 `{"ok": false, "error": "..."}`，并在系统提示词里写明"看到 ok 为 false 时不要基于该结果作答"。

---

## 6. 调试：Agent 不动了怎么办

### 6.1 第一件事：把每一步的 messages 打出来

99% 的"Agent 不动了"，答案就在 messages 里。极简版打印的是一行摘要，**真正的调试要打印完整结构**：

```python
def dump_messages(messages, step=None):
    print("=" * 60)
    print("Step %s | 共 %d 条消息" % (step, len(messages)))
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = (msg.get("content") or "").replace("\n", "\\n")
        if role == "assistant" and msg.get("tool_calls"):
            calls = ", ".join("%s(%s)" % (c["name"], json.dumps(c["arguments"], ensure_ascii=False))
                              for c in msg["tool_calls"])
            print("  [%d] assistant: %s | tool_calls: %s" % (i, content[:100], calls))
        elif role == "tool":
            print("  [%d] tool(%s): %s" % (i, msg.get("tool_call_id"), content[:120]))
        else:
            print("  [%d] %s: %s" % (i, role, content[:120]))
```

建议把这段代码做成开关（`--trace` 参数或 `AGENT_TRACE=1` 环境变量），平时关掉，出问题打开。

### 6.2 六个典型症状 → 病因

| 症状 | 最可能的原因 | 怎么验证 / 怎么改 |
|---|---|---|
| 第 1 步就不输出 Action | 系统提示词的格式约束与任务冲突；模型不支持该格式 | 打印模型**原始输出**（`repr`），看它到底写了什么 |
| 一直在 Thought 里分析，从不调工具 | 提示词没说清"必须调用工具或给答案" | 补一条硬规则 + 一个 few-shot 示例 |
| 总是选同一个工具（哪怕它明显不合适） | 那个工具的 `description` 写得太"宽"，其他工具太弱 | 检查 `registry.to_openai_schema()` 的实际内容，收窄描述 |
| 参数总是空 `{}` | 参数没有 `description`，模型不知道填什么 | 给每个参数补描述 + 示例值 |
| 输出格式对，正则就是匹配不上 | 模型加了 `**加粗**`、代码块或中文标点 | `print(repr(output))`，看清真实字符 |
| 第 2 步就结束并说"信息够了" | 模型产生了幻觉，以为自己知道答案 | 在系统提示词里写明"涉及文件内容和精确计算时，必须先获取真实数据" |

**排查顺序永远是：先看原始输出 → 再看工具 schema → 最后才怀疑模型。** 新手 90% 的时间花在"换个模型试试"，而有经验的工程师 90% 的时间花在"看 messages 和 schema"。

### 6.3 trace 三件套

`common/trace.py` 提供的 `Trace` 类，把每一步记成一条结构化事件：

```python
from common.trace import Trace

trace = Trace("ch05_react")
trace.log("step", step=step, thought=thought[:200], action=name, args=args,
          observation=str(observation)[:200],
          prompt_tokens=usage.get("prompt_tokens", 0),
          completion_tokens=usage.get("completion_tokens", 0),
          elapsed_ms=int((time.time() - step_start) * 1000))
...
print(trace.summary())        # 一句话总结：几步、几次工具、多少 token、为什么停
trace.save("trace_ch05.json") # 存盘，用于事后复盘和对比不同版本
```

**每次运行都存一份 trace，是你从"能跑通"走向"能优化"的分水岭。** 没有 trace，你只能凭感觉调提示词；有了 trace，你能回答"改完之后平均步数从 5.2 降到 3.8"这种问题。第 12 章会把 trace 变成评测体系。

### ⚠️ 常见坑

- **现象**：`print(messages)` 输出一大坨、什么都看不出来。**原因**：没做截断和结构化打印。**怎么改**：用 6.1 的 `dump_messages`，只打前 100–120 字符 + 工具调用的结构化摘要。
- **现象**：`KeyError: 'tool_calls'`，在某些模型上不报，在某些模型上报。**原因**：不同服务商对空 `tool_calls` 的处理不一致（有的返回 `None`，有的直接不返回这个字段）。**怎么改**：一律用 `reply.get("tool_calls") or []`，**永远不要假设字段存在**。
- **现象**：把 `messages` 里的中文 `content` 打印成乱码。**原因**：Windows 控制台默认编码不是 UTF-8。**怎么改**：跑之前 `set PYTHONIOENCODING=utf-8`（Windows）或 `export PYTHONIOENCODING=utf-8`；脚本内也可以 `sys.stdout.reconfigure(encoding="utf-8")`（Python 3.7+）。

---

## 7. 成本与延迟的直观感受

学完原理，必须对"这玩意多贵、多慢"有直觉。**用一个 5 步 Agent 的真实数据算一遍。**

假设：系统提示词 600 token，每一步模型输出（Thought + Action）约 80 token，每次观察结果约 100 token。Agent 走了 5 步工具调用 + 1 次收尾。

| 第几次请求 | 本次 prompt 大小 | 本次输出 |
|---|---|---|
| 1（决定第 1 个动作） | 600 + 180 = 780 | 80 |
| 2 | 600 + 360 = 960 | 80 |
| 3 | 600 + 540 = 1140 | 80 |
| 4 | 600 + 720 = 1320 | 80 |
| 5 | 600 + 900 = 1500 | 80 |
| 6（收尾回答） | 600 + 1080 = 1680 | 150 |
| **合计** | **7380 prompt** | **630 completion** |

**一个 5 步 Agent，发了 6 次请求，消耗约 8000 token。** 同样的问题如果一次答上来，是 `600 + 150 + 180 ≈ 930` token —— **8 倍左右**。两个必须记住的结论：

1. **prompt 随步数线性增长**：第 n 次请求的 prompt ≈ 600 + 180n，而 Total = Σ(600+180k) 是**平方级**的。步数从 5 涨到 10，token 不是翻倍，而是涨到约 3 倍（约 2.5 万 token）。
2. **重复率极高**：6 次请求里约 80% 的 prompt token 是**把历史重复发了一遍**。这不是浪费 —— 模型没有记忆，你不发它就不知道。但 Agent 成本结构里，"上下文重发"就是大头。

延迟同理：单次请求 1.2–2.5 秒，6 次串行 **9–15 秒**，用户盯着屏幕等的体验已经很差了。

**四条优化方向**：**减少步数**（收益最大，工具描述写清楚、提示词里给"优先用哪个工具"的建议，5 步压到 3 步）；**裁剪历史**（只留最近 N 轮，更早的压成一句摘要，第 07 章）；**截断观察结果**（工具层限长，第 04 章 6.4，直接砍掉大头）；**分级模型 + 缓存**（动作选择用便宜模型、总结用强模型，重复子问题缓存结果）。

> 一个实用的自检习惯：**在 Agent 结束那一行永远打印 `steps / total_tokens / cost / elapsed`。** 你会很快发现某些"感觉很强"的 Agent 每次要花两毛钱、跑 20 秒 —— 这些数字会反过来重塑你设计工具和提示词的方式。

### ⚠️ 常见坑

- **现象**：调试时用最贵的模型跑 Agent，一天花了几十块。**原因**：Agent 循环会放大消耗（每次迭代重发历史），贵模型 × 6 次请求 = 灾难。**怎么改**：**调流程用小模型，最终验证再用大模型**；并一定接上第 4 节的成本上限。
- **现象**：`estimate_cost` 算出来的钱和账单对不上。**原因**：不同服务商的计价单位不同（有的按字符、有的按 token，缓存命中的价格也不同）。**怎么改**：`estimate_cost` 是**估算工具**，用来做相对比较和上限保护，不要当对账依据；真要对账以服务商后台为准。
- **现象**：Agent 从 5 步降到 4 步，但总 token 反而涨了。**原因**：某一步的观察结果特别长（比如把整个文件读进来了）。**怎么改**：优化时看的是 **total prompt tokens**，不是步数。步数只是间接指标。

---

## 8. 配套代码导读：`code/ch05_react_loop.py`

```text
第 1 部分  react_minimal()   50 行以内的教学版：PROMPT（Thought/Action 格式约束）
          + parse_action()（正则解析，含 Finish 特判）+ 循环。只做一件事：把循环跑起来给你看
第 2 部分  ReActAgent 类     增强版（原生 Function Calling 做 ReAct）：REACT_SYSTEM_PROMPT、
          五种停止条件（第 4 节）、失败恢复（第 5 节）、dump_messages()（第 6 节）、Trace 每步落盘
第 3 部分  三个演示任务       A 单工具（计算）→ 观察最简循环；B 多工具串联（列目录 → 挑最大
          文件 → 读前 5 行）→ 观察 ReAct 的价值；C 故意注入失败 → 观察停止条件与失败恢复
```

```bash
AGENT_MOCK=1 python code/ch05_react_loop.py            # 离线跑通三个任务
AGENT_MOCK=1 python code/ch05_react_loop.py --task C   # 只跑失败恢复那个
python code/ch05_react_loop.py --trace                 # 在线 + 打印完整 messages
```

看输出时重点观察三件事：**① 每一轮 Thought 说了什么；② 观察结果变了之后，下一步的策略有没有跟着变；③ 结束时是哪个停止条件触发的、花了多少 token。** 第 ② 点是判断"这到底是不是一个 Agent"的核心标准。

---

## 本章小结

1. **ReAct = Reasoning + Acting**，思想是让模型每一步先想再做，并把"想"的内容留在上下文里 —— 这是它能自我纠正的物理载体。
2. **50 行就能实现一个完整 Agent**：格式约束的系统提示词 + 正则解析 + 观察回填 + `Finish` 终止。
3. **文本格式的 ReAct 一上线就碎**（加粗、代码块、单引号 JSON……），因为它用"文本约定"代替了"协议"。**正确做法是用原生 Function Calling 承载 Action，用 `content` 承载 Thought。**
4. **五种停止条件全上**：最大步数、重复动作、无进展、成本上限、超时。加上它们只需要几十行代码，收益是"永远不会无限跑下去"。
5. **失败恢复的前提是分清"能不能救"**：可重试的退避重试，不可重试的换参数或放弃。错误信息要写成"说明书"：是什么错 → 有哪些替代项 → 下一步做什么。
6. **Agent 不动了，先看 messages 和工具 schema，最后才怀疑模型。**
7. **一个 5 步 Agent ≈ 6 次请求、8000 token、10 秒以上**，其中约 80% 的 prompt 是重复发送的历史。步数增加时成本是平方级增长。

关联阅读：[[02-Wiki/专题总结/01-Agent是什么]]、[[02-Wiki/专题总结/03-工具调用与Function-Calling]]

---

## 动手练习

**练习 1（照着改一行）**
把 `react_minimal()` 的 `max_steps` 从 6 改成 2，跑任务 B（列目录 → 挑最大文件 → 读前 5 行）。观察：它在第几步被截断？返回的 `stop_reason` 是什么？然后再改成 12 跑一次，记录实际用了几步。
**交付**：两次运行日志 + 你的 `max_steps` 建议值及理由。

**练习 2（加一个停止条件）**
实现"空转检测"：如果连续 2 轮模型都没有产生 `tool_calls`、也没有给出非空 `content`（或 content 全是"我需要更多信息"这类空话），就追加一条 user 消息提醒它"本轮必须给出 Action 或最终答案"；再空转一轮就按 `format_error` 终止。
**交付**：代码 + 一个能稳定复现空转的构造用例（提示词里故意制造歧义）+ 两次运行日志。

**练习 3（自己实现一个新功能）**
把极简版（文本 ReAct）改造成"可切换"的版本：加一个 `use_native_fc=True/False` 开关，两条路径共用同一套工具和同一套停止条件。在任务 B 上各跑 5 次，记录：成功率、平均步数、总 token、是否出现过解析失败。
**交付**：改造后的代码 + 一张 10 行的对比表 + 200 字结论（"在你的测试里哪种更好？为什么？"）。

---

## 自测题

**Q1. ReAct 和"直接用 Function Calling 多轮调用工具"最本质的区别是什么？**

<details><summary>点击查看答案</summary>

区别在于**有没有一条显式的、留在上下文里的推理轨迹（Thought）**。FC 也能多轮，但模型的推理发生在它内部、不留在消息历史里，下一步看不到"自己上次为什么这么选"，也就难以发现自己跑偏。ReAct 把推理明文写进上下文，模型能在下一步读到它并纠正。代价是多花 token，且文本格式需要解析（所以今天的实现应该用 FC 承载 Action、用 content 承载 Thought）。

</details>

**Q2. 为什么"最大步数"是必须有的一条，哪怕你已经写了重复动作检测？**

<details><summary>点击查看答案</summary>

因为重复动作检测只能拦住"同样的动作"，拦不住**每种动作都换一遍、但没有任何一种成功**的情况（换了 8 个工具、每个都失败一次，签名全不重复）。最大步数是**唯一与任务形态无关的兜底**：无论模型怎么折腾，步数到了就停。工程上所有循环都必须有一个与业务逻辑无关的硬上界。

</details>

**Q3. 重复动作检测为什么推荐"第 2 次警告、第 3 次终止"，而不是第 2 次就终止？**

<details><summary>点击查看答案</summary>

因为**存在合法的重试场景**：第一次调用因为网络抖动或超时失败了，第二次完全相同的调用可能就成功了。如果第 2 次就终止，你会误杀这类任务。做法是第 2 次时把"你已经用相同参数失败过一次"作为警告喂回模型，给它一次自己改主意的机会；第 3 次依然没变，才判定卡死。**先警告、再终止**是所有停止条件设计的通用模式。

</details>

**Q4. 工具返回了"文件不存在"，Agent 应该重试吗？换成"429 限流"呢？**

<details><summary>点击查看答案</summary>

"文件不存在"**不该用相同参数重试**（重试一百次它也不存在），应该换路径或换工具，并且要把"目录里实际有哪些文件"告诉模型；"429 限流"**可以重试**，退避等待（1s → 2s）后重发同样的参数，通常第二次就成功。判断标准就一句话：**换个时间点重试，有没有可能成功？** 有则重试，没有则换路。

</details>

**Q5. 一个 5 步 Agent 大约发几次请求、花多少 token？为什么说它的成本是"平方级"增长的？**

<details><summary>点击查看答案</summary>

5 步工具调用 + 1 次收尾 = **约 6 次请求**。以"系统提示词 600 token、每步新增约 180 token"估算，总 prompt token ≈ 780+960+1140+1320+1500+1680 ≈ **7380**，加输出约 630，合计约 8000 token。之所以说平方级：第 n 次请求的 prompt ≈ 600+180n，Total 是 Σ(600+180k)，对 n 是二次函数 —— 步数从 5 涨到 10，token 大约涨到 3 倍。**这就是为什么"减少步数"是成本优化里收益最大的一招。**

</details>

---

## 本章产出 / 交付标准

- [ ] `code/ch05_react_loop.py` 能跑，含 **50 行以内的极简版**（文本 ReAct + 正则）与**增强版**（原生 FC + 停止条件 + trace）；
- [ ] 五种停止条件全部实现，并且能各自触发一次（构造用例让 `stop_reason` 分别打印出来）；
- [ ] 一个失败恢复分支：可重试错误退避重试、不可重试错误给出替代方案；
- [ ] 一份 trace 文件（`trace_ch05.json`），能说清每一步的 thought / action / observation / token；
- [ ] 一次成本记录：任务 B 的步数、total token、耗时、估算费用。

**验收标准**：能在白板上画出 Thought → Action → Observation 循环并讲清"为什么显式思考能让模型自我纠正"；能解释五种停止条件各自拦住什么故障；能说出为什么现在的 ReAct 实现应该用 Function Calling 而不是正则。

---

## 结语与下一章

ReAct 给模型装上的不是"更聪明的脑子"，而是一条**回声走廊**：它每走一步都要喊一声"我现在在做什么、为什么这么做"，这声喊留在走廊里，下一步回头就能听见。所以真正让它能自我纠正的，不是"思考"这个动作，而是思考被留在了上下文里、能被下一步读到。

但……这条走廊只保证"每一步都看得见"，不保证"每一步都走得对"。你很快会发现另一件尴尬的事：**换个任务，它就不灵了。** 简单的事实问答，套 ReAct 纯属浪费 token；步骤特别多、依赖又很弱的任务（比如"调研 5 个方向各写一段"），走一步看一步的方式会反复绕路；还有些任务它每轮都在"反思"，结果越改越自信却没变好。

下一章是第 06 章《范式进阶》，讲三种进阶范式：**CoT**（先想再答，什么时候有用什么时候有害）、**Plan-and-Execute**（先出计划再执行，什么时候比 ReAct 好、计划错了怎么办）、**Reflection**（让模型批判自己，以及那个致命陷阱：没有外部信号时它会越改越自信、但并没变好）。最后给一张决策表，告诉你拿到一个任务该选哪个范式。
