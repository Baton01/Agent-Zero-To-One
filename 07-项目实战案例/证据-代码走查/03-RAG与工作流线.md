# 代码走查情报：RAG 与工作流线

> 覆盖 `pkg/rag`、`pkg/workflow`、`pkg/metrics`、`cmd/stage1-4`。
> **行号约定：** 相对项目根，格式 `文件:行号`。

---

## 一、RAG 全链路

### 事实

**分片策略**（`pkg/rag/splitter.go`）：三级降级 —— 先按 `\n\n` 切段落（`:122`），段落内按单 `\n` 合并成行块（`:133-152`）；段落超过 chunkSize 时按句子切（`:92-97` → `splitLongParagraph`）。

**句子切分的终止符含全角 `。？！` 和半角 `.?!`**（`:214`）—— **这是全项目唯一一处针对中文的专门处理**。

**长度一律用 `utf8.RuneCountInString`**（`:58,169`）→ chunkSize=500 是 500 个**字符**而非字节，**中文不会按字节切坏**。

chunkSize=500 / overlap=50（默认值 `pkg/rag/service.go:28-33`，yml 一致）。overlap 实现是**字符级尾截断**：取上一块最后 50 个 rune 拼到下一块开头（`:234-240`）。

**Embedding**：模型 `nomic-embed-text`，**维度 768**（`embedding.go:22-24`）。走 Ollama 的 OpenAI 兼容端点 `/v1/embeddings`，**一次 HTTP POST 带 `Input: texts` 数组**（`pkg/llm/openai.go:167` 起）。**批量 `maxBatchSize = 10` 硬编码**（`embedding.go:30`）。

**Milvus schema**（`pkg/rag/milvus.go:95-121`）：

```go
schema := entity.NewSchema().WithName(collName).WithAutoID(true).
    WithField(...WithName("id")...WithIsPrimaryKey(true).WithIsAutoID(true)).
    WithField(...WithName("content").WithDataType(entity.FieldTypeVarChar).WithMaxLength(65535)).
    WithField(...WithName("embedding").WithDataType(entity.FieldTypeFloatVector).WithDim(int64(s.dimension))).
    WithField(...WithName("metadata").WithDataType(entity.FieldTypeJSON))
// 索引：AUTOINDEX + COSINE
idx := entity.NewGenericIndex("embedding_idx", entity.AUTOINDEX, map[string]string{
    "metric_type": string(entity.COSINE),
})
```

检索 `topK = 5`，search param `NewIndexAUTOINDEXSearchParam(10)` 即 nprobe=10（`:200`）。

**相似度阈值是 topK 之后的后置过滤**（`service.go:134,267`）：`if result.Score < s.scoreThreshold { continue }`。阈值 stage1=0.6、stage2/3=0.5（**不一致且无数据支撑**）。

知识库隔离用 `SearchWithFilter`，filter 是**字符串拼接**：`fmt.Sprintf("metadata[\"%s\"] == \"%s\"", k, val)`（`milvus.go:460-476`）。

### 关键缺陷

**① 零 citation**

```go
// pkg/agent/base.go:273-283
for i, result := range results {
    if i > 0 { contextBuilder += "\n\n" }
    contextBuilder += result.Content          // ← 只有正文
}
return basePrompt + "\n\n以下是与用户问题相关的知识库内容，请参考这些内容回答问题：\n\n" + contextBuilder
```

检索结果塞进 **system prompt**，**没有编号、没有 source、没有 score**。`SearchResultItem.Source` 在 `base.go:273-279` 被取到却**直接丢弃**。

`Service.RetrieveAndGenerate`（`service.go:165-201`）里其实有一套 `[1] [2]` 编号的 prompt 模板，但**全项目无任何调用点**（死代码）。

而且 `source` 本身也只是 `filepath.Base(file)`（`:382`）—— **只有文件名**，两个同名文件不同目录会撞。

**② 阈值后置过滤的架构问题**

**阈值不能提升召回，只能让结果变空。** top-5 全低于阈值时返回 0 条，而不是"再多取几条"。

正确做法：阈值传进 Milvus 做 `range_filter`，或先取 topK×3 再筛。

**③ `generateChunkID` 是真 bug**

