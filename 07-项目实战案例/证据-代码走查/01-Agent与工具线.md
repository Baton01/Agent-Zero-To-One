# 代码走查情报：Agent 与工具调用线

> **这份文件是什么：** 对 `bitguide-agent-platform-go` 的 `pkg/agent`、`pkg/llm`、`pkg/tool`、`pkg/mcp`、`pkg/skill` 五个包逐文件走查的结果。
> **为什么要有它：** 后面所有的面试问答、简历表述都**必须能落到具体文件和行号**。面试官追问「你在哪一行看到的」时，答得出来才是真读过代码。
> **行号约定：** 相对项目根，格式 `文件:行号`。

---

## 一、Agent 主循环（ReAct）

### 事实

- 循环唯一实现：`pkg/agent/base.go:295` 的 `chatWithTools`。分流在 `base.go:164-170` —— `toolManager.Size() > 0` 才走 ReAct，否则走 `chatDirectly`（`base.go:225`）。
- **三个终止条件**：
  1. `base.go:325` 模型没返回 tool_calls → 返回 `response.Content`（正常出口）
  2. `base.go:312` 跑满 10 轮 → 返回 error（`base.go:384`）
  3. `base.go:320-322` LLM 报错 → 直接返回 error
- **工具描述不在 system prompt 里**，而是每轮循环重新生成 `tools` 数组（`base.go:316` → `pkg/tool/manager.go:75-105`）。
- 消息历史：`pkg/memory/conversation.go`，`maxSize=50` **硬编码在 `base.go:51`**，超限丢最旧一条（`conversation.go:91-93`），**无摘要压缩**。
- `ChatWithUser` 只把「用户输入 + 最终回答」写回记忆（`base.go:158,177`），循环内的中间消息不落库。

```go
// pkg/agent/base.go:296-328
maxIterations := 10
for i := 0; i < maxIterations; i++ {
    toolDefs := a.toolManager.GetToolDefinitions()   // 每轮重建工具定义
    response, err := a.llmClient.ChatWithTools(ctx, currentMessages, toolDefs)
    if err != nil { return "", fmt.Errorf("LLM 调用失败: %w", err) }
    if len(response.ToolCalls) == 0 {                 // 出口①
        return response.Content, nil
    }
```

### 关键缺陷

**① 工具调用消息协议不合法（最致命）**

`pkg/llm/client.go:8-11` 的 `Message` 结构体**只有 `Role` 和 `Content`** —— 没有 `ToolCalls` 字段，也没有 `tool_call_id`。

```go
// pkg/agent/base.go:338-341 —— assistant 消息回填时丢掉了 tool_calls
currentMessages = append(currentMessages, llm.Message{
    Role:    "assistant",
    Content: response.Content,      // tool_calls 没带
})
```

`conversation.go:78-83` 的 `AddToolCall` 把**工具名当内容**存成 `type=tool` 的交互记录，`BuildMessages`（`conversation.go:195-197`）又把它映射成 `role:"tool"`。

后果：**只要发生过一次工具调用，后续任意一轮历史里都会出现孤立的 `role:"tool"` 消息**（content 是工具名、没有 tool_call_id、前面没有对应的 assistant tool_calls）。Ollama 容忍这种畸形消息，OpenAI 官方接口会直接 400。

**② Agent 循环跑满 10 轮后留下悬空 user 消息**

`base.go:172-174` 直接 return error，`base.go:177` 的「写回 assistant 回复」不可达。但 `base.go:158` 已经把 user 消息写进历史了 → 下一轮会被重发，历史里留下一条永远没有回复的 user 消息。

**③ 没有 per-iteration 超时**

stage1/stage2 传的是 `context.Background()`（`cmd/stage1/main.go:189`、`cmd/stage2/main.go:348`）。LLM 卡住就是无限等。

**④ 循环内消息用完即弃**

`base.go:306-308` 每轮从记忆重建消息 → **多轮之间「上一轮的工具结果」不可见**，模型无法利用历史工具数据。

