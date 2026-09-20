---
title: 模型输出JSON却解析崩了？Prompt、JSON模式与代码校验三件套
description: 模型明明返回了 JSON，脚本为什么还是 JSONDecodeError？本文从"从客户邮件里抽取订单信息"这个功能讲起，拆解六段式 Prompt、JSON mode 的能力边界，以及必须自己写的那层校验与重试，帮上线后被一条坏 JSON 叫起来过的同学把异常挡在代码里。
keywords: ["模型输出JSON解析失败", "Prompt工程", "JSON mode", "大模型结构化输出", "safe_parse_json", "大模型输出校验与重试"]
tags: ["Prompt工程", "结构化输出", "大模型应用"]
---

# 第03章 Prompt工程与结构化输出

> **本章目标：** 让模型稳定输出程序能解析的 JSON，并用代码校验和重试兜住失败。
> **预计用时：** 3 小时　**前置章节：** 第02章　**配套代码：** `code/ch03_prompt_structured.py`

## 0. 开篇：一个真实的问题

上线第三天，凌晨一点半，你被电话叫醒："抽订单号那个功能全挂了，日志里全是 JSON 解析错误。"

早上到公司，你翻出日志问同事：

> **你：** 这几行本地跑都能过，为什么一上线就炸？
>
> **同事：** 你一批抽多少封？
>
> **你：** 三百封。
>
> **同事：** 那"偶尔"就是必然 —— 一百次里崩一次，三百封跑下来必崩。把报错贴出来看看。

出问题的就是你那个"从客户邮件里抽取订单信息"的功能。Prompt 写得很朴素，前半段看着完全正常：

```python
answer = chat("帮我从这封邮件里提取订单信息：" + email_text, temperature=0)
print(answer)      # 看着挺好：好的，我从邮件中提取到以下信息：订单号是 DP1002，客户是张三，金额 199 元。

data = json.loads(answer)   # 于是你顺手写了这两行
print(data["order_id"])     # json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

你换成 `chat(...)` 要求"返回 JSON"，于是遇到更多花样：有时候外面包了一层 ` ```json `；有时候前面加一句"好的，这是抽取结果："；有时候字段名变成 `订单号`；有时候金额变成字符串 `"199"`。每种情况你的脚本都要崩一次。

**这一章要解决的就是这道坎：从"会聊天"到"会开发"，分界线是"输出必须是程序能解析的"。** 本章的三件核心武器是：**结构化的 Prompt、JSON 模式、以及一层雷打不动的代码校验。**

---

## 1. 为什么"人看得懂"等于 0 分

### 1.1 两种消费者的区别

| 消费者 | 要什么 | "好的，订单号是 DP1002" 对它意味着 |
|--------|--------|-----------------------------------|
| 人 | 通顺、有礼貌、有上下文 | 完美 |
| 程序 | 稳定的字段名 + 稳定的类型 | 灾难：`json.loads` 抛异常，`data["order_id"]` 抛 `KeyError` |

开发者的工作分界线就在这里：**你的下游是代码，不是人。** 代码不懂"大概是这个意思"，它只认确定的字符串和类型。

### 1.2 三条出路

遇到"解析不了"，只有三条路可走 —— 本章把三条都讲透：

1. **约束 Prompt**：把格式要求写清楚，给示例（成本最低，可靠性中等）。
2. **用服务商的能力**：`response_format`（JSON mode）让 API 层保证返回合法 JSON。
3. **代码兜底**：解析失败就修复 + 重试，仍然失败就降级（**这条不能省**，因为前两条都不是 100%）。

### ⚠️ 常见坑

- **现象：本地测试 20 次都成功，上线后偶发崩溃。** 原因：模型输出有概率波动（第 02 章：`temperature=0` 也不保证确定），20 次不出问题不代表 2000 次不出问题。改法：**任何要进 `json.loads` 的文本，前面必须有一个 `try/except`**，这跟"网络请求必须处理超时"是同一等级的常识。
- **现象：用 `re` 从文本里抠字段（`re.search(r"订单号是(\w+)", answer)`）。** 后果：模型换个说法就失效（"订单号为"、"订单："、"DP1002"），维护成本随表达方式指数增长。改法：要字段就**要求它输出 JSON**，别去解析自然语言。
- **现象：把模型的整段输出存进数据库的 `remark` 字段，界面显示时再想办法拆。** 后果：所有下游都被"不结构化"污染。改法：入口处就结构化，宁可在这里失败重试，也不要把混乱往下游传。

