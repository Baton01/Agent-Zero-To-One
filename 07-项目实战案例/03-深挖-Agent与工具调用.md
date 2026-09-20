# 深挖：Agent 与工具调用

> **一句话** · 这条线的核心矛盾是：**模型只输出"想调什么、参数是什么"的概率性文本，而正确性必须由你的确定性代码保证**。Agent 循环就是这两者之间那条缝的缝合处——缝得不好，所有问题都会以"模型乱调工具"或"工具结果莫名丢失"的形式暴露。
>
> **读完你能回答：** ① 循环的三个终止条件分别是什么语义，为什么"模型不返回 tool_calls"不是错误而是唯一的成功出口？② `tool_call_id` 到底约束了什么，为什么省掉它在本项目的 Ollama 下能跑通、换 OpenAI 官方接口必然 400？③ MCP 客户端哪些地方是"协议做对了"、哪些是"看起来对但机制上永远不触发"？

---

## 一、本质：Agent 循环到底在循环什么

### 先看没有它会出什么问题

假设你只有一次 `Chat` 调用。用户问"今天北京适合带孩子去哪玩"，模型能做的只有两件事：从训练数据里回忆（今天是几号它不知道）、或者编一个（它不知道天气）。**幻觉不是模型坏了，是信息缺失**——训练数据里没有"此刻"。你可以在 prompt 里塞实时数据，但塞什么得你提前决定，用户的下一个问题你猜不到。工具调用要解决的就是这件事：**把"需要确定性的部分"从模型手里拿走，交给代码**。模型负责"决定调什么、参数填什么"，代码负责"真的算、真的查"。ReAct 循环是把这个交换重复下去。

### ReAct 的机制，不是定义

ReAct 的原始形式是 `Thought → Action → Observation → ...`。在 Function Calling 的实现里，这三段被拆到了两个地方：

| ReAct 阶段 | 在 Function Calling 里的落地 |
|---|---|
| Thought | 模型的 `content` 文本（可选，很多模型直接跳到动作） |
| Action | 模型的 `tool_calls` 结构化输出 |
| Observation | 你回喂的 `role:"tool"` 消息 |

**关键认识：循环的驱动者是模型，不是框架。** 框架不知道"还差几步"，它只做三件事：把消息 + 工具列表交给模型、看模型有没有要工具、有就把结果接上再交给模型。所以——

**为什么"模型不返回 tool_calls"是正常出口。** 模型是个自回归的下一 token 预测器，它没有任何内部状态表示"我信息够了"或"我该停了"；**不带工具调用、直接给出 content 就是它唯一的停止表达**。这不是异常路径，是设计里唯一的成功路径。

### 这个项目的实现锚点

```go
// pkg/agent/base.go:295-328（节选）
maxIterations := 10                                    // ← 预算，不是逻辑
for i := 0; i < maxIterations; i++ {
	toolDefs := a.toolManager.GetToolDefinitions()      // 每轮重建工具定义
	response, err := a.llmClient.ChatWithTools(ctx, currentMessages, toolDefs)
	if err != nil {
		return "", fmt.Errorf("LLM 调用失败: %w", err)   // 出口②：基础设施故障
	}
	if len(response.ToolCalls) == 0 {
		return response.Content, nil                    // 出口①：正常完成
	}
	// ...执行工具、回填消息...
}
return "", fmt.Errorf("工具调用次数超过限制")             // 出口③：预算耗尽
```

| 出口 | 位置 | 语义 | 应该怎么向用户表达 |
|---|---|---|---|
| ① 无 tool_calls | `base.go:325` | 正常完成 | 直接返回 content |
| ② LLM 报错 | `base.go:320-322` | 基础设施故障 | 可重试；不要吞 |
| ③ 跑满 10 轮 | `base.go:384` | **预算耗尽**（模型死循环或任务太大） | 降级返回已完成部分或转人工，不是 500 |

**但注意出口③的一个实现缺陷**：`base.go:158` 已经把 user 消息写进了会话记忆，而 `base.go:177` 的"写回 assistant 回复"因 `base.go:172-174` 提前 return 而不可达。结果是历史上留下一条**永远没有回复的 user 消息**，下一轮会被重发。

再一个反直觉点：**这个循环里没有"规划器"**。模型的规划只存在于 `content` 文本里，框架不解析、不校验。这不是缺陷——ReAct 本来就是"边想边做"。真正需要显式规划（先 A 再判断再 B）的场景由另一个引擎承接：`pkg/workflow/core/executor.go:27` 的图执行。两个引擎分开是有意的架构选择。

---

## 二、这个项目怎么实现的（带行号）

### 2.1 消息历史是怎么维护的

这里有**两套完全不同的历史**，混在一起看就会理解错：

| | 存储位置 | 生命周期 | 内容 |
|---|---|---|---|
| 会话记忆 | `memory.ConversationMemory` | 跨轮持久（进程内） | 用户输入 + 最终回答 + 工具调用记录 |
| 循环消息 | `currentMessages` 局部变量 | 单轮内，用完即弃 | system + 历史 + assistant(tool_calls) + tool 结果 |

循环开始处，历史是**重建**而不是增量维护：

```go
// pkg/agent/base.go:306-308
messages := session.Memory.BuildMessages(systemPrompt, userInput)
currentMessages := make([]llm.Message, len(messages))
copy(currentMessages, a.convertMessages(messages))
```

工具结果只追加到 `currentMessages`（`base.go:377-380`），同时往记忆里写一条 `AddToolCall`（`base.go:374`）。下一轮请求进来时 `base.go:306` 又从记忆重建——那些 `assistant(tool_calls)` 和 `tool` 消息全部消失。**后果：多轮之间"上一轮的工具结果"对模型不可见**。用户追问"刚才那个天气再算上孩子年龄"，模型手里只有最终回答的文本，没有当时的气温数字。

会话记忆自己还有两个问题：

```go
// pkg/memory/conversation.go:78-83 —— 存的是工具名，不是结果（params/result 被丢弃）
func (m *ConversationMemory) AddToolCall(toolName string, params map[string]interface{}, result string) {
	m.AddInteraction(Interaction{Type: InteractionTypeTool, Content: toolName})
}
// conversation.go:195-197 —— 重建时映射成 role:"tool"
case InteractionTypeTool: msg.Role, msg.Content = "tool", interaction.Content
```

`maxSize=50` 硬编码在 `base.go:51`，超限丢最旧一条（`conversation.go:91-93`），**无摘要压缩**。顺带一个配置谎言：`config.go:56-61` 定义了 `AgentMemoryConfig{Enabled, MaxHistoryLength, SessionTimeoutMs, CompressionThreshold}`，`config.go:69-70` 还给了默认值——**这四个字段全仓零消费者**。记忆上限只认 `base.go:51` 那个字面量。被问"记忆怎么配置"不能答"配置驱动"。

### 2.2 工具描述在哪里、什么时候注入

**不在 system prompt 里**，而是每轮循环重新生成一个 OpenAI 的 `tools` 数组（`base.go:315-319`）：

```go
// pkg/tool/manager.go:83-101（节选）
for name, param := range t.GetParameters() {
	params[name] = map[string]interface{}{
		"type":        param.Type,
		"description": param.Description,      // ← param.Required 没有写进去
	}
}
def := llm.ToolDefinition{Type: "function", Function: llm.FunctionDef{
	Name: t.GetName(), Description: t.GetDescription(),
	// ← properties 里没有 "required" 数组
	Parameters: map[string]interface{}{"type": "object", "properties": params},
}}
```