**⑤ 最大步数写死 10**

`base.go:296` 是局部变量，不是常量、不在 config 里。被问「为什么是 10」只能答「经验值」。

**⑥ 并发首次请求会创建两个 Session**

`getOrCreateSession`（`base.go:388-398`）是 `Load` 后 `Store` 的非原子操作，并发首次请求会创建两个 `SessionContext`，后写覆盖前者，**丢掉一个请求的上下文**。

---

## 二、工具系统

### 事实

```go
// pkg/tool/tool.go:4-16
type Tool interface {
    GetName() string
    GetDescription() string
    GetParameters() map[string]ParameterDef
    Execute(args map[string]interface{}) (*ToolResult, error)   // ← 不带 ctx
}
```

`Execute` 不带 `context.Context` —— 这是超时/取消**无法实现的结构性原因**。

**参数校验**：只有「工具内部自己断言类型」。`manager.go:75-105` 生成 JSON Schema 时**丢掉了 `Required`**：

```go
// pkg/tool/manager.go:82-98
params[name] = map[string]interface{}{
    "type":        param.Type,
    "description": param.Description,   // param.Required 没写进去
}
```

所以模型侧**完全不知道哪些参数必填**。`Required: true` 只是装饰。MCP 工具的 `required` 数组同样被丢（`pkg/mcp/adapter.go:36-57` 只解析 properties）。

**失败处理**：`base.go:359-368`，错误作为 `role:"tool"` 消息回喂模型（这点做对了），但——

四个内置工具的失败路径都是 `return tool.Error("..."), nil`（`tools/calculator.go:44`、`tools/websearch.go:51`、`tools/dataanalyzer.go:55`），**err 为 nil、Data 为空串** → `base.go:377-380` 塞给模型的是 `Content: ""`。**`ToolResult.Error` 在整个 ReAct 循环里没有任何读者。**

**没有超时、没有并发执行**：`base.go:344` 是顺序 `for`，没有 errgroup/goroutine。MCP 工具在 `adapter.go:61` 用 `context.Background()`，**把调用方的 ctx 彻底切断**。

### 四个内置工具的真实实现

| 工具 | 位置 | 真实度 |
|---|---|---|
| `calculator` | `tools/calculator.go:57-91` | 真实现。手写递归下降，按 `+ - * /` 切分。**无括号、无取模、无幂**；除零有检查（`:81-83`）；`0.1+0.2` 的浮点误差会直接暴露给模型 |
| `datetime` | `tools/datetime.go:42-100` | 真实现。时区 + 中文星期 + 今天/明天/后天/大后天 + 本周末/距周末天数。非法时区静默忽略（`:45-49`） |
| `web_search` | `tools/websearch.go:48-76` | **纯 mock**。按关键词（股票/AI/新能源/半导体）走 `rand` 生成假新闻，连「上证指数报 X 点」都是随机数（`:78-95`），**不发任何网络请求** |
| `data_analyzer` | `tools/dataanalyzer.go:52-83` | **纯 mock**。`data` 参数只被回显进报告头（`:87`），统计/趋势/相关性/建议全部 `r.Float64()` 随机（`:85-205`） |

**描述与实现严重不符**：`websearch.go:28` 写「搜索网络信息…返回相关搜索结果摘要」，`dataanalyzer.go:27` 写「对输入数据进行统计分析、趋势分析、相关性分析等，返回结论和建议」。

### 另一个坑：埋点写在死代码里

`tool.Manager.Execute`（`manager.go:108-128`，带日志、带 `metrics.ToolExecuteSuccess`）**在 ReAct 循环里根本没被调用** —— `base.go:360` 直接调 `t.Execute`。所以 `ToolExecuteStart/Success` 日志和 `metrics.ToolCallsTotal` 在 stage1/2 里是**死代码**。只有 stage4 的 `tool_node.go:46` 用它。

### 做得好的地方