---

## 2. Prompt 的六段式结构

### 2.1 六段是什么，缺一段会怎样

| 段落 | 作用 | 不写会出现的具体现象 |
|------|------|---------------------|
| **① 角色** | 定身份与专业视角 | 语气漂移：一会儿客服、一会儿文案、一会儿百科 |
| **② 任务** | 一个动词说清做什么 | 模型自由发挥，比如"顺便"帮你把邮件也重写了一遍 |
| **③ 约束** | 边界：不许做什么、缺信息怎么办 | 加解释、加 emoji、加"希望有帮助"、编造缺失字段 |
| **④ 输入** | 待处理的内容，用分隔符包起来 | 模型分不清"指令"和"数据"（也是 Prompt 注入的入口，第 12 章） |
| **⑤ 输出格式** | 字段名、类型、可空、枚举值 | 每次结构都不一样，字段名有时中文有时英文 |
| **⑥ 示例** | 一个正例（可选一个负例） | 边界情况全靠猜：找不到的字段是 `null` 还是 `""` 还是省略？ |

### 2.2 坏 Prompt → 好 Prompt：同一个任务的完整对照

**坏版本**：

```text
帮我从这封邮件里提取订单信息：{email}
```

真实可能的输出（三种都见过）：

```text
好的，我从这封邮件中提取到了以下信息：订单号是 DP1002，客户名称是张三，金额 199 元。
```
```text
订单信息：订单号 DP1002 / 客户 张三 / 金额：199元（人民币）
```
```text
{"订单号": "DP1002", "客户": "张三", "金额": "199"}
```

**好版本**（六段齐全，建议直接抄这个骨架）：

```text
你是订单信息抽取器。

【任务】从 <email> 标签内的邮件正文中抽取订单信息。

【约束】
1. 只输出 JSON，不要输出任何解释、前后缀、Markdown 代码块。
2. 邮件里没有的信息填 null，不要猜测、不要编造。
3. amount 必须是数字（不带货币符号），currency 用三位大写字母代码。
4. order_id 保持原样，不要补全、不要改写大小写。

【输出格式】
{"order_id": "string|null", "customer": "string|null", "amount": number|null, "currency": "string|null"}

【示例】
输入：<email>你好，订单 DP1002 麻烦退一下，一共 199 元。</email>
输出：{"order_id": "DP1002", "customer": null, "amount": 199, "currency": "CNY"}

【输入】
<email>{email}</email>
```

三个值得注意的细节：

- **示例里故意给了一个 `null`**（示例中 `customer` 就是 `null`）。**一个含边界情况的示例，比十条约束更有效** —— 模型从示例里学"缺失就填 null"比从文字规则里学更稳。
- **`{email}` 放在最后。** 两个原因：① 模型对靠后的内容注意力更强；② 静态的 prompt 前缀保持不变，**可以命中服务商的缓存**（第 02 章：prefix cache 要求前缀逐字节相同）。
- **`<email>` 标签把数据和指令隔开。** 这是最低成本的防注入手段：邮件里就算写了"忽略以上指令"，它也只是被包裹的数据。

### ⚠️ 常见坑

- **现象：示例只给了一个"完美情况"，遇到缺字段的邮件就乱编。** 改法：示例里必须包含一个 `null`、一个空数组、一个数字字符串（比如"数量：3 件"）这类边界样本。
- **现象：约束写了一大堆"不要做 X"，模型反而更容易做 X。** 原因：否定的说法本身会把 X 提到模型注意力里。改法：把否定改成**正向指令**："只输出 JSON（不要解释）" → 更好的是"第一个字符必须是 `{`，最后一个字符必须是 `}`"。
- **现象：示例和输出格式不一致**（格式写 `amount: number`，示例里却是 `"199"`）。后果：模型会**优先学示例**，于是你的类型约定作废。改法：写完 prompt 后逐字段核对"格式说明"与"示例"是否一致 —— 这是最值得花 30 秒的检查。
- **现象：把整封邮件（含签名、往期回复）原样塞进 prompt，抽取结果被历史信息污染。** 改法：**预处理比 prompt 技巧更有效** —— 先用代码把邮件正文裁到关键字附近（去掉免责声明、签名档），再喂给模型。

