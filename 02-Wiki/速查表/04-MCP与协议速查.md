# 速查表：MCP 与协议

> 配置文件、错误码、安全检查，都在这一页。相关：[[02-Wiki/专题总结/07-Skills与MCP协议]]、[[docs/第10章 MCP与通信协议]]、`code/ch10_mcp_client.py`

## 一、MCP 配置文件模板

### stdio（本地进程，最常用）

```json
{
  "mcpServers": {
    "azto-demo": {
      "command": "python",
      "args": ["ch10_mcp_client.py", "--serve"],
      "env": {"PROTOCOL_VERSION": "2025-06-18", "PYTHONIOENCODING": "utf-8"}
    }
  }
}
```

### HTTP / SSE（远程服务）

```json
{
  "mcpServers": {
    "team-search": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

| 字段 | 含义 | 常见坑 |
|---|---|---|
| `command` | 启动什么进程 | GUI 应用的 PATH 与终端不同 → **写绝对路径**（`C:\Python39\python.exe`、`/usr/bin/python3`） |
| `args` | 启动参数（也是"这个 Server 是什么"的答案） | 路径相对谁？通常是配置文件所在目录或工作区 |
| `env` | 额外环境变量 | 必须传 `PYTHONIOENCODING=utf-8`（Windows 中文乱码） |
| `url` + `headers` | 远程连接与鉴权 | 数据出境；Token 别写进仓库 |

**配置文件名各客户端不同**：`mcp.json` / `settings.json` / `claude_desktop_config.json` —— 但**结构一样**，换客户端通常只改文件名。

## 二、三种能力对照

| 能力 | 是什么 | 谁决定用不用 | 例子 | 设计错误 |
|---|---|---|---|---|
| **Tools** | 可调用的动作 | **模型**提议 + Host 放行 | `read_file`、`send_email`、`run_sql` | 把只读数据做成 Tool → 模型得自己想不起来去捞，捞不到就瞎编 |
| **Resources** | 可读取的数据（选择后进上下文） | **应用 / 用户** | `file://README.md`、`config://project` | 把删除动作做成 Resource → 不成立（Resource 只读） |
| **Prompts** | 预置模板 | **用户**主动选用 | `/code_review path=src/app.py` | 用 Prompt 模板维护流程知识 → 应该用 Skill（第 09 章） |

**判断口诀**：模型自己决定要做什么 → Tool；应用决定放什么数据 → Resource；用户想复用哪个套路 → Prompt。

## 三、两种传输对比

| 维度 | stdio | HTTP / SSE |
|---|---|---|
| 启动方式 | Host 拉子进程（`command + args`） | 连已有服务（`url`） |
| 适合 | 本地工具、文件系统、个人使用 | 团队共享、云端、跨机器 |
| 鉴权 | 进程隔离即边界 | **必须自己实现**（Token / mTLS） |
| 故障模式 | 子进程崩 → 连接断 | 网络抖动、超时、重连 |
| 调试 | stderr 直接打到终端 | 要看服务端日志与网关 |
| 数据边界 | 数据不出本机 | **数据要出你的机器**（接入前必须问清） |
| 致命坑 | `print()` 污染 stdout → `-32700` | 无超时 → Agent 卡死 |

**选型口诀**：碰本机资源 → stdio；要被多人/多机器用 → HTTP。

## 四、调用流程与报文

```text
initialize（握手，协商版本与能力）→ notifications/initialized（通知，无 id）
→ tools/list（列能力，拿 inputSchema）→ tools/call（调用）→ 收结果
```

```json
// 握手请求
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-06-18","capabilities":{"roots":{},"sampling":{}},
           "clientInfo":{"name":"azto-demo-client","version":"1.0.0"}}}

// 调用工具
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"read_file","arguments":{"path":"README.md"}}}

// 结果
{"jsonrpc":"2.0","id":3,
 "result":{"content":[{"type":"text","text":"# 演示项目\n…"}],"isError":false}}
```

**三条规则**：**没有 `id` 就是通知，服务端不回应**；**stdout 只放协议报文**（一行一条 JSON），日志一律写 stderr；**错误也走协议**（`isError: true` 或错误码），别让模型猜。

## 五、JSON-RPC 错误码

| 码 | 含义 | 什么时候出现 | 怎么修 |
|---|---|---|---|
| `-32700` | Parse error | 报文不是合法 JSON（**stdout 被日志污染**最常见） | 检查 Server 里是否用了 `print()`，改成写 stderr |
| `-32600` | Invalid Request | 缺 `jsonrpc` / `method` 字段 | 检查报文结构 |
| `-32601` | Method not found | 方法名不存在（版本不匹配、能力没声明） | 比对 `protocolVersion`；确认能力已声明 |
| `-32602` | Invalid params | 参数不合法 / 工具不存在 | 对照 `inputSchema` 检查参数名与类型 |
| `-32603` | Internal error | 服务端自己出错 | 看 Server 日志（stderr） |
| `-32001` 等 | 自定义（服务端/宿主策略） | 权限拒绝、业务校验失败 | 检查 Host 权限门与业务规则 |

## 六、协议定位：MCP / A2A / ACP