- 工具失败**回喂模型让它自愈**（`base.go:363-366`）比直接 500 好。
- `factory.go:211-218` 的 `GetToolFactories` 让每个 Agent 拿**新实例**，避免共享状态。
- **MCP 工具通过 `adapter.go:12-23` 适配成同一个 `tool.Tool` 接口**，ReAct 循环零改动地同时支持内置和远程工具 —— 这是整个项目结构上最干净的一处。

---

## 三、LLM 客户端

### 事实

- 接口：`pkg/llm/client.go:40-52`（`Chat` / `ChatWithTools` / `Embed` / `EmbedBatch`）。
- `openai.go` = 具体实现；`gateway.go` = 按 `modelRef` 的多模型路由 + 客户端缓存（`sync.Map`，`:17`），构造时预创建（`:30-35`），**未知 ref 静默回落默认模型**（`:52-56`）。

### 关键缺陷

**① temperature / maxTokens 配了不用**

```go
// pkg/llm/openai.go:99-103
resp, err := c.client.CreateChatCompletion(ctx, openai.ChatCompletionRequest{
    Model:    c.model,
    Messages: reqMessages,
    Tools:    toolDefs,
    // MaxTokens / Temperature 都没有设置
})
```

`config.go:21-28` 定义了、`openai.go:19,37` 加载进了 `c.maxTokens`，**就是从不写进请求**。

**② 无超时 / 无重试 / 无熔断 / 无降级**

`openai.go:29-35` 用 `openai.DefaultConfig` + `NewClientWithConfig`，底层是 `http.DefaultClient`（**无 Timeout**）；embedding 用的 `httpClient: &http.Client{}`（`openai.go:42`）同样无 Timeout。config 里也没有 timeout 字段。

**③ 无流式输出**。全仓只有 `pkg/mcp/http_transport.go:54` 的 `Accept: text/event-stream`，与 LLM 无关。

**④ 无 token / 成本统计**。`pkg/log/log.go:346` 有 `LLMResponse(model, tokens)` 但**全仓无调用者**。

**⑤ 空响应当成功**

```go
// pkg/llm/openai.go:69-71
if len(resp.Choices) == 0 { return "", nil }   // 返回空字符串 + nil error
```

`base.go:325-328` 又把它当最终答案 → **用户看到空白回答且没有任何错误**。更糟：stage1 `cmd/stage1/main.go:186` 用 `if response == ""` 判断「Skill 未命中」，所以 skill 命中但返回空时会**再触发一次完整普通对话**（双倍 LLM 调用）。

**⑥ 工具参数解析错误被吞**

```go
// pkg/llm/openai.go:118-126
if tc.Function.Arguments != "" {
    json.Unmarshal([]byte(tc.Function.Arguments), &args)   // 返回的 error 被忽略
}
```

**⑦ 未知 modelRef 静默回落**（`gateway.go:52-56`、`factory.go:65-70`）—— 配置写错不报错，只会静默用错模型。

### 做得好的地方

- 多模型隔离正确：`LLMGateway` 自己实现 `Client` 接口委托默认模型（`gateway.go:98-118`），可直接当 `llm.Client` 传给 Agent（`cmd/stage2/main.go:100`）。
- embedding 不用 go-openai 而是**手写 HTTP**（`openai.go:155-236`），为了兼容任意非 OpenAI 模型名（`nomic-embed-text`）—— 这个取舍讲得出来是加分项。

---

## 四、MCP 协议

### 事实

**连接池**（`pkg/mcp/pool.go`）：`map[agentID]map[serverName]*Client` + `sync.RWMutex`（`:13-17`）。

**注意：这不是连接池，是 per-agent 连接注册表** —— 没有池大小、没有复用（同一 server 在不同 agent 下各起一个进程）、没有空闲回收、没有后台探活。`GetStatus`（`:116-128`）只列名字。