```go
// pkg/rag/splitter.go:243-248
string(rune('0'+index))     // index≥10 会产生 ':' ';' '<' 等字符，ID 完全无意义
```

目前"没炸"只是因为 Milvus 用 AutoID、chunk.ID 根本没入库（`milvus.go:168-172` 只插 content/embedding/metadata）。**问到「chunk id 怎么保证唯一」就露了。**

**④ `currentLength` 记账 bug**

长段落分支把 `currentLength = 0` 但 `currentChunk` 里还留着 overlapBuffer（`splitter.go:84-102`）→ 下一段被算少 → **实际 chunk 会超过 500**。

**⑤ 重复索引（会影响检索质量）**

`LoadDocumentsFromDirForKB`（`service.go:350-392`）**不先删旧数据**，而 stage1（`cmd/stage1/main.go:84`）和 stage2（`cmd/stage2/main.go:81`）**每次启动都直接调它** → **每次重启往 Milvus 追加一份重复 chunk**。只有 `LoadKnowledgeBase`（`service.go:293-303`）走了"先删"。

**检索结果会重复。**

**⑥ 配置被静默忽略**

`maxBatchSize` 在 yml 和 `config.go:132` 都有，但 `SetMaxBatchSize` **无任何调用点**。`MilvusConfig.Collection` 被忽略 —— collection 永远叫 `bitguide_documents`（`store.go:55-61` + `milvus.go:70-72`），维度写死 768。

**⑦ `DeleteBySource` 直接 DropCollection**（`milvus.go:305-320`，注释自己承认 Milvus 不支持按条件删）。

### 完全未实现

- **Rerank：无**（全项目 grep 零命中）
- **混合检索 / BM25 / 稀疏向量：无**
- **评估：完全没有** —— 没有 golden set、没有 Recall/MRR/NDCG、没有 A/B

知识库只有 **4 篇 md、共 20554 字节**（`knowledge/parenting_docs/`），按 500 字切约 20-30 个 chunk —— **这个量级做评估也没有统计意义**。

**被问「你怎么知道检索变好了」只能答「看日志」。** `service.go:127` 那行 `log.Info("[RAG]", "Raw search returned %d results (threshold: %.2f)")` 就是全部的"评估手段"。

---

## 二、工作流引擎（Stage 4）

### 事实

**图定义**（`pkg/workflow/core/graph.go`）：
- 哨兵节点 `Start = "__start__"` / `End = "__end__"`（`:11-14`）—— **直接对标 LangGraph**。
- `WorkflowGraph{nodes, edges, channels}`（`:17-22`）。声明了 `mu sync.RWMutex`（`:21`）但 **`g.mu` 全文件零使用**，`AddNode/AddEdge` 完全无锁。
- `Build()` 只校验"至少一个节点"（`:70-75`）—— 不校验 Start 边、不校验边指向的节点是否存在（运行时才报，`executor.go:42-44`）、不校验可达性。

**三个概念**：
- 节点：`interface { GetID() string; Execute(ctx, *WorkflowState) (*WorkflowState, error) }`（`node.go:6-9`）
- 边：`SimpleEdge{From,To}` 恒定路由；`ConditionalEdge{From, Router func(state) string}`（`edge.go:10-27`）
- Channel：`interface{ Merge(current, incoming interface{}) interface{} }`（`channel.go:4-6`）—— **语义等价于 LangGraph 的 reducer**

### 执行模型：串行

```go
// pkg/workflow/core/executor.go:12
// WorkflowExecutor runs a workflow graph with serial execution.
// executor.go:40-72
for currentNodeID != End && currentNodeID != "" {
    node := e.graph.GetNode(currentNodeID)
    state, err = node.Execute(ctx, state)
    currentNodeID = e.resolveNextNode(currentNodeID, state)
}
```

**严格单线程 while 循环，一次一个节点。没有 goroutine、没有 errgroup、没有 superstep、没有并行分支、没有 fan-out/fan-in 概念。**

全包唯一并发点是 `SubTaskExecutorNode`。

**路由**：`resolveNextNode`（`:159-174`）—— 只有 1 条边就直接走；多条边时返回**第一条 `ResolveTarget` 非空**的边。

因为 `SimpleEdge.ResolveTarget` 恒返回非空 `To`（`edge.go:16`）→ **边的注册顺序决定语义**。混合挂静态边+条件边时，**静态边永远赢**，条件边可能永远轮不到。