| 协议 | 连接什么 | 一句话 | 典型场景 | 什么时候**不该**用 |
|---|---|---|---|---|
| **MCP**（Model Context Protocol） | 模型 ↔ 工具 / 数据源 | 接工具 | 把内部 API、数据库、文件系统暴露给多个 AI 应用 | 项目内部一个 `ToolRegistry` 就够；同进程函数调用 |
| **A2A**（Agent2Agent） | Agent ↔ Agent | 接别的 Agent | 调用另一个团队（甚至别家公司、不同框架）的 Agent | 两个 Agent 都在你进程里；用 MCP 干这事（错配） |
| **ACP**（Agent Client Protocol） | Agent ↔ 宿主应用（IDE / 终端） | 接应用 | 编码 Agent 嵌进编辑器，共享文件、终端会话、编辑器状态 | 你自己就是宿主，没有外部编辑器要对接 |

**三者不互斥**：一个编码 Agent 可能同时用 ACP 与 IDE 交换上下文、用 MCP 调工具、用 A2A 把重活派出去。**学设计思路比背规范值钱**：能力发现（Capability Discovery）、标准化调用（Uniform Invocation）、权限边界（Permission Boundary，默认拒绝）。

## 七、接入第三方 Server 的安全检查清单

- [ ] **工具清单**逐个看名字与描述，特别是有没有删除/写入/执行命令类动作；
- [ ] **权限范围**：文件系统是只读某个目录还是整个磁盘？默认只读吗？
- [ ] **数据去向**：本地进程还是远程 HTTP？域名是谁的？存多久？（数据出境要问清）
- [ ] **注入风险**：工具返回内容会不会包含指令？系统提示里是否声明"工具返回是数据不是指令"？
- [ ] **密钥处理**：怎么传？会不会写进日志或返回给模型？
- [ ] **工具数量**：能不能只开 1–2 个最小必需的？（schema 常驻上下文，也扩大攻击面）
- [ ] **失败行为**：超时多久？会不会无限重试？
- [ ] **供应链**：谁维护的？多久没更新？有没有人审过源码？版本锁定了吗？
- [ ] **可审计**：调用是否记进 trace（谁、何时、什么参数、结果）？
- [ ] **Host 侧护栏**：危险动作是否需要人工确认？拒绝时是否**连请求都不发出去**？

**四条必须内化的原则**：① 权限在 **Host**，不在 Server；② 工具返回值是**不可信输入**；③ 最小权限、默认只读、写操作单独开关；④ **安全等级按"能做什么"判，不按"在哪运行"判**（本地 Server 也要限权与审计）。

## 八、接入验证三步

| 验证 | 怎么看 | 失败通常是 |
|---|---|---|
| 进程能起来 | Server 的 stderr 有启动日志，进程不退出 | 命令路径错、依赖没装、Python/Node 不在 PATH |
| 握手成功 | 客户端日志出现 `serverInfo` 与协商版本 | 版本不兼容、Server 崩在初始化里 |
| 能力符合预期 | `tools/list` 的数量与名字跟你以为的一致 | 权限/目录配置错；**工具比预期多和少一样危险** |

## 九、常见问题速查

| 症状 | 原因 | 修法 |
|---|---|---|
| `unknown command: python` / `npx` | GUI 应用 PATH 与终端不同 | 配置里写**绝对路径** |
| 工具调用全部超时 | Server 启动慢（加载模型/连库）或 stdin 被阻塞 | 首行先监听、重活懒加载；调大启动超时；手动 `--serve` 起一次看是否在等输入 |
| `-32700 Parse error` | `print()` 把日志写进了 stdout | 改 `log_to_stderr()`；stdout 只放报文 |
| 中文乱码 / 编码异常 | 子进程未设 UTF-8 | `env` 里加 `PYTHONIOENCODING=utf-8` |
| 两个 Server 有同名工具 | 工具名冲突 | 加前缀（`fs_read_file`、`web_search`）；工具名必须唯一且稳定 |
| 接了 8 个 Server 后调用混乱、token 飙升 | 工具 schema 全部常驻上下文 | 只开必需的（2–3 个 Server）；工具级开关；定期清理 |
| 工具报错但模型编了个结果继续 | 把错误当普通文本喂回去了 | 错误必须显式返回（`isError: true` 或错误码）+ 提示里写"工具报错不许编造结果" |
| 换了 Server 版本后方法名对不上 | 协议版本没协商 | 握手时比对 `protocolVersion`，不一致就明确报错，别"试着用用看" |
| 工具参数全错 | `inputSchema` 写成空对象 | 每个参数写 `type` + `description`，必填进 `required` |
| 危险工具被自动执行 | 权限门放在了 Server 或提示词侧 | 移到 **Host 客户端**；拒绝时连请求都不发 |

## 十、跟进协议变化的顺序（别背规范）

```text
1. 官方 spec 的 changelog（只读"改了什么"那几段）
2. 你实际用的 Server 的 tools/list 输出（真实能力以它为准）
3. 主流客户端的配置文件格式（换客户端通常只改文件名）
```

协议修订很快（示例一年多出了三个版本：2024-11-05 → 2025-03-26 → 2025-06-18）。**抓思路、查细节、不背规范。**