**两种 transport**：
- stdio（`stdio_transport.go`）：`exec.CommandContext` 起子进程，stdin/stdout 管道 + **换行分隔 JSON**（`:37,84-94`），stderr 异步打日志（`:70-76`）。
- http（`http_transport.go`）：`http.Client{Timeout: 30s}`（`:27-30`），**`Connect` 是空实现**（`:34-38`）。

### 关键缺陷（重灾区）

**① stdio 的 `IsAlive()` 永远返回 true**

```go
// pkg/mcp/stdio_transport.go:117-123
func (t *StdioTransport) IsAlive() bool {
    if t.cmd == nil || t.cmd.Process == nil { return false }
    return t.cmd.ProcessState == nil   // ← ProcessState 只有 Wait() 之后才非 nil
}
```

全仓**没有一处调用 `t.cmd.Wait()`**。所以进程崩溃、被 Kill、被 Close 之后 `IsAlive()` 一律返回 `true` → **崩溃检测和重连逻辑永远不触发**。`Close()` 之后再调用工具会得到 `发送请求失败: file already closed`。

**② 热更新会顺手杀掉新 Agent 的 MCP 连接**

`registry.go:179` 创建新 Agent 时 `ConnectAgentServers(agentID)` 先关旧连接、把新 client 存进**同一个 agentID key**（`pool.go:33,55`）；随后 `registry.go:196` 的 `go r.gracefulDestroy(oldEntry, agentID)` 等引用归零后调 `factory.DestroyAgent(agentID)`（`registry.go:210-212` → `factory.go:174-177` → `pool.DisconnectAgentServers(agentID)`）—— 关掉的是**新 Agent 的 client**。

因为第 ① 点 `IsAlive` 恒真，adapter 不会重连 → **热更新后该 Agent 的所有 MCP 工具永久失效**。

**③ 重连错误被静默吞掉**

```go
// pkg/mcp/adapter.go:64-68
if !a.client.IsAlive() {
    clog.Warn("[MCP]", "Server [%s] 连接已断开，尝试重连...", a.client.GetName())
    if err := a.client.Reconnect(ctx); err != nil {
    }        // 空的 if 体：重连失败无声吞掉，然后照常 CallTool
}
```

**④ 并发不安全**

`CallTool`/`call` 全程无锁（`client.go:156-190, 258-298`），`requestID++`（`client.go:259`）数据竞争；stdio 下两个 goroutine 同时读写同一对管道会**串响应**；HTTP 下 `t.sessionID` 也是无锁写（`http_transport.go:69-71`）。

Gateway 是多请求并发的（`pkg/gateway/gateway.go:66-160`），所以这不是理论问题。

**⑤ 建立连接时持池写锁**

```go
// pkg/mcp/pool.go:28-52
func (p *MCPClientPool) ConnectAgentServers(...) ([]*Client, error) {
    p.mu.Lock()                       // 锁住整个池，直到所有进程 spawn + initialize 完成
    defer p.mu.Unlock()
```

而且该函数签名返回 `error`，**函数体内任何路径都不会返回非 nil error**（`:28-58`）—— `factory.go:110-113` 的错误分支是死代码。

**⑥ 无 Wait → Unix 僵尸进程**；`Process.Kill()` 只杀直接子进程，不杀进程组（`:110-113`）→ `go run` 这种会再 fork 的场景留孤儿。

**⑦ HTTP transport 声明接受 SSE 却不解析**：`Send` 带 `Accept: text/event-stream`（`:57-59`）但直接把 body 交给 `json.Unmarshal`（`client.go:279`）—— 真实的 Streamable HTTP MCP Server 返回 SSE 时会解析失败。

**⑧ stdio `Send` 无超时**（`:82-95` 的 `ReadString` 阻塞无 deadline），服务端卡住 → 整个 Agent 卡住。

### 做得好的地方