### 三种 channel：全是死代码

```go
// core/state.go:55-63
func (s *WorkflowState) Set(key string, value interface{}) {
    if ch, ok := s.channels[key]; ok {
        s.data[key] = ch.Merge(s.data[key], value)
    } else {
        s.data[key] = value
    }
}
```

- `AppendChannel`：`current` 和 `incoming` 都往 `[]interface{}` 里拼（`channels/append.go:6-19`）
- `LastValueChannel`：直接覆盖（`channels/last_value.go:6-8`）
- `EphemeralChannel`：**注释写 "is cleared between supersteps by the executor"**（`channels/ephemeral.go:3`），**但 executor 里没有任何清除逻辑，全项目也没有任何一处使用它**。

**关键事实**：
- `WithChannel` / `WithAppendChannel`（`graph.go:58-67`）**全项目零调用点**
- `state := core.NewWorkflowState()` 建 state 时**不挂任何 channel**（`cli.go:76`、`server/handler.go:118`）
- 只有 `Resume` 里用的是 `NewWorkflowStateWithChannels(e.graph.GetChannels())`（`executor.go:99`）

**结论：channel 抽象是完整的死代码，三个实现都没被真正跑通过。** "superstep" 这个概念在整个引擎里**不存在**。

### 环检测：没有

**图级零环检测**：无 visited 集合、无最大步数。**`A→B→A` 会无限循环。**

防无限循环只靠 `LoopNode` 自带的 `MaxIterations = 10`（`patterns/loop_node.go:19-26`），而它是**把 body 节点当嵌套函数调用**（`:32-44`），**不是图上的环**：

```go
iteration := 0
for iteration < n.MaxIterations && n.ShouldContinue(state) {
    iteration++
    state, err = n.Body.Execute(ctx, state)
}
```

注意 `ShouldContinue` 是**前置判断** → 初值 false 时 body 一次都不执行。

### Checkpoint

结构：`{ExecutionID, CurrentNodeID, StateData, CreatedAt, Status}`（`checkpoint.go:6-12`），状态 running/paused/completed/failed。

`redis_store.go` 存两个 key：
```go
keyPrefix = "bitguide:workflow:checkpoint:"   // String，整个 Checkpoint 的 JSON，Set(..., 0) 即永不过期
indexKey  = "bitguide:workflow:checkpoints"   // Hash: execID -> status
```

**写入时机：每个节点执行完都写一次**（`executor.go:67-69`）。

**恢复**：`Resume`（`:83-149`）要求 `cp.Status == "paused"`（`:95`），从 checkpoint 重建 state、清 `_interrupted`、并入 userInput，然后 `resolveNextNode(cp.CurrentNodeID, state)`（`:115`）—— **从暂停节点的下一条边继续，不会重跑中断节点**。

### InterruptNode

```go
// patterns/interrupt_node.go:23-48
if n.CheckpointStore != nil {
    cp := &checkpoint.Checkpoint{ExecutionID: state.ExecutionID, CurrentNodeID: n.ID,
        StateData: state.ToMap(), Status: "paused"}
    n.CheckpointStore.Save(ctx, cp)
}
state.Set("_interrupted", true)
state.Set("_interrupt_prompt", prompt)
```

**它不阻塞、不等待**，只打标记 + 存 checkpoint；由 executor 在节点返回后检测 `_interrupted` 并 return（`executor.go:59-65`）。**人机交互是"协作式"的。**

### Supervisor / Router / SubTaskExecutor

- **`SupervisorNode`**（`supervisor_node.go:26-41`）：调 `PlannerFn(userInput, AvailableAgents)` 拿 `[]SubTask`，**只把第一个子任务**写进 `current_sub_task`。**Planner 是普通 Go 回调，不是 LLM 规划**；demo 里就是个硬编码的两项闭包（`cmd/stage4/main.go:228-235`）。
- **`RouterNode`**（`router_node.go:21-33`）：`RouterFn(userInput) → routeKey` → 查 `AgentMapping`。demo 里 `RouterFn` **恒返回 `"default"`**（`main.go:104-106`）→ **router-demo 是个恒等路由**。
- **`SubTaskExecutorNode`**（`sub_task_executor_node.go:39-77`）：**唯一的真并发**。