---

## 3. 让模型稳定输出 JSON 的完整套路

### 3.1 五步法

```text
① 明确 schema      —— 把字段名、类型、可否为 null、枚举取值都写进 prompt
② 给一个示例       —— 含一个边界情况（null / 空数组 / 数字字符串）
③ 强约束措辞       —— 「只输出 JSON」「第一个字符必须是 {」「不要解释」
④ 开启 JSON mode   —— 服务商支持就加上 response_format={"type": "json_object"}
⑤ 代码校验 + 重试  —— 第 4 节，这一步不能省
```

第 ④ 步的用法（以 OpenAI 兼容接口为例）：

```python
resp = llm.chat(
    messages=[
        {"role": "system", "content": "你是订单抽取器，只输出 JSON。"},   # JSON mode 通常要求 system 里出现 "JSON" 字样
        {"role": "user", "content": prompt},
    ],
    temperature=0,
    response_format={"type": "json_object"},
)
print(resp["content"])
```

> 说明：本项目的 `LLM.chat(messages, tools=None, temperature=None, max_tokens=None)` 会把**额外关键字参数原样透传**进请求体，所以 `response_format` 可以直接传；服务商不支持时会抛 `RuntimeError`，用下面的降级函数兜住。

支持情况（**以各服务商最新文档为准**，不支持的传了会报 400）：

| 服务商 | `response_format` | 备注 |
|--------|-------------------|------|
| OpenAI | 支持 | 较早支持，含 JSON Schema 模式 |
| DeepSeek | 支持 | 需在 prompt 中出现 "json" 字样 |
| 月之暗面 Kimi | 支持 | 同上，注意其文档对枚举值的限制 |
| 智谱 GLM | 支持 | 部分模型支持，传之前先试一次 |
| 通义千问（兼容模式） | 支持情况随模型不同 | 报 400 就去掉该字段，退回纯 Prompt 约束 |

**工程建议：写一个降级开关。** 第一次带 `response_format`，如果服务端报错（`invalid_parameter` / `unsupported`），自动去掉重发一次 —— 这样你的代码在"支持"和"不支持"的服务商上都能跑：

```python
def chat_json(llm, messages, **kw):
    try:
        return llm.chat(messages, response_format={"type": "json_object"}, **kw)
    except RuntimeError as error:                 # 服务商不支持该参数
        print("[warn] 该服务不支持 response_format，已退回纯 Prompt 约束：%s" % error)
        return llm.chat(messages, **kw)
```

### 3.2 关键认知：JSON mode 只保证"是合法 JSON"

它会保证输出能被 `json.loads` 解析，**但不保证字段符合你的 schema**：

```json
{"order_number": "DP1002", "customer_name": "张三", "total": "199"}   ← 合法 JSON，但字段名和类型全不对
```

所以：**JSON mode 解决的是"格式合法"，代码校验解决的是"语义正确"，两者不能互相替代。** 这也是为什么第 4 节必须存在。

### ⚠️ 常见坑

- **现象：开了 JSON mode 仍然拿到带解释的文字。** 原因：某服务商的实现是"prompt 注入式"的软约束，或你走的是中转网关把参数吞了。改法：保留第 4 节的校验与重试，不要假设它一定生效。
- **现象：传了 `response_format` 报 400 `invalid_parameter`。** 原因：该模型/该版本不支持。改法：用上面的 `chat_json()` 降级，并在日志里记下"当前服务商不支持"，避免以后重复踩。
- **现象：JSON mode 下模型输出 `{}`（空对象）。** 原因：prompt 里的 schema 描述含糊，模型"合法地"给了个空。改法：schema + 示例 + 必填字段说明三件套一起上；空对象要在校验层被判定为失败（缺必填字段）并重试。

---

## 4. 代码校验：`safe_parse_json` + 重试