**每轮重建是合理的**：工具集合可能中途变化（Skill 注入，见 `skill_enhancer.go:45-63`），只在构造时算一次的话，Skill 声明的工具永远不会出现在 `tools` 数组里。代价是每轮重新序列化全部工具描述——工具上百时这个开销和 token 占用就不能忽略。

**一个副作用**：Skill 路径下工具描述被注入两次——`skill_enhancer.go:70-80` 写进 system prompt，`base.go:316` 又生成 `tools` 数组。同一个信息占两份 token，而且措辞不同（一份是 `GetToolDescriptions` 手写的 Markdown，一份是 JSON Schema），模型可能给出不一致的解读。

### 2.3 工具执行失败怎么处理

错误处理有**四条路径**，只有前三条会走到模型面前：

```go
// pkg/agent/base.go:349-380（节选）
t, ok := a.toolManager.Get(tc.Name)
if !ok {                                              // 路径①：工具不存在
	currentMessages = append(currentMessages, llm.Message{Role: "tool", Content: fmt.Sprintf("工具 %s 不存在", tc.Name)})
	continue
}
result, err := t.Execute(tc.Arguments)
if err != nil {                                       // 路径②：Execute 返回 error
	currentMessages = append(currentMessages, llm.Message{Role: "tool", Content: fmt.Sprintf("工具执行失败: %s", err.Error())})
	continue
}
session.Memory.AddToolCall(tc.Name, tc.Arguments, result.Data)
// 路径③：工具"成功"返回，但 Data 可能是空串
currentMessages = append(currentMessages, llm.Message{Role: "tool", Content: result.Data})
```

**路径③是漏洞所在。** 四个内置工具的失败路径全是 `return tool.Error("..."), nil`——**err 为 nil、Data 为空串**：`calculator.go:44`（参数类型错）、`calculator.go:50`（除零）、`websearch.go:51`（关键词为空）、`dataanalyzer.go:55`（数据为空）。于是 `base.go:377-380` 塞给模型的是 `Content: ""`。

**模型看到的是一次"成功但什么都没返回"的工具调用**，它无从知道是参数错了还是数据源空了；用户侧体验是"工具返回空"。根因是 **`ToolResult.Error` 在整个 ReAct 循环里没有任何读者**——`base.go` 只看 `result.Data`，从不看 `result.Success` 和 `result.Error`。

顺带一个观测性缺口：`tool.Manager.Execute`（`manager.go:108-128`）带着 `log.ToolExecuteStart/Success` 和 metrics 埋点，但 **ReAct 循环里根本没调它**——`base.go:360` 是直接 `t.Execute`。所以这些日志和 `metrics.ToolCallsTotal`（`metrics.go:60`）在 stage1/stage2 是死代码，只有 stage4 的 `integration/tool_node.go:46` 用它。真正有输出的只有 `base.go` 里的 `AgentToolCall` / `AgentToolResult`（`log.go:298-311`）。

### 2.4 停止条件

真实存在的停止条件有四个，只有三个是设计出来的：

| # | 位置 | 触发 | 返回 |
|---|---|---|---|
| 1 | `base.go:325` | 模型不返回 tool_calls | `response.Content, nil` |
| 2 | `base.go:312` → `base.go:384` | `i` 跑满 10 | `error("工具调用次数超过限制")` |
| 3 | `base.go:320-322` | LLM 调用报错 | `error` |
| 4 | 隐式 | ctx 被取消 | 取决于下层 HTTP 客户端是否尊重 ctx |

**条件 4 在有意路径里基本失效**：stage1/stage2 传的是 `context.Background()`（`cmd/stage1/main.go:189`、`cmd/stage2/main.go:348`），永不取消、永无 deadline。所以"LLM 卡住"在这个 Agent 里等于"永久卡住"（stage3 的 gateway 路径传请求 ctx，会好一些）。**条件 2 的 10 是局部变量**（`base.go:296`，不是常量、不在 config 里），被问"为什么是 10"只能答"经验值"——对比工作流的 loop 节点把它做成了字段 `MaxIterations`（`patterns/loop_node.go:15`，默认 10 在 `:24`）：**同一个项目里两套循环用了两种做法**，这个对比本身就是好的回答素材。

---

## 三、五个必须知道的工程细节

### 3.1 工具调用消息协议：为什么 tool_call_id 不能省（重点）

#### OpenAI 协议要求什么

不是建议，是**校验规则**。一轮工具调用的正确形状：

```json
[
  {"role":"user","content":"北京今天适合去哪玩"},
  {"role":"assistant","content":null,
   "tool_calls":[{"id":"call_abc","type":"function",
                  "function":{"name":"datetime","arguments":"{\"timezone\":\"Asia/Shanghai\"}"}}]},
  {"role":"tool","tool_call_id":"call_abc","content":"当前时间: 2026-09-20 ..."}
]
```

三条硬约束：① `assistant` 里每个 `tool_call.id` **必须**在后面有一条 `tool` 消息的 `tool_call_id` 对应；② 每条 `role:"tool"` **必须**带 `tool_call_id` 且不能对应到不存在的 id；③ `function.arguments` 是 JSON **字符串**，不是对象。

#### 这个项目丢掉了什么

```go
// pkg/llm/client.go:8-11 —— Message 只有两个字段
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}
```

注意 `client.go:14-18` 的 `ToolCall` **是有 `ID` 字段的**，`openai.go:122-126` 也确实把 `tc.ID` 取出来了——但它在循环里被丢了两次：

```go
// pkg/agent/base.go:338-341 —— assistant 回填，tool_calls 没了
currentMessages = append(currentMessages, llm.Message{Role: "assistant", Content: response.Content})
// pkg/agent/base.go:377-380 —— tool 回填，没有 tool_call_id
currentMessages = append(currentMessages, llm.Message{Role: "tool", Content: result.Data})
```

发出去的请求于是变成 `[user, assistant:"我来查一下", tool:"当前时间: ..."]`——**孤儿 tool 消息**：前面没有带 `tool_calls` 的 assistant。

#### 为什么在本项目的 Ollama 环境里"跑得起来"

因为 Ollama 的 OpenAI 兼容层**不校验消息配对**。它把 `role` 当模板槽位渲染：`tool` 角色的内容被拼进 `tool` 槽，`assistant` 的内容进 `assistant` 槽，没有 `tool_calls` 的 assistant 就是一段普通文本，Qwen/Llama 的 chat template 能接受。

**这正是最危险的地方**：本地 demo 通过 ≠ 协议正确。因为 `param.Required` 丢失（3.3）和这个缺陷都不会在"单轮 + 不用历史"的场景里暴露，只有多轮会话才会炸——而教学 demo 通常只演示一轮。

#### 触发条件与爆炸半径

⚠️ **历史里至少有一次工具调用记录，且发生了第二轮请求。** 链路是：第 1 轮 `base.go:374` → `conversation.go:78-83` 把工具名写进记忆（`type=tool`, `content="datetime"`）；第 2 轮 `base.go:306` → `conversation.go:195-197` 把它映射成 `role:"tool"`，`content:"datetime"`，既无 `tool_call_id`，前面也无 `assistant.tool_calls`。换到严格校验的接口（OpenAI 官方、Azure、严格 vLLM 模式）直接 `400: messages with role 'tool' must be a response to a preceding message with 'tool_calls'`。**不是偶发、是可复现**：只要用过一次工具，这个会话就永久坏了。

#### 怎么修（三步，按依赖顺序）