```go
for i, task := range subTasks {
    wg.Add(1)
    go func(idx int, t SubTask) {
        defer wg.Done()
        ag, entry := n.Registry.AcquireForRequest(t.AgentID)
        sessionID := "wf-" + state.ExecutionID     // ← 所有子任务共用同一个 sessionID
        response, err := ag.ChatWithSession(ctx, t.Description, sessionID)
        results[idx] = fmt.Sprintf("【%s】\n%s", t.Description, response)
    }(i, task)
}
wg.Wait()
```

用 `WaitGroup` + `Mutex` 只保护 `firstErr`，`results[idx]` 按索引写不共享（**这点写对了**）。

**但**：子任务全失败时只返回第一个错误、**不 cancel 其余 goroutine**；**无超时、无并发上限**（子任务多就打爆）；所有子任务**共用同一个 sessionID** → 同一 agent 的多个子任务会写进同一个会话记忆（有锁不崩，但**消息交错、上下文互相污染**）。

- **`AgentNode`**：**执行时**从 Registry 取 agent（所以 etcd 热更新生效），走引用计数（`agent_node.go:36-41`）。
- **`ToolNode`**：要求 `state["tool_args"]` 是 `map[string]interface{}`（`tool_node.go:41-44`）—— **8 个 demo 里一个都没用到它**。

### 与 LangGraph 的对比

**像的地方**：`__start__`/`__end__` 哨兵、nodes+edges 图、条件边 router、Channel/reducer 抽象（`AppendChannel` ≈ `operator.add`，`LastValueChannel` ≈ 默认 last-value channel）、checkpointer + interrupt/resume 的形态、Supervisor/Router/Loop 这些 prebuilt 模式、state 当共享黑板。

**差的地方（按被问到的概率排序）**：

| # | 差异 | 具体 |
|---|---|---|
| 1 | **执行模型** | LangGraph 是 Pregel 风格 superstep，同超步内多个节点并行、channel 带版本号做合并、有 join/barrier。这里是**单线程 while 循环一次跑一个节点，根本没有超步概念** |
| 2 | **channel 是核心 vs 装饰** | LangGraph 的 channel 就是运行时数据结构；这里的 channel **从没被任何 state 实例挂载过**，三条实现全是死代码 |
| 3 | **checkpoint 语义** | LangGraph 有 thread_id + checkpoint_id、每步快照成链、支持 time travel / 分支回放。这里每个 execution **只保留一份最新快照**（同 key 覆盖），没有历史、无法回放 |
| 4 | **interrupt** | LangGraph 是节点内动态 `interrupt()` + `Command(resume=...)`，可中途挂起；这里**节点返回后由 executor 检查状态标记**，节点必须主动 `Set("_interrupted", true)` |
| 5 | **没有的能力** | 流式/事件流、子图组合、节点级 retry/timeout、节点缓存、`Command` 式 goto、Send/fan-out API |

### 关键缺陷

- **`ExecutionID` 用 `UnixNano()%1e8` 生成**（`executor.go:191-193`）—— **只有 8 位十进制，约 0.1 秒就会重复，并发下极易撞 ID**。
- **`Checkpoint.CreatedAt` 从未被赋值**（`:180-185` 和 `interrupt_node.go:34-39` 都没设）→ `/status` 返回的 `createdAt` **永远是 `0001-01-01T00:00:00Z`**（`handler.go:186`）。
- **Redis checkpoint 无 TTL**（`Set(..., 0)`）+ 每步全量 state JSON → **长会话存储无界增长**。
- `saveCheckpoint` 用 `context.Background()`（`:186`）→ 请求取消后仍会写。
- Redis `Update` 是 Load+Save 的**非原子读改写**（`redis_store.go:61-74`）；`ListByStatus` 是 HGetAll + **N+1 次 Load**（`:77-95`）。
- `MemoryCheckpointStore.Load` **返回内部指针**，调用方改它等于改存储（`memory_store.go:42-46`）；而 `Save` 是 copy-on-write、`Update` 原地改 —— **三种语义不一致**。

---

## 三、Stage 4 的完成度

### 事实