### 4.1 模型会怎么破坏你的 JSON（9 个现场）

| # | 现象 | 原因 | 修法 |
|---|------|------|------|
| 1 | 外面包一层代码块：第一行是三个反引号 + `json` | 训练数据里的习惯 | 剥壳（正则提取代码块内容） |
| 2 | 前面加了解释："好的，这是抽取结果：" | 模型"礼貌" | 从第一个 `{` 开始截，配对括号取到结尾 |
| 3 | 用了中文引号 `“order_id”` | 中文语料影响 / 你粘贴时被自动替换 | 全角引号 → 半角 |
| 4 | 数字变字符串 `"199"` | 模型对类型不敏感 | 类型校验 + 允许安全转换（`"199"` → `199`） |
| 5 | 单引号 `{'a': 1}` | Python 习惯污染 | 尝试 `ast.literal_eval` 兜底，或重试 |
| 6 | 尾随逗号 `{"a": 1,}` | 生成习惯 | 正则修复 |
| 7 | 加了注释 `{"a": 1}  // 说明` | 延续代码风格 | 配对括号截断即可剥离 |
| 8 | 输出被截断：`{"order_id": "DP1` | `max_tokens` 太小（第 02 章） | `json.loads` 必然失败 → 调大 `max_tokens` 后重试 |
| 9 | 字段名漂移：`订单号` / `order_no` / `orderId` | 没给示例或示例与格式不一致 | 校验必填字段，不通过就重试；从源头修 prompt |

### 4.2 `safe_parse_json` 完整实现

```python
import json
import re

class SchemaError(Exception):
    """模型输出能解析成 JSON，但字段不符合约定（缺字段/类型不对）。"""

def _strip_code_fence(text):
    """剥掉三个反引号包裹的代码块。用 {3} 写反引号，避免和文档里的代码块冲突。"""
    match = re.search(r"`{3}(?:json)?\s*(.*?)`{3}", text, re.S)
    return match.group(1).strip() if match else text.strip()

def _normalize_quotes(text):
    """中文引号 → 英文引号（最常见的手滑来源）。"""
    return (text.replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'"))

def _cut_outer_braces(text):
    """从第一个 { 或 [ 开始，用括号配对找到配对的收尾，丢掉后面的解释文字。
    配对时会跳过字符串内部，避免被字符串里的括号骗到。"""
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

def safe_parse_json(text, required_fields=None, field_types=None):
    """把模型的自由文本尽量变成 dict。失败抛 SchemaError。"""
    if not text or not text.strip():
        raise SchemaError("模型返回空内容")
    candidate = _cut_outer_braces(_normalize_quotes(_strip_code_fence(text)))
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)      # 去尾随逗号
    try:
        data = json.loads(candidate)
    except ValueError as error:
        raise SchemaError("JSON 解析失败：%s | 原文前 200 字：%s" % (error, candidate[:200]))

    if not isinstance(data, dict):
        raise SchemaError("顶层不是对象，而是 %s" % type(data).__name__)

    for name in (required_fields or []):
        if name not in data:
            raise SchemaError("缺少必填字段：%s（实际字段：%s）" % (name, sorted(data.keys())))

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
                raise SchemaError("字段 %s 应为数字，实际是无法转换的字符串 %r" % (name, value))
        if isinstance(value, bool) and wanted in ((int,), (float,), (int, float)):
            raise SchemaError("字段 %s 应为数字，实际是布尔值（bool 是 int 的子类，必须显式排除）" % name)
        if not isinstance(value, wanted):
            raise SchemaError("字段 %s 应为 %s，实际是 %s" % (name, wanted, type(value).__name__))
    return data
```

### 4.3 重试的关键：把报错回灌给模型

**重试不是"用同样的 prompt 再问一遍"** —— 那样只是碰运气。有效的做法是把「上一次的原文 + 具体的错误信息」一起发回去：