```go
// 第 1 步：消息结构补字段（pkg/llm/client.go）
type Message struct {
	Role       string     `json:"role"`
	Content    string     `json:"content"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`   // assistant 携带
	ToolCallID string     `json:"tool_call_id,omitempty"` // tool 携带
}
// 第 2 步：回填时带上（base.go:338-341 / base.go:377-380）
append(..., llm.Message{Role: "assistant", Content: response.Content, ToolCalls: response.ToolCalls})
append(..., llm.Message{Role: "tool", ToolCallID: tc.ID, Content: payload})
```

第 3 步最容易漏：**`conversation.go:20-24` 的 `Interaction` 也要结构化**，否则落库的仍是工具名，`BuildMessages`（`conversation.go:171-212`）重建出来还是孤儿消息。修内存结构不修持久结构等于没修——因为 `base.go:306` 每轮都是从记忆重建的。建议 `Interaction` 加 `ToolCallID` / `ToolCalls`，`AddToolCall` 改成存 `result` 而不是 `toolName`。

**一个协议细节**：assistant 带 `tool_calls` 时 `content` 通常应为 `null` 而非 `""`。`openai.go:130-133` 现在把 `choice.Message.Content` 原样返回，模型只调工具不讲话时它是空串。严格接口一般容忍这个组合，但规范形状是 `null`，修的时候顺手处理。

### 3.2 工具描述怎么写模型才不会选错

**描述是唯一的路由表。** 模型选工具时能看到的全部信息就是：工具名 + `description` + 参数的 JSON Schema。没有任何其他信号——所以描述质量直接等于选择准确率。

反面教材就在这个项目里：

```go
// pkg/tool/tools/websearch.go:27-29
return "搜索网络信息。输入搜索关键词，返回相关搜索结果摘要。可搜索新闻、财经、科技等"
```

实现（`websearch.go:48-76`）：**不发任何网络请求**，按关键词走 `rand` 生成假新闻——连"上证指数报 3198 点"里的数字都是 `3000+r.Intn(200)`（`websearch.go:89`）。`dataanalyzer.go:27` 更严重："对输入数据进行统计分析、趋势分析、相关性分析等"——实现里 `data` 只被回显进报告头，统计量、趋势、相关性、建议**全部是 `r.Float64()`**（`dataanalyzer.go:103-106` 起）。

三个具体后果：

1. **模型会一本正经引用假数据。** 返回文本末尾确实有"注：以上为模拟数据"（`websearch.go:88`），但那是**数据体里的自然语言**，不是结构信号；模型完全可以忽略它，尤其在 system prompt 要求"给出精确结论"时。
2. **描述超出实现，能力不可发现。** 描述承诺"可搜索新闻、财经、科技"，`searchType` 参数声明枚举 `news/finance/tech`（`websearch.go:39-43`），而实际分支走的是中文关键词匹配（`websearch.go:62-73`：股票/AI/新能源/半导体）——`searchType` 只在 `default` 分支影响一句文案。**参数枚举与实现分支是两套互不相干的路由。**
3. **假数据没有可识别标记，无法自动熔断。** 如果返回里带 `data_source: "mock"` 这类结构字段，上层至少能拦截或加免责声明。现在没有。

正面例子就在同一个包里：

```go
// pkg/tool/tools/datetime.go:26-28
return "获取当前日期和时间。当用户提到'今天'、'明天'、'后天'、'这周末'等相对时间时调用此工具，返回详细的日期参考信息。"
```

它对的地方是**写了"什么时候该调用"**。模型最容易犯的错不是不知道有工具，而是**不知道现在该用它**——触发条件比功能说明更能提高准确率。对照 `calculator.go:26` 只写了功能（"执行基本的数学计算"），没说"不要用它算含括号的表达式"，模型就会拿它去算 `(2+3)*4`，而 `evaluateExpression`（`calculator.go:57-91`）按 `+ - * /` 从右往左切分、**不支持括号**，结果是错的。

**但注意**：描述写得再好也不能替代执行侧的校验。描述是在降低概率，校验才是保证（见 3.3）。

### 3.3 必填参数为什么要传给模型

**`required` 是模型侧唯一的"必填"信号。** `properties` 说明"有哪些字段"，`required` 说明"哪些必须填"。没有 `required` 数组，**模型眼里的所有字段都是可选的**——这不是模型偷懒，在它看到的 schema 里省略任何参数都合法。

```go
// pkg/tool/manager.go:83-88
for name, param := range t.GetParameters() {
	params[name] = map[string]interface{}{
		"type":        param.Type,
		"description": param.Description,
		// param.Required 没写进去
	}
}
```

`ParameterDef.Required`（`tool.go:22`）定义了，四个内置工具也认真标了（`websearch.go:37` 的 query、`calculator.go:35` 的 expression），**但它从未到达模型**。MCP 侧同样漏：`adapter.go:36-57` 只解析 `inputSchema["properties"]`，**完全没读 `required` 数组**——即使 MCP server 正确声明了 `required: ["city"]`，到适配层也被丢掉。

后果链——**错误被吞了两层**：

1. 模型省略 `query` → `websearch.go:50-52` 返回 `tool.Error("搜索关键词不能为空"), nil`
2. `base.go:361-368` 的 `if err != nil` **不成立**（err 是 nil）
3. 走到 `base.go:377-380`，回喂 `Content: result.Data` = `""`
4. 模型收到一条空内容的工具消息 → 用户看到"工具返回空"

需求上应该是"参数缺失，请补充关键词"，实际表现是"工具没反应"：模型填参数时没有必填提示（第一层），工具报错时错误文本又没进消息（第二层）。

```go
// 修法：pkg/tool/manager.go:82-101 —— 聚合 required 列表
required := make([]string, 0)
for name, param := range t.GetParameters() {
	params[name] = map[string]interface{}{"type": param.Type, "description": param.Description}
	if param.Required { required = append(required, name) }
}
// 在 Parameters 里加 "required": required（为空时省略该字段）
```

必须用 `[]string` 聚合：`GetParameters()` 返回 map，**遍历顺序随机**，不能靠"下一个参数"推断。MCP adapter 同步补：

```go
// pkg/mcp/adapter.go:43-50 附近
if req, ok := a.tool.InputSchema["required"].([]interface{}); ok {
	for _, r := range req {
		if name, ok := r.(string); ok {
			if p, exists := params[name]; exists { p.Required = true; params[name] = p }  // ← 必须写回
		}
	}
}
```
`adapter.go:43` 用的是值类型 `paramDef := tool.ParameterDef{}`，**改完必须重新赋值回 map**——Go 里改 map 内 struct 的经典坑，写漏这行改动静默失效。另外 `required` 只是降低概率、不是保证，执行侧的类型断言（`calculator.go:42-45`）必须保留；也别用它表达条件必填（"analysisType 是 correlation 时才必须传 data"），JSON Schema 表达不了，只能在工具内部校验。

### 3.4 工具粒度：粗还是细

**判据不是"功能单一"，而是"这组参数能否在一次模型决策里被完整确定"。** 参数之间必然一起出现的，就该在同一个工具里；如果需要模型先做一次路由决策才能确定参数，那应该是两个工具。

做得对的：`calculator` 只收一个 `expression`（`calculator.go:30-38`）。**把"算"和"说"分开**是关键决策——让模型直接算，它会在长数字上出错；用工具算，结果确定性。

偏粗的地方：`data_analyzer` 的 `analysisType` 是 `statistical/trend/correlation/recommendation` 四选一（`dataanalyzer.go:38-42`），`context` 是自由文本。**这是把内部路由塞进了参数里**——模型用一个 enum 选择工具内部的哪个分支，问题在于：模型对这个 enum 的理解没有依据（描述只有"分析类型"几个字），选错分支的代价是返回完全不相干的结果；`context` 进工具后基本不参与逻辑（只被回显进报告头），属于无效参数；工具内部的分支模型看不见、无法纠偏，失败时也不知道该改哪个参数。