- **入口是 CLI，不是 server**：`cmd/stage4/main.go` 只做"加载配置 → 建 LLM/Factory/Registry → 可选 etcd → checkpoint store → 注册 8 个 demo → `cli.Run()`"（`:23-88`）。banner 明写 "BitGuide Stage 4 — 工作流引擎 CLI"（`cli.go:168-175`）。
- **`--server` 是个幌子**：`:27-31` 只是把 `--server` 排除出"配置文件路径"的解析，**没有任何分支去启动 HTTP server**。
- **`pkg/workflow/server` 是死代码**：`WorkflowHandler.RegisterRoutes`（`handler.go:74-79`，注册了 `GET /api/v1/workflow/list`、`POST /run`、`GET /status/{id}`、`POST /resume/{id}`）**全项目零调用点**。写好的 4 个 REST 接口**一个都访问不到**。
- **已实现的节点类型**：functionNode、AgentNode、ToolNode、RouterNode、SupervisorNode、SubTaskExecutorNode、LoopNode、InterruptNode —— **8 类代码都写完了**。
- **实际被 demo 用到的**：functionNode、AgentNode、RouterNode、SupervisorNode、SubTaskExecutorNode、InterruptNode。**ToolNode 和 LoopNode 零使用**。
- **`_ = tool.NewManager()`（`main.go:43`）**：建了个 tool manager 然后**丢掉**，等于 ToolNode 无法被装配。
- **stage4.yml 没有 `rag` 段、没有 `skills` 段** → **Stage 4 完全不接 RAG、不接 Skills**。`gateway.enabled: true, port: 8081` 也从没被 main.go 读取过。
- **8 个 demo**：linear / router / tech / pipeline（preprocess-agent-postprocess）/ classifier / collab（drafter+reviewer）/ interrupt（draft→人工确认→refine）/ supervisor。
- **demo 7 的人工反馈是空的**：CLI 把用户输入塞进 `user_feedback`（`cli.go:99-102`），但 refine_agent 的 InputKey 是 `draft`（`main.go:211-212`）→ **没有任何节点读 `user_feedback`，用户敲的字被静默丢弃**，只是"按回车继续"。
- **测试：0 个**。`find -name "*_test.go"` 在整个仓库返回 0 个文件。
- **仓库根目录躺着一个 32MB 的 `stage4` 二进制**（Mach-O arm64），而 `.gitignore` 只忽略了 `/stage1 /stage2 /stage3`，**漏了 `/stage4`**。
- stage4 全用 stdlib `log.Printf`，**没用项目自己的 `pkg/log`**（彩色 clog）→ 和 stage1-3 日志风格不一致。
- **README 从头到尾只写 3 个 stage**，结构树里**连 `metrics` 和 `workflow` 两个包都没有**；go.mod 是 `go 1.23.0` 而 README 写 1.22+。

**Stage 4 是"实现了但没进 README"的隐藏阶段。**

---

## 四、可观测性（metrics）

`pkg/metrics/metrics.go` 用 Prometheus `promauto` 定义了 **6 个自定义指标**：

| 指标 | 类型 | 是否有写入点 |
|---|---|---|
| `bitguide_chat_requests_total{status}` | CounterVec | 有，stage3 两处 |
| `bitguide_chat_duration_seconds` | Histogram（DefBuckets） | 有，stage3 一处 |
| `bitguide_active_sessions` | Gauge | 有，但**只在 /health 里采样** |
| `bitguide_active_lanes` | Gauge | **无任何写入点** |
| `bitguide_rate_limit_rejected_total` | Counter | 有，stage3 一处 |
| `bitguide_tool_calls_total{tool_name}` | CounterVec | **无任何写入点** |

埋点全在 **stage3 的 HTTP chat 处理器**里：

```go
// cmd/stage3/main.go:416-423
chatStart := time.Now()
resp := activeProcessFunc(r.Context(), gwReq)
metrics.ChatDurationSeconds.Observe(time.Since(chatStart).Seconds())
if resp.Success { metrics.ChatRequestsTotal.WithLabelValues("success").Inc() } else {
    metrics.ChatRequestsTotal.WithLabelValues("error").Inc()
    if resp.ErrorCode == "TOO_MANY_REQUESTS" { metrics.RateLimitRejectedTotal.Inc() }
}
```