```python
def extract_order(email_text, llm, max_attempts=3):
    """抽取订单信息：解析失败就把错误回灌给模型重试。"""
    prompt = build_prompt(email_text)          # 第 2.2 节的六段式 prompt
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
                 "上一次的输出无法被解析，错误信息：%s\n"
                 "请重新只输出一个 JSON 对象，第一个字符必须是 {，不要任何其他文字。" % last_error},
            ]
        last_raw = llm.chat(messages, temperature=0)["content"]      # 注意：LLM.chat 返回 dict
        try:
            return safe_parse_json(last_raw,
                                   required_fields=["order_id"],
                                   field_types={"amount": (int, float)})
        except SchemaError as error:
            last_error = str(error)
            print("[retry %d/%d] %s" % (attempt, max_attempts, last_error))
    raise SchemaError("重试 %d 次仍失败，最后一次错误：%s" % (max_attempts, last_error))
```

回灌为什么有效：模型非常擅长"按指出的错误改"，这比让它猜"我哪里不满意"简单得多。实测里，**第 2 次尝试的成功率通常明显高于第 1 次的重复**。并且错误信息要具体：`缺少必填字段：order_id（实际字段：['订单号', 'amount']）` 远比"格式不对"有用 —— 它把"应该改什么"直接告诉了模型。

### 4.4 重试的边界

| 边界 | 建议值 | 理由 |
|------|--------|------|
| 最大尝试次数 | 2–3 次 | 每次都是钱和时间；3 次仍失败说明是 prompt/任务本身有问题 |
| 每次重试的 `temperature` | 0（首次）→ 0.2（重试） | 首次求稳，重试时略微放开避免"卡在同一个错法上" |
| 降级策略 | 返回 `None` + 记一条日志 | 让上游决定"转人工还是丢弃"，不要让异常把整批任务打断 |
| 失败监控 | 统计"重试率"和"最终失败率" | 重试率突然上升 = 服务商模型变了或你的 prompt 有问题（第 12 章） |

```python
def extract_or_none(email_text, llm):
    """生产环境里更常见的写法：不抛异常，返回 None 让上游决策。"""
    try:
        return extract_order(email_text, llm)
    except SchemaError as error:
        print("[fail] 抽取失败，转人工：%s" % error)
        return None
```

### ⚠️ 常见坑

- **现象：重试 3 次全失败，报错信息完全一样。** 原因：错误是"输入本身不具备该字段"（比如邮件里根本没有订单号），模型不可能变出来。改法：区分**可重试错误**（格式错、字段名漂移）和**不可重试错误**（信息不存在）。后者应该在 prompt 里明确允许 `null`，并把 `order_id` 设为"可空但必须出现"。
- **现象：`isinstance(True, int)` 是 `True`，导致布尔值通过了数字校验。** 改法：见上面代码里对 `bool` 的显式排除 —— 这个坑在"数量/金额"字段上真实存在，模型有时输出 `true`。
- **现象：正则去尾随逗号把字符串里的内容改坏了**（比如 `{"note": "x,}"}`）。原因：启发式修复不区分字符串内外。改法：接受这个极小概率的误修（修坏的 JSON 会在下一步解析时失败并重试），或者用更严格的解析器。**关键是不让它静默通过** —— 校验层的存在就是为了兜住启发式修复的误伤。
- **现象：重试时把模型上次的错误输出原样放进 `assistant` 消息，但它"以为"自己答对了。** 原因：错误信息只在后面的 user 消息里。改法：像示例那样，`assistant` 放原文、紧跟着的 `user` 明确说"上一次的输出无法被解析 + 具体错误"。
- **现象：解析失败时只打印 `JSONDecodeError: Expecting value: line 1 column 1`，看不到原文。** 改法：异常里一定要带**原文前 200 字**（`SchemaError` 里已经这么做了）。不然你连"模型到底输出了什么"都不知道，无法修 prompt。

---

## 5. 三种结构化输出方案对比

| 维度 | 纯 Prompt 约束 | JSON mode（`response_format`） | Function Calling（第 04 章） |
|------|----------------|-------------------------------|------------------------------|
| 怎么工作 | 靠提示词 + 示例，模型自觉 | API 层约束"必须是合法 JSON" | 用工具 schema 约束参数结构 |
| 保证什么 | 什么都不保证 | **只保证 JSON 合法** | schema 驱动，结构通常更稳 |
| 字段名/类型对吗 | 不保证 | 不保证（可能字段名全错） | 相对更可靠，但仍需校验 |
| 能否强制枚举值 | 靠 prompt | 部分支持 JSON Schema 模式 | 支持（枚举写进 schema） |
| 服务商支持 | 全部支持 | 多数支持，版本不一 | 主流支持 |
| 适合什么 | 快速原型、单字段、简单场景 | 需要稳定产出 JSON 的首选 | 需要多工具、需要模型自己决定调哪个 |
| 调试难度 | 低（就是文字） | 低 | 中（要看 `tool_calls` 结构） |