`web_search` 是同一问题的另一面（`websearch.go:62-73`）：四个领域的分支被塞进一个工具，本该是**四个数据源四个工具**，或至少是四个显式枚举值而不是中文关键词的 `strings.Contains`。现在的行为是"问'行情'命中股票分支，问'股价'走 default"——路由结果不可预测。

- **粗粒度的代价**：描述变模糊（写四条太冗长，写一句泛泛则选不准）；参数变成选项，漏参/错参概率非线性上升；报错时无法归因。
- **细粒度的代价**：工具数量线性增长 → `tools` 数组 token 占用（`base.go:316` 全量发送，无检索）、选择空间变大、选错率上升；往返次数增加（两个细粒度工具 = 两轮循环 = 两次 LLM 调用）；N 份不重叠的触发条件描述维护成本高。

**实用规则**：一工具一事，而"一事"的判据是**"一组必然同时出现的参数"**。`analysisType` 不是必然同时出现的参数，它是**选择器**——选择器应该变成工具名（`analyze_trend` / `analyze_correlation`），让模型在"选工具"这一步决策，而不是在"填参数"这一步。

### 3.5 并行调用工具什么时候安全、什么时候不安全

安全性取决于两件事：① **可交换性**——并发操作交换顺序后结果是否相同（`datetime` + `web_search` 可交换；扣库存和下单不可交换）；② **共享资源**——是否争用同一个不可重入的资源（文件、DB 连接、**同一个 MCP stdio 管道**）。

这个项目 `base.go:344` 是严格的顺序 `for`，**从不并行**。所以上面两个问题目前都没暴露——但这个"安全"是被动的，不是设计判断的结果，而是性能优化没做。真要加并行，有一条必然踩的坑：`mcp/client.go:156-190` 的 `CallTool` 全程无锁，底层 stdio 是"写一行 → 读一行"（`stdio_transport.go:84-94`）。两个 goroutine 同时进出会发生：`client.go:259` 的 `c.requestID++` 数据竞争；以及**响应串线**——A 和 B 的请求都写进同一个管道，A 读到 B 的响应。它**不会报错**（JSON 解析得过），只是内容不对，表现为"工具返回了另一个工具的结果"，极难排查。所以正确顺序是：**先修传输层互斥，再加 errgroup**。stdio 的物理通道只有一条，串行化本来就是正确选择；`http_transport.go:69-71` 的 `sessionID` 无锁写也要一起处理。

两个实现细节：**保序回填**——`tool_calls` 顺序是模型生成的，回填 `tool` 消息应按原顺序（或按 `tool_call_id` 明确对应），否则同样的输入产生不同的历史，影响可复现性和 prompt cache。**部分失败语义**——顺序执行时第一个错就 `continue`，逻辑简单；并行时要决定一个失败是否取消其他，建议**不取消**（只读工具并发时，部分成功的信息量比全部丢弃大，把失败写成 `tool` 消息里的错误文本让模型自己决定）。

**明确不该并行的情况**：工具之间有数据依赖（第二个工具的入参来自第一个的结果）。这时模型**不会**一轮返回两个——它还没看到第一个的结果。如果它真的一轮返回了有依赖的两个，说明描述写得让模型以为参数可以自己估，应该改描述，而不是加并行。

---

## 四、MCP：这个项目做得对和做错的地方
前面三节讲的是"自己写循环"要处理什么。MCP 解决的是另一个问题：**工具不该长在你的进程里**。一旦工具是远程的、别人写的、能独立升级的，就需要一个协议来问"你有什么工具""这个工具怎么调""结果是什么"。这个项目手写了 MCP 客户端（没用官方 SDK），因此把协议细节全暴露了——这对面试是好事。

### 4.1 做对的：完整握手流程

```go
// pkg/mcp/client.go:98-131（节选）
params := map[string]interface{}{"protocolVersion": "2024-11-05", "capabilities": ..., "clientInfo": ...}
if err := c.call("initialize", params, &result); err != nil { return err }
_ = c.notify("notifications/initialized", nil)   // ← 第二步不能省
return c.listTools()                             // ← 第三步
```

三步各有理由：`initialize` 协商协议版本与双方能力；`notifications/initialized` 是**规范要求客户端必须发的**，服务端收到它才进入正常操作状态；`tools/list` 才能拿到工具清单。**省掉 initialized 会怎样**：部分 server（尤其 TypeScript SDK 实现的）在收到它之前会拒绝 `tools/list` 和 `tools/call`。这个项目做对了，是手写 MCP 客户端里少见的正确顺序。

**第二处做对的细节**：`notify` 走 `SendNotify`（`client.go:300-315`）而不是 `Send`。因为 notification **没有 `id`**，服务端按规范不会回复；走 `Send` 会永久阻塞在 `ReadString('\n')` 上——而且是在**连接阶段**挂死，不是调用阶段。

**该做得更好的**：`protocolVersion` 写死 `"2024-11-05"`（`client.go:100`），没有版本协商。规范要求服务端返回不支持的版本时**断开连接**，现在只是把 `serverInfo` 记进日志（`client.go:120-124`）。

### 4.2 做对的：Transport 抽象

```go
// pkg/mcp/transport.go:7-22
type Transport interface {
	Connect(ctx context.Context) error
	Send(request []byte) ([]byte, error)
	SendNotify(data []byte) error
	Close() error
	IsAlive() bool
}
```

**没有它会出什么问题**：ReAct 循环里就得写 `if transport == "stdio" {...} else if http {...}`，每加一种传输都要动循环。有了这层，`client.go:70-78` 按配置选一个实现，上层无感。

真正体现价值的是 `adapter.go`——MCP 的 schema 形状（`inputSchema`）和内置工具的形状（`map[string]ParameterDef`）不同，`adapter.go:36-57` 做转换后，**`base.go` 一行都没改就同时支持内置工具和远程工具**：`base.go:316` 的 `GetToolDefinitions()` 和 `base.go:349` 的 `Get(name)` 都只认接口，不关心来源。`factory.go:159-169` 把 MCP 工具注册进同一个 `tool.Manager`，Skill 的工具注入也能以名字引用 MCP 工具（`skill_enhancer.go:54-58` 的 fallback 分支）。**这是整个项目结构上最干净的一处**，值得主动讲。

**边界（主动标注会显得成熟）**：`Send([]byte) []byte` 是严格请求-响应式的，这个签名把 MCP 的另外三种能力挡在门外了——服务端主动请求（sampling / roots）、进度通知、日志通知。而且 `client.go:279` 直接把 body 交给 `json.Unmarshal`，`http_transport.go:54` 却声明接受 `text/event-stream`——**真实的 Streamable HTTP server 返回 SSE 时会解析失败**。

### 4.3 做错的：IsAlive 恒真

```go
// pkg/mcp/stdio_transport.go:117-123
func (t *StdioTransport) IsAlive() bool {
	if t.cmd == nil || t.cmd.Process == nil { return false }
	// 如果进程已退出，ProcessState 非空
	return t.cmd.ProcessState == nil
}
```

**Go 的机制**：`exec.Cmd.ProcessState` 只在 **`Wait()` 返回之后**才被赋值，它表示"这个子进程已经被收尸"，**不表示"进程还活着"**。全仓没有任何一处调用 `t.cmd.Wait()`（`Close` 在 `stdio_transport.go:106-114` 只做 `stdin.Close()` + `Process.Kill()`），所以 `ProcessState` 永远是 `nil`，`IsAlive()` 永远返回 `true`。