- `transport.go:7-22` 抽象干净，stdlib/http 可替换。
- **`Client.initialize`（`client.go:98-131`）完整走 `initialize` → `notifications/initialized` → `tools/list`**，协议版本 `2024-11-05`，并且用 `SendNotify`（`:300-315`）避免死锁 —— 这是手写 MCP 客户端里少见的正确顺序。
- adapter 把 MCP `inputSchema` 转成统一 `ParameterDef`（`adapter.go:36-57`），工具来源对上层透明。

### 安全点（主动抛出来会显得成熟）

`McpServers` 是 etcd 推送的配置（`config/config.go:47`、`dynamic/manager.go:120-141`），`command`/`args` 直接进 `exec.CommandContext`（`stdio_transport.go:37`）→ **任何能写 etcd 配置的人都能在 Agent 主机上执行任意命令**，没有命令白名单/路径限制。

---

## 五、Skill 系统

### 事实

**Skill 与 Tool 的区别在这个项目里的落地**：Tool 有 `Execute`（被模型调用），**Skill 没有执行体** —— Skill = 专业 SystemPrompt + 工具引用 + few-shot（`pkg/skill/skill.go:14-35`）。

执行时：注入工具 → 用 Skill 的 prompt 覆盖 system prompt → **复用同一个 ReAct 引擎**（`skill_enhancer.go:26-42` → `base.go:195`）。

**触发时机**：`manager.go:126-138` 的 `MatchAndExecute`，调用点 3 处：`cmd/stage1/main.go:178`、`cmd/stage2/main.go:337`、`pkg/gateway/gateway.go:133-153`。

**加载**：`Loader.LoadAll` 走 `filepath.Walk` 找所有 `SKILL.md`（`loader.go:31-44`），**ID 取自目录名**（`:60-64`），YAML front matter 解析 `name/description/tools/examples`（`:108-131`），**Markdown 正文整体作为 SystemPrompt**（`:76`）。

### 关键缺陷

**① `SKILL.md` 里的 `{{city}}` / `{{user_input}}` 没有任何模板渲染代码**

全仓 grep `{{` 在 Go 代码里零命中。那整段「执行步骤 / Prompt 模板」是**原样进 system prompt 的**，`tool: get_weather city: "{{city}}"` 只是给模型看的文字提示。

被问「变量怎么注入」如果答「有模板引擎」就是致命失分。

**② Skill 热更新是死代码**

```go
// pkg/skill/manager.go:86-94
if oldModTime, exists := m.skillsModTime[id]; exists && reloadedModTime.After(oldModTime) {
    ...m.skillsModTime[id] = reloadedModTime...
    return reloaded, true
}
return skill, true      // skillsModTime 只在这里被写，而这里要求它已存在 → 永不命中
```

`skillsModTime` 从 `LoadAll`（`:40-52`）到 `Register` 都不写 → **改了 SKILL.md 永远不会生效**，只会每次 `Get` 多一次 `os.Stat`。

**③ 关键词匹配对中文完全失效**

`keywordMatch`（`manager.go:163-173`）用 ASCII 空格/`,`/`.`/`/` 分词。中文 description 没有这些分隔符 → 整段描述成为**一个 token** → `strings.Contains(input, 整段描述)` 几乎不可能成立。

加上 skill 的 `Name` 是英文（`parenting-activity-advice`）而用户输中文、`examples` 为空 → **关键词路径实质上永不命中**，全部依赖 LLM 匹配。**`MatchSkills` 是装饰。**

**④ LLM 匹配是每条消息一次额外 LLM 调用**（`manager.go:197`），无缓存、无 embedding 路由 → 延迟和成本翻倍。

**⑤ `injectTools` 直接污染共享 Agent**

`skill_enhancer.go:45-63` **直接往共享 Agent 的 toolManager 里注册工具，不撤销** → Skill 声明的工具会永久污染该 Agent 后续所有普通对话。

**⑥ `SkillRefs` 只被当布尔开关用**（`gateway.go:136`、`stage2/main.go:336`）—— **没有用来筛选哪些 Skill 可用**。