**三条结论：**

1. **能开 JSON mode 就开**，它是免费的可靠性提升。
2. **要"模型自己决定调什么工具"时，才上 Function Calling**（第 04 章），它解决的是"选哪个动作"，不只是"输出什么格式"。
3. **任何一层都不能省掉代码校验。** JSON mode 保证不了字段名，Function Calling 也保证不了业务规则（比如"金额必须大于 0"）。**prompt 层负责提高成功率，代码层负责不把错误放行。**

### ⚠️ 常见坑

- **现象：同一段业务代码里三套方案混用，解析路径分叉。** 比如有的调用走 `safe_parse_json(文本)`，有的走 `tool_calls` 分支，有的直接 `json.loads`。后果：改动 schema 时漏掉一处，线上出现"某些路径报 KeyError"。改法：**封装统一入口**（例如 `extract(prompt, schema)`），内部决定用哪种方案，调用方永远只拿到 dict 或 `None`。
- **现象：开了 JSON mode，同时在 prompt 里要求"请顺便解释一下你的判断"。** 冲突：JSON mode 要求输出是一个 JSON 对象，解释文字会导致解析失败或解释被塞进某个字段。改法：**要么加一个 `reason` 字段**（结构化地放解释），要么这次调用不开 JSON mode，分两步做。
- **现象：以为 Function Calling 会帮你校验业务规则。** 它只约束**结构**（字段名和类型由 schema 描述），不会校验"订单号是否存在""金额是否超限"。改法：业务规则永远写在代码里（第 02 章的两层校验 + 本章的重试）。
- **现象：用 Function Calling 只为了"拿一个 JSON"，结果代码复杂度翻倍。** 提示：如果不需要"模型自己选动作"，`response_format` + 校验就够了；上 Function Calling 的判据是**多工具、需要模型决定调哪个**。

---

## 6. 跑通本章代码

```bash
cd Agent-Zero-To-One/code

# 先离线跑（Mock 会故意返回几种"坏 JSON"，让你看校验层怎么处理）
AGENT_MOCK=1 python ch03_prompt_structured.py
```

脚本做四件事：

```text
==================================================
第 03 章 · Prompt 结构化与 JSON 校验
==================================================

【1】坏 Prompt vs 好 Prompt 的对比
  坏 Prompt 的输出（3 种真实风格）：
    - 好的，我从这封邮件中提取到了以下信息：订单号是 DP1002，客户名称是张三，金额 199 元。
    - ```json {"order_id": "DP1002", ...} ```（外面包了代码块）
    - {"订单号": "DP1002", "金额": "199"}（字段名与类型都不对）
  → 三种都无法直接 json.loads 成你要的字段。

【2】safe_parse_json 逐个修复
  输入 1：三个反引号包裹的代码块            → 剥壳成功
  输入 2：「好的，这是结果：{...}」          → 括号配对截断成功
  输入 3：{"order_id": "DP1002",}           → 去尾随逗号成功
  输入 4：{"order_id": “DP1002”}            → 中文引号修复成功
  输入 5：{"amount": "199"}                → 数字字符串安全转换成功
  输入 6：{"amount": true}                 → 拒绝（布尔不是数字）
  输入 7：{"alert": "x"}                   → 拒绝（缺少必填字段 order_id）

【3】错误回灌重试
  第 1 次：{"订单号": "DP1002"} → SchemaError：缺少必填字段 order_id
  第 2 次（把错误回灌）：{"order_id": "DP1002", ...} → 成功

【4】三种方案的成本与成功率对比（在线模式跑 20 次统计）
  Mock 模式下跳过；在线模式会打印 JSON 合法率 / 字段正确率 / 平均 token
--------------------------------------------------
结论：Prompt 决定成功率，代码校验决定不出事故。两者都要。
==================================================
```