**后果链**：① `adapter.go:64` 的 `if !a.client.IsAlive()` 永不进入 → 重连逻辑是死代码；② 子进程崩溃/被 kill 后，`Send` 的 `Fprintf(t.stdin, ...)` 返回 `file already closed`（`stdio_transport.go:84-86`），每次调用都失败；③ **永久性的**——没有任何机制会重新拉起这个进程，该 Agent 的这个 MCP 工具就此报废。

**第二个后果，Unix 上的僵尸进程**：`Close` 只调 `Process.Kill()` 从不 `Wait()`，子进程退出后没人收尸会变成僵尸进程堆积。`Kill()` 也只杀直接子进程不杀进程组，`go run` 这种会再 fork 的场景会留孤儿。项目在 Windows 上开发，表现为句柄泄漏，所以没人注意到。

```go
// 修法：Connect 里 Start() 之后启动收尸 goroutine（stdio_transport.go:65 后）
go func() { if err := t.cmd.Wait(); err != nil { log.Debug("[MCP]", "exited: %v", err) }; close(t.done) }()
// IsAlive 改成读进程状态而不是读 ProcessState
select { case <-t.done: return false; default: return true }
```

这样 `Close` 之后再调用也能正确报告不存活，而不是像现在这样返回 `true` 然后在 `Send` 里报 `file already closed`。

**注意一个反模式**：不要用"发个 ping 看有没有响应"替代 `Wait`。HTTP transport 的 `IsAlive`（`http_transport.go:133-159`）就是这么做的——2 秒超时的 ping，看起来更"正确"，但问题在于 stdio 的 `Send` **没有超时**（`stdio_transport.go:89` 的 `ReadString` 没有 deadline），探活本身会把调用方挂死。**进程存活检测应该读本地状态，不是发远程请求。**

### 4.4 做错的：热更新会杀掉新 Agent 的 MCP 连接

这是一条**必然发生的时序 bug**，不是竞态——单线程顺序执行就会命中。

```
第一步：registry.go:179  r.factory.CreateAgentFromConfig(newConfig)
        factory.go:110   f.mcpPool.ConnectAgentServers(ctx, cfg.ID, cfg.McpServers)
        pool.go:29-33    p.mu.Lock(); disconnectAgentServersLocked(agentID)  ← 清掉旧连接
        pool.go:55       p.clients[agentID] = agentClients                    ← 新连接存进同一个 key
第二步：registry.go:196  go r.gracefulDestroy(oldEntry, agentID)
        registry.go:211  r.factory.DestroyAgent(agentID)
        factory.go:175   f.mcpPool.DisconnectAgentServers(agentID)            ← 按 agentID 删除
        pool.go:102      delete(p.clients, agentID)
```

**根因是 key 的设计**：pool 的 key 是 `agentID`（`pool.go:13-15` 的 `map[agentID]map[serverName]*Client`），新旧两代 Agent 共用同一个 key，任何"按 agentID 清理"都必然命中**新一代**的 client。所以 `gracefulDestroy` 关掉的是新 Agent 的 MCP 连接。

**为什么表现是"永久失效"**：叠加 4.3——`IsAlive()` 恒真，`adapter.go:64` 的重连分支永不进入。而 `adapter.go:66-67` 那个**空的 if 体**（重连失败静默吞掉，然后照常 `CallTool`）让这个故障连日志都留不下。

**修法**：key 换成 generation 而不是 agentID（如 `agentID + ":" + instanceSeq`），`DestroyAgent` 收具体的 `[]*Client` 或 generation。核心原则：**当一个资源的所有权属于某个实例时，清理的 key 也必须是实例级的。**

**边界**：内置工具不受影响——`factory.go:211-218` 的 `GetToolFactories` 每次返回**新实例**，不存在共享状态。所以故障表现是"MCP 工具坏了，`calculator` 还能用"，这个差异本身就是定位线索。

### 4.5 做错的：CallTool 无锁并发

```go
// pkg/mcp/client.go:258-276（节选）
func (c *Client) call(method string, params interface{}, result interface{}) error {
	c.requestID++                                          // ← 无锁自增
	req := Request{JSONRPC: "2.0", ID: c.requestID, Method: method, Params: params}
	data, err := json.Marshal(req)
	respData, err := c.transport.Send(data)                // ← 无锁写读管道
```

`c.mu`（`client.go:18`）只在 `Connect`（`client.go:63-64`）和 `Reconnect`（`client.go:220-221`）里被持有，`CallTool`（`client.go:156-190`）→ `call` **完全不持锁**。两个后果：`requestID++` 数据竞争（`go test -race` 必报）；stdio 下"写一行读一行"（`stdio_transport.go:84-94`）在并发时**串响应**且不报错。**这不是理论问题**：Gateway 的 `Process`（`gateway.go:63`）是被并发调用的，同一个 Agent 的同一个 MCP client 会被多个 session 同时使用。

**修法**：① `requestID` 改 `atomic.Int32`；② 在每个 transport 内部加 `sync.Mutex` 把 `Send` 串行化——**对 stdio 来说串行化不是妥协而是正确选择**，物理通道只有一条，并发本来就是假的；③ `sessionID` 纳入同一把锁。边界：若某个 server 的工具确实需要高并发，正确做法是让 HTTP transport 走真正的连接复用（HTTP 天然支持并发），stdio 保持串行。

### 4.6 安全：etcd 推的 command 直接进 exec.CommandContext

**链路**：`config.go:47` 的 `McpServers []ServerConfig` 是 `AgentConfig` 的字段 → etcd watch 到变更后 `dynamic/manager.go:120-141` 触发 Agent 更新 → `registry.go:179` → `factory.go:110` → `stdio_transport.go:37`：

```go
// pkg/mcp/stdio_transport.go:36-42
t.cmd = exec.CommandContext(ctx, t.command, t.args...)
for k, v := range t.env { t.cmd.Env = append(t.cmd.Env, fmt.Sprintf("%s=%s", k, v)) }  // 只追加不覆盖
```

⚠️ **结论**：任何能写 etcd 该前缀的人 = **在 Agent 主机上以 Agent 进程权限执行任意命令**。没有命令白名单、没有路径限制、没有 env 清理（只追加不覆盖）、没有资源限制（CPU / 内存 / 进程数 / 文件句柄）。

**这是"设计给的权限"，不是实现 bug**——MCP stdio 的本质就是"启动一个你配置的命令"。但它必须被主动承认，因为面试官看到 `exec.CommandContext` + 远程配置来源一定会问。缓解措施按成本从低到高：① **配置来源加校验**（写入侧签名、Agent 侧验签后再进 `exec`）；② **命令白名单**（只允许固定绝对路径，甚至校验文件哈希，`args` 限制长度和字符）；③ **容器化执行**（每个 server 一个容器/pod，限制资源与网络，顺带解决进程组和僵尸问题）；④ **权限收敛**（etcd 该前缀只给平台侧写，业务方通过平台 API 提交配置）。

第 4 条最有效——**"谁能改配置"就是"谁能在你机器上执行命令"**，这个等式必须显式化。另外恶意 server 可以 fork 出守护进程存活到 Agent 重启之后，因为 `Close()`（`stdio_transport.go:106-114`）只杀直接子进程。

---

## 五、Skill 与 Tool 的边界

### 没有这个区分会出什么问题

如果 Skill 也有自己的 `Execute`，那调用一个 Skill 就变成"在一个工具里再跑一遍 ReAct 循环"。后果是：嵌套循环（10 × 10 轮）、上下文翻倍、无法中断、错误无法归因、两层重试语义冲突。**Skill 一旦有执行体，你就有两个控制流要维护。**