**⑦ 工具描述被注入两次**：`buildSkillPrompt` 写进 system prompt（`skill_enhancer.go:71-79`），`base.go:316` 又生成 `tools` 数组 → 重复占 token。

### 做得好的地方

- **Skill 不做流程执行、只做 prompt + 工具白名单，把控制权交给 ReAct 循环**（`skill.go:6-12` 的注释还引了 Semantic Kernel / Assistants / LangChain 三种主流做法）—— 这个设计判断是对的。
- 技能格式对齐 Anthropic Skill 的 `SKILL.md`（front matter + Markdown）。

---

## 六、多 Agent（Stage 2）

### 事实

- **注册表**：`pkg/agent/registry.go`，`map[string]*AgentEntry` + `sync.RWMutex`（`:66-70`），`AgentEntry` 带 `refCount atomic.Int32` + `marked atomic.Int32`（`:24-31`）。
- **热更新流程**（`:153-214`）：`markForDeletion` → 建新实例 → 替换 map → `go gracefulDestroy`（等引用归零，最多 5s）。
- **工厂**：`factory.go:22-32`，`CreateAgentFromConfig`（`:101-137`）7 步装配。
- **路由**：Stage 2 **没有 supervisor**。只有三种：CLI 手动 `/switch`、Gateway 按 `req.AgentID` 或「第一个可用 Agent」（`gateway.go:68-78`）、单 Agent 内部的 Skill 匹配。

### 关键缺陷

**① `acquire()` 的 TOCTOU**

```go
// pkg/agent/registry.go:34-40
func (e *AgentEntry) acquire() bool {
    if e.marked.Load() == 1 { return false }   // 检查
    e.refCount.Add(1)                          // 自增：两步之间可被 markForDeletion 插入
    return true
}
```

窄窗口：请求通过检查后 `markForDeletion` 生效、`waitForRelease` 看到 0 返回、旧 Agent 被 `Close()`，请求随后在**已关闭的 Agent** 上跑。

**② 持写锁做重活**

`Unregister` 在**持有写锁时同步等最多 5 秒**（`:120-144` → `waitForRelease`），`RegisterWithSource`/`doUpdate` 在**持写锁时创建 Agent**（`:91-117, 154-199`），而创建 Agent 会 **fork MCP 子进程并做 JSON-RPC 握手** → **一次注册/注销期间全注册表被阻塞**。

`waitForRelease` 用 50ms 轮询（`:217-229`），应该用 `sync.Cond` 或 channel。

**③ `configEqual` 漏比字段**

```go
// pkg/agent/registry.go:344-371
if a.ID != b.ID || a.Name != b.Name || a.SystemPrompt != b.SystemPrompt ||
    a.ModelRef != b.ModelRef || a.KnowledgeBase != b.KnowledgeBase { return false }
// 比较了 ToolRefs/SkillRefs/McpServers，但没有比较 Description 和 Memory
```

→ 只改描述或只改记忆配置的 etcd 推送会被判定「配置未变化，跳过更新」。

**④ `HealthCheck` 不是健康检查**

`factory.go:248-288`：MCP 状态直接写死 `"configured"`（`:256-259`），RAG 只判断 service 是否为 nil，Skill 只判断存在。**全仓无任何调用者读取 `MCPStatus`/`SkillsFound`。**

**⑤ 没有任何测试**（`find . -name "*_test.go"` 返回空）。

### 做得好的地方（这条线上最值得讲的）

- **Agent 热更新的引用计数 + 优雅销毁**（`registry.go:24-63,154-214`）配合 **Gateway 请求级 acquire/release**（`gateway.go:100-113`），且热替换后如果请求拿到已标记的旧 entry，会**刷新到最新 Agent**（`gateway.go:101-108`）。创建失败还会**回滚 `marked=0`**（`registry.go:181`）。
- **session 级 Lane 串行化**（`gateway.go:117-131`）。
- 这三层加起来是「企业级网关」故事里最实的部分。