**在线跑一次更有价值**（同一段 prompt 跑 20 次，统计 JSON 合法率）：

```bash
python ch03_prompt_structured.py
```

把结果记进笔记 —— 这份"自己实测的成功率"比任何教程里的百分比都可信。

---

## 本章小结

| 结论 | 关键细节 |
|------|----------|
| 分界线：输出要能被代码解析 | "人看得懂"对程序等于 0 分 |
| 六段式 Prompt | 角色 / 任务 / 约束 / 输入 / 输出格式 / 示例；示例里必须有边界情况 |
| 输入放最后 | 注意力更靠后 + 静态前缀可命中缓存 |
| 分隔符包住数据 | `<email>...</email>` 是最低成本的防注入 |
| JSON mode 的边界 | 只保证"合法 JSON"，不保证字段名和类型 |
| 校验层不能省 | `safe_parse_json`：剥壳 → 括号截断 → 引号/逗号修复 → 字段与类型校验 |
| 重试要回灌错误 | 上次原文 + 具体错误信息，成功率远高于重复同样的请求 |
| 重试有边界 | 最多 2–3 次，失败降级为 `None` + 日志，别让异常打断整批任务 |
| 三方案取舍 | 能开 JSON mode 就开；要选工具才上 Function Calling；代码校验永远保留 |

---

## 动手练习

**练习 1（照着改一行）：让它必定失败一次。** 把 `extract_order()` 里 `build_prompt()` 返回的 prompt 中"只输出 JSON"那句删掉，跑 5 次，统计有多少次触发了 `[retry]`。再把"示例里的 `customer: null`"这一行删掉，观察"邮件里没有客户名"时的表现有什么变化。

**练习 2（加一条业务规则）：** 给 `safe_parse_json()` 的调用处加一条业务校验：`amount` 如果小于 0 或大于 100000，判为失败并重试。想一想：这类"业务规则"该放在 `safe_parse_json()` 里，还是放在调用方？（提示：`safe_parse_json` 应该只关心"格式与类型"，业务规则属于调用方 —— 因为它跟具体任务绑定。）

**练习 3（自己实现一个新功能）：做一个批量抽取器 + 失败报告。** 写 `batch_extract(emails, llm)`：对每封邮件调用 `extract_or_none()`，最后打印一份报告：成功数、重试次数分布、失败清单（含每封失败的最后一轮错误信息）。要求：① 单封失败不能中断整批；② 报告里给出"总 token 消耗"和"估算成本"（用第 02 章的 `estimate_cost`）。

---

## 自测题

**第 1 题：** 为什么不能靠正则表达式从模型的自然语言输出里抠字段？

**第 2 题：** 开了 JSON mode，为什么还需要写 `safe_parse_json` 这一层？

**第 3 题：** 重试时"把上次的输出和错误信息一起发回去"为什么比"用同样的 prompt 再问一遍"有效？

**第 4 题：** 模型的输出是 `{"order_id": "DP1002", "amount": "199"}`，你的 schema 要求 `amount` 是数字。列出两种处理方式，并说明各自的取舍。

**第 5 题：** 什么时候该从"纯 Prompt 约束"升级到 Function Calling？请说出一个必须升级的场景，和一个不需要升级的场景。

<details>
<summary>点击查看答案</summary>

**第 1 题：** 因为自然语言的表达方式几乎无穷（"订单号是"、"订单号为"、"订单："、"单号 DP1002"……），正则只能覆盖你想到的那几种；模型换个说法你就漏抽或误抽。而且正则很难处理"字段缺失""多个订单号""否定句（不是 DP1002）"这些情况。正确做法是**要求结构化输出**，把"理解语义"交给模型，把"校验结构"交给代码 —— 各做各擅长的事。

**第 2 题：** 因为 JSON mode 只保证"这段文本能被 `json.loads` 解析"，**不保证内容符合你的 schema**：字段名可能是 `order_number`（而不是 `order_id`）、类型可能是字符串、字段可能缺失、甚至返回 `{}`。`safe_parse_json` 负责的是语义层的检查：必填字段是否存在、类型是否正确、能否安全转换。两者是"格式合法"和"语义正确"两个不同的关卡，缺一不可。