### 这个项目的判断：Skill 是数据，不是代码

`Skill` 接口（`skill.go:14-35`）只有 7 个 getter：`GetID / GetName / GetDescription / GetSystemPrompt / GetToolRefs / GetExamples / GetLastModified`。**没有 `Execute`。**

```go
// pkg/skill/skill_enhancer.go:26-42（节选）
func (e *Enhancer) EnhanceAndExecute(ctx context.Context, ag agent.Agent, skill Skill, userInput, sessionID string) (string, error) {
	e.injectTools(ag, skill)                     // 1. 把 Skill 声明的工具注入 Agent
	skillPrompt := e.buildSkillPrompt(skill, ag)  // 2. Skill 的 SystemPrompt + 工具使用指引
	response, err := ag.ChatWithSkill(ctx, userInput, sessionID, skillPrompt) // 3. 复用 ReAct
```

`ChatWithSkill`（`base.go:185-206`）和 `ChatWithUser`（`base.go:152-180`）的唯一区别是 system prompt 的来源——两者都调 `chatWithTools`（`base.go:195` / `base.go:166`）。**整个项目只有一个 ReAct 循环。**

**为什么这个判断是对的**：① 控制流只有一处，Skill 带来的行为差异全通过 system prompt 和工具集合表达，调试时只需看一条消息历史；② 权限是显式的——Skill 的能力上限 = `ToolRefs` 声明的工具，审计不用读代码；③ Skill 是纯数据（`loader.go:31-44` 走 `filepath.Walk` 找 `SKILL.md`，ID 取自目录名 `loader.go:60-64`），格式对齐 Anthropic 的 SKILL.md，可推送可版本化，不需要重新编译；④ 模型决策空间没有嵌套，Skill 只是换了开场白和可用工具。

**但注意这个判断的前提**：Skill 只能表达"prompt + 工具白名单"。**需要确定性步骤时**（先调 A、按返回值判断再调 B、失败重试三次），Skill 表达不了——这段逻辑只能写在 prompt 里让模型"照着做"，而模型不保证照做。这个项目用另一个引擎承接：`pkg/workflow/core/executor.go:27` 的图执行（串行节点 + 条件边 + `patterns/loop_node.go` 的循环 + `patterns/interrupt_node.go:11-31` 的人工中断）。**两个引擎分开、各管一类需求，这个架构判断是对的。**

### 缺陷：`{{var}}` 没有任何模板渲染

这是最容易被追问到失分的地方。`skills/parenting-activity-advice/SKILL.md` 里写了大量变量语法：`city: "{{city}}"`、`expression: "2026 - {{child_birth_year}}"`、`用户问题: {{user_input}}`、`天气信息: {{tool_get_weather}}`。

**全仓 Go 代码里没有任何模板渲染实现**——对 `{{` 的 grep 只命中一处，是 Go 的复合字面量语法（`manager.go:197` 的 `[]llm.Message{{Role: ...}}`）。`loader.go:76` 把 front matter 之后的所有 Markdown 正文原样赋给 `SystemPrompt`：

```go
// pkg/skill/loader.go:75-76
// 剩余的 Markdown 正文作为 SystemPrompt
skill.SystemPrompt = strings.TrimSpace(content)
```

所以 `{{city}}` **原样进了 system prompt**。那段"执行步骤"不是可执行代码，是**给模型看的文字建议**；`{{user_input}}` 不会被替换，模型看到的是字面量花括号，然后自己理解这段模板的意图、从真实对话里找输入。

**被问"变量怎么注入"时，正确答案是**："设计上留了模板语法，但实现上没有渲染器——它靠模型自己理解。要真正支持，得在 `Enhancer.buildSkillPrompt`（`skill_enhancer.go:67-86`）里加一层渲染。"答"有模板引擎"是致命失分。

**一个更严重的连带问题**：`{{tool_datetime}}` / `{{tool_get_weather}}` 这种"把工具结果填进 prompt"的意图，在当前实现里**根本不可能工作**——工具结果只在循环局部变量 `currentMessages` 里（`base.go:377-380`），而 system prompt 在循环**开始之前**就构建好了（`base.go:303`）。要在 prompt 里引用工具结果，需要两阶段执行（先跑一轮拿数据，再填模板跑第二轮），这与当前单循环结构不兼容。**这是设计层面的缺口，不只是少写了一个渲染函数。**

### 缺陷：Skill 热更新是死代码

```go
// pkg/skill/manager.go:86-94
reloadedModTime := reloaded.GetLastModified()
if oldModTime, exists := m.skillsModTime[id]; exists && reloadedModTime.After(oldModTime) {  // ← exists 永假
	m.mu.Lock(); m.skills[id] = reloaded; m.skillsModTime[id] = reloadedModTime; m.mu.Unlock()
	return reloaded, true
}
return skill, true
```

条件是 `exists`——**要求 key 已存在**，而 `m.skillsModTime` 唯一的写入口就在这个 if 体内（`manager.go:90`），`LoadAll`（`manager.go:40-52`）和 `Register`（`manager.go:55-59`）都不写它。所以第一次 `Get` 时 `exists == false` 不进分支、不写；第二次仍是 `false`。**`exists` 永远不成立，热更新永不生效**，代价是每次 `Get` 多一次 `os.Stat`（`manager.go:81` → `loader.go:144`）。

修法很直接：`LoadAll` 里顺便写 `m.skillsModTime[id] = skill.GetLastModified()`；或者删掉 manager 层这个冗余判断——`loader.go:149` 的 `ReloadSkill` 已经做了 `if !info.ModTime().After(baseSkill.LastModified) { return nil, nil }`。

### 缺陷：关键词匹配对中文完全失效

```go
// pkg/skill/manager.go:163-173
func keywordMatch(input, desc string) bool {
	words := strings.FieldsFunc(desc, func(r rune) bool {
		return r == ' ' || r == ',' || r == '，' || r == '.' || r == '。' || r == '/'
	})
	for _, w := range words {
		if len([]rune(w)) >= 2 && strings.Contains(input, w) { return true }
	}
	return false
}
```
分隔符是 ASCII 空格和几种中英文标点，而 SKILL.md 的 description 是"根据天气和日期推荐适合的亲子活动"——**没有空格逗号**，`FieldsFunc` 返回**整段描述作为一个 token**，判断变成 `strings.Contains(input, "根据天气和日期推荐适合的亲子活动")`，用户输入几乎不可能包含这一整句。**关键词路径实质上永不命中。**

再叠加两个因素：Skill 的 `Name` 是英文（`parenting-activity-advice`）而用户输中文；`examples` 在 front matter 里为空。`MatchSkills`（`manager.go:141-161`）的三条路径——name 匹配、描述关键词匹配、example 匹配——**全部失效**。所以实际全部依赖 LLM 匹配（`manager.go:176-216`），而它是**每条消息一次额外 LLM 调用**（`manager.go:197`），无缓存、无 embedding 路由，每个请求的延迟和成本翻倍。

**最坏的形式不是"没有关键词匹配"，而是"有但不起作用"**：读代码的人会以为存在"关键词优先、失败降级 LLM"的两级结构（`MatchSkillsHybrid`，`manager.go:219-227` 也确实这么写的），实际只有一级——这比纯粹没有更危险，因为它让容量估算和成本分析都做错。

### 另外三个小缺陷