### 关键缺陷

- **`ToolCallsTotal` 从来没 Inc 过**（`pkg/tool/*` 里零引用）→ 面板上永远是空/0。
- **`ActiveLanes` 同理**，只有定义。
- **`ActiveSessions` 只在有人访问 `/health` 时才 Set**，而 Prometheus 抓的是 `/metrics` → 这个 Gauge 的时效性**完全依赖外部是否在打 /health**，属于反模式（Gauge 应该在事务边界更新）。
- **Histogram 用 `prometheus.DefBuckets`（0.005→10s），对 LLM 场景分桶完全错位**：一次 qwen2.5 生成常在 1-30s，**10s 以上全落进最后那个桶，P99 无法分辨**。
- `prometheus/client_golang` 在 go.mod 里是 **indirect**（`go.mod:38`）→ 说明是后加的，`go mod tidy` 没跑干净。
- **端口对不上**：`stage3.yml:88` gateway port 是 **8080**，但 `docker/prometheus.yml` 抓的是 `host.docker.internal:8081`（8081 是 stage1.yml 的端口）→ **按默认配置起 stage3，Prometheus 抓不到**。

### 完全未实现

- LLM 侧指标（token 数、调用次数、TTFT、错误率、模型维度）
- RAG 指标（检索耗时、命中数、平均 score、阈值过滤掉多少）
- 工作流指标（节点耗时、checkpoint 次数、暂停次数）—— **stage4 对 `pkg/metrics` 零引用**
- Tracing（全项目无 OpenTelemetry/span）
- 日志字段规范（stage4 还在用 `log.Printf`）

---

## 五、cmd/stage1-4 的差异

| | stage1 (205 行) | stage2 (395 行) | stage3 (676 行) | stage4 (252 行) |
|---|---|---|---|---|
| 入口形态 | CLI 单 Agent | CLI 多 Agent 平台 | HTTP 服务 | CLI 工作流引擎 |
| 配置 | 单 `agent:` | 单 `agent:` + etcd | 单 `agent:` + etcd | **`agents:` map 多 Agent** |
| RAG | 有 | 有 | 有 | **无** |
| Skills | 有 | 有 | 有 | **无** |
| etcd | 无 | `enabled: true` | `enabled: true` | `enabled: false` |
| Redis | 无 | `enabled: false` | `enabled: true` | `enabled: true`（只给 checkpoint） |
| Gateway | 无 | 无 | 全开（auth/限流/安全） | yml 有但**代码不读** |
| 可观测 | 无 | 无 | Prometheus | 无 |

**配置文件差异**：
- `stage1.yml`：`scoreThreshold: 0.6`、`topK: 5`、MCP（stdio，`go run mcp-servers/parenting/main.go`），**无 etcd/redis/gateway**
- `stage2.yml`：`etcd.enabled: true`、`redis.enabled: false`、`scoreThreshold: 0.5`
- `stage3.yml`：全量。`gateway.port: 8080`、`authEnabled: true` + `apiKey: bitguide-demo-key`、`globalQps: 1000` / `userQps: 10`、`sensitiveWords` 4 个词
- `stage4.yml`：`llm` + `agents`(3 个) + `etcd(disabled)` + `redis(enabled)` + `gateway(8081, 都 false)`。**没有 rag、没有 skills、没有 memory 顶层段**

**阈值不一致**：stage1=0.6 vs stage2/3=0.5，两份 yml 都带 `maxBatchSize: 10` 但**代码不读**。

---

## 六、三句话总结这条线

1. **最诚实的自我描述**：一个把 LangGraph 核心概念（状态图 / channel reducer / checkpoint / interrupt-resume）用约 2000 行 Go 重写并跑通 8 个 demo 的教学实现，**串行执行、无并发调度、无 RAG 评估、无自动化测试，server 层与 channel 层写了但没接线**。
2. **最强的亮点**：checkpoint + interrupt + resume 的完整闭环真的能跑（这是 LangGraph 最难抄的一块），Memory/Redis 双实现 + 四态状态机。
3. **最容易被问倒的**：说"支持并行"只有 `SubTaskExecutorNode` 一处撑场面；说"有 channel 机制"实际零调用点；说"有环检测"其实没有；说"有 HTTP 工作流 API"写了但没接线。