**第 3 题：** 因为重复同样的请求只是抽样碰运气（`temperature=0` 也可能因为浮点/批处理而变化），模型并不知道你要它改什么。把"上次原文 + 具体错误"发回去，等于给了一个**明确的修改指令**（`缺少必填字段 order_id（实际字段：['订单号', 'amount']）`），模型很擅长按指出的错误精确修改。错误信息越具体，第 2 次成功率越高。

**第 4 题：** ① **安全转换**：在类型校验里对"期望数字但拿到纯数字字符串"做 `int()/float()` 转换，转换失败才判错。取舍：兼容性好、少一次重试（省钱省时），代价是"数字字符串"这种模型错误被静默容忍，可能掩盖 prompt 的缺陷（建议在日志里记一次"发生转换"，用于监控）。② **直接判错并重试**，并在 prompt 里加强"amount 必须是数字，不要加引号"。取舍：数据更干净、能尽早暴露 prompt 问题，代价是多一次调用（钱 + 延迟），且如果模型顽固，3 次全失败就转人工了。生产上常见组合：**转换 + 记日志 + 在 prompt 里修**，三件事一起做。

**第 5 题：** 该升级的场景：**需要模型自己决定"调用哪个动作"**，而不是只输出数据。例如"根据客户邮件内容，决定是查询订单、还是查询物流、还是直接转人工，并给出对应参数" —— 这时用工具 schema 约束参数结构比在 prompt 里描述更可靠，也是第 04 章的内容。不需要升级的场景：**只是要一个固定结构的 JSON**（抽取订单号、客户名、金额），纯 Prompt + JSON mode + 代码校验就够了，上 Function Calling 只会让代码更复杂、还要处理 `tool_calls` 的解析。

</details>

---

## 本章产出 / 交付标准

- [ ] 能写出六段式 prompt，并说出每一段"不写会出现什么现象"。
- [ ] 能默写 `safe_parse_json` 的处理顺序：剥壳 → 括号截断 → 引号/逗号修复 → 字段与类型校验。
- [ ] 手写实现"错误回灌"的重试循环，并解释它为什么有效。
- [ ] 能说出 JSON mode 的两个边界（只保证合法 JSON、不保证字段），并给出一段支持/不支持的降级代码。
- [ ] 跑通 `AGENT_MOCK=1 python ch03_prompt_structured.py`，看清 7 种输入分别被如何处理。
- [ ] 在线跑一次"同一 prompt 20 次"的 JSON 合法率统计，把**实测数字**记进笔记。
- [ ] 完成练习 3 的批量抽取器，单封失败不中断整批，并输出失败清单与成本。
- [ ] 记录到 [[03-学习笔记/Week02-Prompt与结构化输出]]，更新 [[00-配置/进度看板]]。

**加分项：** 把你抽取器的 `order_id` 加上"格式校验 + 存在性校验"（第 02 章的两层校验），并统计"模型编造的订单号"比例。

---

## 结语与下一章

Prompt 不是"把话说清楚"，而是给一个看不见的合作者写接口文档：schema 是字段表，示例是样张，校验层是质检员。模型负责生成，代码负责验收 —— **这条流水线上唯一不能省的，就是质检那一环**，因为模型永远会用"最像正确答案"的方式出错。

但校验只能保证"格式对、字段在"，还有一类事它管不了：模型说"我帮你查了天气"，其实它根本没有这个能力。

下一章是第 04 章《工具调用》，把本章的"结构化输出"从**说**升级到**做**：模型不再只输出 JSON，而是输出"我要调用 `search_order(order_id='DP1002')`"，由你的代码去执行。我们会手写一遍完整的 Function Calling 循环（不用任何框架），讲清 `tools` schema 怎么写、`tool_calls` 怎么解析、`role: "tool"` 消息为什么要带 `tool_call_id`、工具描述怎么写得让模型不选错，以及工具报错时怎么把错误变成"正常的观察结果"。

> 配套代码：`code/ch04_tool_calling.py`　配套阅读：[[02-Wiki/专题总结/02-Prompt工程与结构化输出]]