- **`injectTools` 直接污染共享 Agent**（`skill_enhancer.go:45-63`）：往共享 Agent 的 toolManager 注册工具，**注册了不撤销**。触发过带 `get_weather` 的 Skill 后，该 Agent 后续所有普通对话都带上这些工具——而且 `base.go:164` 的 `if a.toolManager.Size() > 0` 判断也会因此改变走向（本该走 `chatDirectly` 的变成走 ReAct）。
- **`SkillRefs` 只被当布尔开关用**（`gateway.go:135-136`、`cmd/stage2/main.go:335-336`）：两处都只判断 `len(agentCfg.SkillRefs) > 0`，**没有用它筛选哪些 Skill 可参与匹配**。配置了 A Skill 的 Agent 也会被 B Skill 命中。工具描述被注入两次（见 2.2）。

---

## 六、换个场景会怎样（横向对比）

| 场景 | 这个项目的做法 | 更好的做法 | 代价 |
|---|---|---|---|
| **工具数量上百**（20 个 MCP server，400 个工具） | 每轮把全部工具塞进 `tools` 数组（`base.go:316` → `manager.go:79` 遍历所有） | 工具检索：embedding 召回 top-K；分层（先选 server 再选工具）；按 Skill/Task 切分工具组 | 多一次检索调用的延迟；**召回失败时模型"看不到"工具**——比选错更难排查（它不会说"我缺个工具"，而是用错的工具或直接编） |
| **需要人工确认**（删除/转账/下单前） | 任何工具都无条件执行（`base.go:360`）；唯一的"人在回路"在 `patterns/interrupt_node.go:11-31`，ReAct 循环完全没接 | 工具元数据加 `SideEffect` / `RequiresApproval`；执行前挂起——存 checkpoint、返回"待确认"、批准后带 `tool_call_id` 继续 | 需要**持久化循环中间状态**（现在 `base.go:306-308` 用完即弃，做不到）；需要 out-of-band 的恢复通道和超时清理；"一次请求"变成"两个独立 API 调用" |
| **需要并行工具** | 顺序 `for`（`base.go:344`），从不并行 | 只读工具 `errgroup` 并发 + 保序回填 + 每工具独立超时；有副作用工具按资源键分桶串行 | 并发度受传输层限制——**必须先修 `client.go:156-190` 的无锁并发**；错误语义从"第一个错就 continue"变成"部分成功" |
| **需要工具级超时** | 结构上不可能：`Tool.Execute` 不带 ctx（`tool.go:15`）；MCP adapter 用 `context.Background()` 切断调用方 ctx（`adapter.go:61`）；`cmd/stage1/main.go:189` 传的也是 Background | `Execute(ctx, args)` 签名 + 每工具 `defaultTimeout` 元数据 + MCP 侧用带 deadline 的 ctx | 破坏性接口改动，波及 4 个内置工具 + adapter + factory；**超时无法取消已在子进程里跑的操作**——取消要在 MCP 层发 `notifications/cancelled`，协议层没实现 |
| **需要多轮记忆** | 上限 50 硬编码（`base.go:51`），超限丢最旧（`conversation.go:91-93`），无摘要；循环内工具结果不落库（`base.go:306-308`）；`AgentMemoryConfig` 四个字段全仓零消费者 | 分层记忆（近期原文 + 中期摘要 + 长期向量检索）；**工具结果单独存储**，只回喂摘要 + ID，模型按需取全文 | 摘要本身是 LLM 调用（延迟 + 成本 + 失真会累积）；向量检索引入新失败模式；"按需取"需要新工具，又回到工具选择问题 |
| **需要流式输出** | 全仓无流式（`openai.go:99` 用 `CreateChatCompletion` 而非 stream）；只有 `http_transport.go:54` 声明 SSE 且不解析 | 循环按事件流推送：iteration start / tool call / tool result / final token，前端能显示"正在调用 web_search" | 要把 `llm.Client` 从"返回完整响应"改成 channel/回调，循环改成事件驱动；**工具调用轮次里模型通常没有 token 可流**（content 为空），流式主要改善最终回答那段 |
| **换严格接口**（OpenAI / Azure / 严格 vLLM） | 立刻炸：孤儿 `role:"tool"` 消息（3.1）→ 400；参数解析错误被吞（`openai.go:120`）；空响应当成功（`openai.go:69-71`） | 先用协议校验器跑一遍历史；给 `Message` 加 `ToolCalls` / `ToolCallID`，`Interaction` 同步结构化 | 改动集中在 `llm` 包（小），但**必须连带改 `memory` 包**，否则重建后照旧畸形；还要处理 `content: null` 与 `""` 的差异 |
| **需要成本可观测** | `log.go:346` 有 `LLMResponse(model, tokens)` 但**全仓零调用**；`metrics.go:60` 的 `ToolCallsTotal` 在 ReAct 路径是死代码（`base.go:360` 绕过 `manager.go:108-128`） | 在 `openai.go` 的响应处理里读 `resp.Usage`，按 Agent / session / model 打 label；工具计数挪到 `base.go:360` 旁边 | `Usage` 不是所有兼容接口都返回（早期 Ollama 没有），要空值兜底；按 session 打 label 会让标签基数爆炸，需要抽样或聚合 |

**读法**：留意"代价"列反复出现的三个主题——**状态持久化（人工确认、多轮记忆）、接口签名变更（工具超时、流式）、传输层约束（并行、成本）**。这三件事是所有 Agent 层改造的共同前置条件；回答"让你重构你先做什么"时，从这里挑切入点比泛泛说"加缓存加监控"实得多。

---

## 七、面试要点（12 个高频问题）

### Q1: Function Calling 的原理是什么？模型真的执行了你的代码吗？
**骨架：** ① 模型只输出"调哪个函数、参数是什么"的结构化意图（`tool_calls`）；② **执行永远在你的代码里**（`base.go:360` 的 `t.Execute`）；③ 参数是 JSON **字符串**（`openai.go:119-120` 要 `json.Unmarshal`），不是对象；④ 结果作为一条**新的 `role:"tool"` 消息**回喂（`base.go:377-380`）；⑤ 所以"模型执行了代码"是彻头彻尾的误解——它连你的函数是否存在都不知道，只见过一个 schema。

### Q2: Agent 循环怎么终止？为什么"模型不返回 tool_calls"是正常出口？
**骨架：** ① 三个显式出口（`base.go:325` / `base.go:320` / `base.go:384`）；② 模型是自回归的下一 token 预测器，**没有内部状态表达"我够了"**，直接给 content 就是它唯一的停止表达；③ 所以出口①不是降级而是唯一的成功路径，别当异常处理；④ 三个出口语义不同，不该都映射成 500；⑤ 出口③（预算耗尽）应降级返回部分结果或转人工——那时模型通常已产出有用的中间内容。

### Q3: 工具调用的消息协议里 `tool_call_id` 有什么用？省了会怎样？
**骨架：** ① 它把 `tool` 消息和 `assistant.tool_calls` 里的具体某一条**绑定**——一轮可返回多个 tool_calls，靠 id 对应；② 严格接口会校验配对，缺了直接 400；③ 本项目丢两次：`client.go:8-11` 的 `Message` 没有 `ToolCalls` 字段，`base.go:338-341` 不带 `tool_calls`，`base.go:377-380` 不带 id；④ Ollama 容忍是因为它把 role 当模板槽位渲染、不校验配对——**本地跑通不代表协议正确**；⑤ 触发条件是"用过一次工具 + 第二轮会话重建"（`base.go:306` → `conversation.go:195-197`），单轮 demo 不暴露。

### Q4: 工具执行失败了，错误该返回给模型还是给用户？
**骨架：** ① 默认**回喂模型**——它能自己纠正（改参数、换工具、如实告知），比 500 好，`base.go:361-368` 这点做对了；② 但"回喂哪个字段"是关键：本项目回喂 `result.Data`（`base.go:379`），而工具失败时返回的是 `tool.Error(msg), nil` → **err 为 nil、Data 为空串**，`ToolResult.Error` 全仓无读者；③ 正确做法：回喂前判断 `result.Success`，把 `Error` 文本放进 `tool` 消息；④ 区分错误类型：参数错误（回喂模型）、权限/额度错误（回喂但不重试）、基础设施错误（回喂 + 告警）；⑤ 重试要有上限，否则模型会用同一个错参数循环到 `maxIterations`。

### Q5: 怎么让模型选对工具？描述该怎么写？
**骨架：** ① `description` 是模型能看到的**唯一路由表**，没有别的信号（`client.go:27-37` 的 `ToolDefinition` 只有 name / description / parameters）；② 要写**触发条件**（"当用户提到今天/明天/这周末时调用"——`datetime.go:27` 是对的），不只是功能；③ 要写**反触发条件**（"不要用它算含括号的表达式"），否则模型会拿 `calculator` 算 `(2+3)*4` 而 `calculator.go:57-91` 不支持括号；④ 描述必须和实现一致——`websearch.go:28` 声称"搜索网络信息"但实现是 `rand` 假数据，是反面教材；⑤ 描述再准也不能替代参数校验。

### Q6: JSON Schema 里的 `required` 到底起什么作用？
**骨架：** ① 它是**模型侧唯一的必填信号**——没有 `required`，模型眼里所有字段都是可选的；② 本项目在 `manager.go:83-88` 丢掉了 `Required`，MCP 侧 `adapter.go:36-57` 也没读 `inputSchema["required"]`；③ 后果链：模型漏参 → 工具返回 `tool.Error("xxx不能为空")` → 因 err 是 nil，`base.go:377-380` 回喂空串 → 体验是"工具没反应"而不是"缺参数"；④ 修的时候注意 `GetParameters()` 返回 map、**遍历顺序随机**，`required` 要用 `[]string` 聚合；改 adapter 时 struct 是值类型，**必须写回 map**（`adapter.go:43-50`）；⑤ 边界：`required` 只降低概率，执行侧断言不能省。

### Q7: 工具粒度怎么定？一个工具做多件事有什么问题？
**骨架：** ① 判据不是"功能单一"，而是**"这组参数能否在一次模型决策里被完整确定"**；② 参数是"选择器"就是信号——`dataanalyzer.go:38-42` 的 `analysisType` 四选一，等于把内部路由塞进参数；③ 选择器应该变成工具名（`analyze_trend` / `analyze_correlation`），让模型在"选工具"这一步决策；④ 同项目的成功例子是 `calculator` 单参数 `expression`（`calculator.go:30-38`）——"算"和"说"分开是关键；⑤ 反过来，`web_search` 把四个领域分支塞进一个工具（`websearch.go:62-73`），路由不可预测，本该是四个数据源。

### Q8: 什么时候可以并行调用工具？
**骨架：** ① 两个条件：**可交换性**（通常意味着只读）+ **无共享资源**（不争同一个管道/文件/连接）；② 本项目从不并行（`base.go:344` 是顺序 for），所以问题没暴露；③ 真要加，有一条必然踩的坑——`mcp/client.go:156-190` 的 `CallTool` 无锁，stdio 是"写一行读一行"，并发会**串响应且不报错**；④ 正确顺序是"先修传输层互斥，再加 errgroup"；⑤ 实现细节：保序回填（否则历史顺序不可复现），并明确"部分失败不取消其余"的语义。

### Q9: MCP 的握手流程是什么？为什么 `initialized` 通知不能省？
**骨架：** ① 三步：`initialize`（协商协议版本 + capabilities）→ `notifications/initialized` → `tools/list`（`client.go:98-131`）；② `initialized` 是规范要求客户端**必须发**的，服务端收到它才进入正常操作状态，部分实现（TypeScript SDK）在此之前会拒绝 list/call；③ 本项目还做对了另一处——notification 走 `SendNotify` 而不是 `Send`（`client.go:300-315`），因为 notification **没有 id**，服务端不会回复，用 Send 会永久阻塞在 `ReadString` 上；④ 该做没做的：`protocolVersion` 写死 `"2024-11-05"`（`client.go:100`），服务端返回不支持的版本时规范要求断开，现在只记日志。

### Q10: MCP stdio 模式下怎么判断子进程还活着？
**骨架：** ① 不能读 `cmd.ProcessState`——Go 里它只在 **`Wait()` 返回之后**才被赋值，表示"已收尸"而不是"还活着"，所以 `stdio_transport.go:117-123` 的 `IsAlive()` 恒真；② 全仓没有一处 `cmd.Wait()` → 崩溃检测和重连永不触发，`adapter.go:64-68` 的重连分支是死代码；③ 连带后果：Unix 上**僵尸进程**堆积（没人收尸），`Kill()` 也只杀直接子进程不杀进程组；④ 修法：单独 goroutine 里 `cmd.Wait()`，把结果写进 channel/atomic，`IsAlive` 读它；⑤ 反模式提醒：**别用"发个 ping 看有没有响应"替代 Wait**——`stdio_transport.go:89` 的 `ReadString` 没有 deadline，探活会把调用方挂死。

### Q11: Skill 和 Tool 的边界在哪？为什么 Skill 不该有执行体？
**骨架：** ① Tool 有 `Execute`（被模型调用）；**Skill 没有执行体**，Skill = 专业 SystemPrompt + 工具引用 + few-shot（`skill.go:14-35`）；② 执行时注入工具、覆盖 system prompt、**复用同一个 ReAct 引擎**（`skill_enhancer.go:26-42` → `base.go:195`）；③ 有执行体的代价：嵌套循环（10×10）、上下文翻倍、无法中断、错误无法归因；④ 这个判断的好处：控制流只有一处、权限显式（`ToolRefs`）、Skill 是纯数据可热加载；⑤ 边界：Skill 表达不了确定性步骤，那类需求归 `pkg/workflow` 的图引擎（`core/executor.go:27` + `interrupt_node.go`），分工明确是对的；⑥ 主动坦白：`SKILL.md` 的 `{{var}}` 没有渲染器，`loader.go:76` 把正文原样当 prompt，`{{tool_get_weather}}` 在当前单循环结构下**根本做不到**（prompt 在循环开始前就构建了，`base.go:303`）。

### Q12: 从 Ollama 换到 OpenAI 官方接口，这个 Agent 会先在哪里炸？
**骨架：** ① 第一个炸点是**多轮会话的第二轮**——孤儿 `role:"tool"` 消息（`conversation.go:195-197`），严格接口直接 400，且只要用过一次工具就可复现；② 第二个是**静默错误**：`openai.go:120` 吞掉 `json.Unmarshal` 的 error，解析失败时 `args` 是空 map，工具收到空参数返回 `tool.Error` → 又被 `base.go:379` 回喂成空串；③ 第三个是**空响应当成功**：`openai.go:69-71` 返回 `("", nil)`，`base.go:325-328` 当最终答案，用户看到空白回答且无任何错误——更糟的是 `cmd/stage1/main.go:186` 用 `if response == ""` 判断"Skill 未命中"，会**再触发一次完整对话**（双倍 LLM 调用）；④ 第四个是**协议形状**：assistant 带 `tool_calls` 时 `content` 应为 `null` 而非 `""`（`openai.go:130-133` 原样返回空串）；⑤ 回答这类问题要先说"在哪一层"，再说"怎么验证"——拿一段真实历史跑协议校验器，或在 `ChatWithTools` 前断言消息配对。
