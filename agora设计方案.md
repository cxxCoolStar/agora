# Agora 设计方案

> 本文是 Agora 的 Python 实施设计，不是 Java 源码的逐类翻译。设计依据为 `C:\Users\asta1\ai-project\open-agent` 当前源码及 Java 原版的真实业务边界。Agora 是独立重构/验证项目，不能把 Java 版本的生产流量、指标或运行事实直接归因到 Agora。

## 1. 目标与边界

### 1.1 目标

构建一个面向云端、多用户部署的 Agent Assistant Runtime，复用 OpenAgent 前端的交互方式，支持：

- 多 Agent 配置与会话管理；
- OpenAI-compatible 模型调用、流式输出和标准 tool call；
- 文件工作区、网页搜索/抓取、记忆和技能等工具；
- 同一会话 FIFO、跨会话并发、取消、超时、预算和循环保护；
- 会话消息与可恢复事件持久化，SSE 断线后可按游标回放；
- 上下文压缩和运行级敏感信息脱敏。

### 1.2 第一阶段不做

第一阶段不实现完整 Java 版本的所有外围能力：

- 管理后台的全部配置页面；
- 多渠道消息总线和 Redis worker 集群；
- MCP server 的完整生命周期管理；
- Docker sandbox 的生产级编排；
- 完整评测平台、自动记忆抽取和工作区历史快照；
- 文档切分、Embedding、向量库和混合检索。RAG 作为独立 Python 项目处理。

第一阶段的验收标准是“一个会话可以可靠地完成多轮工具任务，并能在浏览器断线后恢复结果”，不是功能数量最大化。

## 2. 从 Java 版本提取出的核心语义

Java 版本的有效链路如下：

```text
HTTP/SSE
  -> ChatStreamService
  -> AgentRunCoordinator
  -> AgentConversationFactory + ReActAgentKernel
  -> LLMService / ToolInvoker
  -> session_messages + session_events
  -> ChatEventHub -> SSE subscribers
```

需要保留的是这些语义，而不是 Spring 的类名：

1. 提交消息时先写入用户消息和 `CREATED` run，再进入会话队列。
2. 同一 `(agent_id, session_id)` 串行执行，跨会话由全局并发上限控制。
3. 每个 assistant tool call 都必须有配对的 tool result；工具异常也要转成可回灌模型的失败结果。
4. 高频 `content_delta` 是瞬时事件；`content`、`tool_call`、`tool_result`、`error`、`done` 先持久化再广播。
5. SSE 通过会话内单调递增 `seq` 恢复；订阅时先注册实时监听，再回放数据库，最后切换 live 状态。
6. 浏览器断开不等于取消 Agent run；取消必须通过显式 cancel 接口完成。
7. 工作区工具只接受相对路径，拒绝绝对路径、`..` 穿越和符号链接逃逸。
8. 达到步数、时间或预算后关闭工具并请求一次最终合成；模型协议污染或退化输出不能直接交付。

## 3. Python 目标架构

采用四层模块，外部只依赖少量深模块接口。`runtime` 不知道 FastAPI、SQLAlchemy 或具体模型 SDK；这些复杂性放在 adapter 内部。

```text
src/agora/
├─ domain/
│  ├─ models.py          # Run、Message、ToolCall、ToolResult、AgentEvent
│  ├─ errors.py
│  └─ policies.py        # budget、loop guard、timeout、redaction 规则
├─ runtime/
│  ├─ agent_loop.py      # 一次运行的 ReAct 状态机
│  ├─ run_coordinator.py # 会话 FIFO + 全局并发 + cancel/timeout
│  ├─ conversation.py    # 上下文装配、消息演进、压缩
│  └─ tool_scheduler.py  # 工具访问范围与执行顺序
├─ ports/
│  ├─ llm.py             # stream(request, on_event) -> ModelResponse
│  ├─ tools.py           # Tool、ToolRegistry、ToolInvoker
│  ├─ repositories.py    # Session/Message/Run/Event repository
│  └─ events.py          # EventPublisher / EventSubscriber
├─ adapters/
│  ├─ llm_openai.py      # httpx + OpenAI-compatible SSE 聚合
│  ├─ persistence.py     # SQLAlchemy async repository
│  ├─ events_local.py    # 进程内事件中心
│  ├─ filesystem_tools.py
│  ├─ web_tools.py
│  ├─ memory.py
│  └─ skills.py
├─ api/
│  ├─ chat.py            # stream/history/sessions/steer/cancel
│  ├─ agents.py
│  └─ schemas.py
└─ main.py               # FastAPI 装配
```

### 3.1 外部接口（稳定 seam）

#### `AgentLoop.run(command, sink) -> RunResult`

接口负责一次运行的完整状态机：模型调用、tool call、工具结果、重试、循环保护、最终交付和终态。调用者不需要知道内部有多少轮。

#### `Conversation.open(command) -> ConversationContext`

装载 system prompt、Agent 配置、历史消息、记忆/技能摘要和 workspace。上下文对象只暴露 `build_request`、`append_assistant`、`append_tool_result`、`append_user`、`compact_if_needed` 和 `workspace`。

#### `LLMClient.stream(request, on_event) -> ModelResponse`

模型适配器统一处理 OpenAI-compatible SSE、content/reasoning delta、tool call 分片聚合、usage 和供应商错误。上层不依赖具体 SDK。

#### `Tool.execute(arguments, context) -> ToolResult`

工具总是返回结构化结果；参数错误、路径违规、超时和执行异常都映射为稳定错误码，不把宿主机异常直接抛给 Agent loop。

#### `EventPublisher.publish(session, event) -> seq`

持久化事件先分配 `seq`，成功后广播；瞬时增量使用 `seq=-1`。SSE 只依赖事件接口，不直接读取运行内部状态。

## 4. Java 到 Python 的映射

| Java 版本 | Python 版本 | 设计取舍 |
|---|---|---|
| `ReActAgentKernel` | `runtime.agent_loop.AgentLoop` | 保留状态机和结果语义，使用 `async def`；不复制 Hook 的七个扩展点，第一阶段只保留 model/tool/run 三类 middleware。 |
| `AgentRunCoordinator` | `runtime.run_coordinator.RunCoordinator` | `asyncio.Lock` 保护会话队列，`Semaphore` 控制全局运行数，`asyncio.timeout` 和 `Task.cancel` 实现超时/取消。 |
| `BoundedVirtualThreadExecutor` | `Semaphore + bounded asyncio.Queue` | Python 的 I/O 并发不需要线程池；明确限制运行数和排队数，队列满返回 429。 |
| `AgentConversation` | `ConversationContext` | 让 loop 不接触数据库、配置和文件系统。 |
| `LLMService` | `LLMClient` | `httpx.AsyncClient` 解析 SSE；tool call arguments 保持原始 JSON 字符串。 |
| `ToolRegistry/Invoker` | `ToolRegistry/ToolInvoker` | Pydantic 做输入校验，统一包装异常和超时。 |
| `ToolScheduler` | `ToolScheduler` | 第一阶段同一模型响应的多个调用按模型顺序串行；以后再按访问范围扩展并行。 |
| `ChatSessionRepository` | `SessionRepository` | SQLAlchemy async + SQLite 起步，保留 PostgreSQL 兼容 SQL；事件 seq 由数据库事务分配。 |
| `ChatEventHub/SseStream` | `EventHub/SSE broadcaster` | FastAPI `StreamingResponse`；订阅先注册、再回放、再 go-live，避免竞态。 |
| `ContextCompactor` | `ContextCompactor` | 首尾保留、中间 handoff 摘要；压缩只改变运行上下文，不重写原始消息历史。 |
| `WorkspacePaths` | `WorkspacePathResolver` | `pathlib` normalize/resolve + realpath 校验，拒绝 symlink escape。 |
| HookRegistry | `Middleware` | 用少量明确的 middleware seam，避免把扩展点暴露成十几个难以测试的回调。 |

## 5. 运行状态机

```text
CREATED
  -> QUEUED
  -> RUNNING
      -> MODEL_CALL
          -> TEXT      -> FINALIZE -> SUCCEEDED
          -> TOOL_CALL -> EXECUTE   -> append tool result -> MODEL_CALL
      -> LIMIT_REACHED (step/budget/loop)
      -> TIMED_OUT
      -> CANCELLED
      -> FAILED
```

`AgentLoop` 的最小规则：

- 每次模型调用前检查 cancellation、deadline 和 budget；
- `ToolCalls` 先发布 assistant content/tool-call，再逐个执行并发布结果；
- 任意 tool call 缺少结果时补一个 `TOOL_RESULT_MISSING` 失败结果；
- 连续相同 `(tool_name, arguments_hash)` 达到提醒阈值时注入 transient system note，达到硬阈值时不执行该调用，进入最终交付；
- 连续多轮工具失败时，下一轮不提供 tools；
- 最终交付调用不带 tools，失败时返回可解释的固定兜底文本；
- 所有终态都发出一个 `done`，持久化异常不能让前端永久等待。

## 6. 持久化模型

第一阶段使用 SQLite，表结构保持简单且可迁移到 PostgreSQL：

- `agents(id, name, system_prompt, model, provider_id, runtime_config_json, created_at, updated_at)`
- `providers(id, type, api_base, api_key_ciphertext, models_json, created_at, updated_at)`
- `sessions(user_id, agent_id, id, title, preview, channel, context_version, created_at, updated_at)`
- `session_messages(user_id, agent_id, session_id, seq, role, content, provider, model, tool_call_id, tool_name, metadata_json, image_urls, created_at)`
- `session_events(user_id, agent_id, session_id, seq, event_type, event_data, created_at)`
- `agent_runs(run_id, user_id, agent_id, session_id, status, error_code, error_message, tool_iterations, owner_id, lease_expiry, fencing_token, created_at, started_at, completed_at)`

消息和事件各自拥有会话内单调 `seq`，不要复用一个序列。assistant 的 tool calls、raw assistant JSON 和 UI metadata 放在 `metadata_json`，tool 消息用 `tool_call_id` 配对。

单进程版本可以先不启用 lease/fencing，但表字段和状态机提前保留；进入多实例时再实现 owner、续租和 fenced 写回，不能把单进程锁描述成分布式一致性。

## 7. API 与 SSE 契约

复用前端所需的核心路径，后端实现不需要兼容 Java 的内部接口：

- `POST /api/chat/stream`：写入 user message，返回本回合 SSE；
- `GET /api/chat/subscribe?agentId=&sessionId=&since=`：事件回放与长连接订阅；
- `GET /api/chat/history`、`GET /api/chat/sessions`；
- `POST /api/chat/steer`：运行中插话，消息进入当前 run 的 steer queue；
- `DELETE /api/runs/{runId}`：显式取消；
- `GET/POST/PATCH /api/agents`：第一阶段的最小 Agent 配置；
- `GET /api/tools`：展示已注册工具。

事件统一为：

```json
{"seq": 12, "type": "tool_call", "data": {"id": "call_1", "name": "read_file", "arguments": "{...}"}}
```

`content_delta` 和 `reasoning_delta` 不落库；`content`、`tool_call`、`tool_result`、`error`、`done` 先写库后广播。SSE 写失败只注销订阅，不取消运行。

## 8. 工具和安全基线

第一阶段内置工具按以下顺序实现：

1. `list_dir`、`read_file`；
2. `write_file`、`edit_file`、`apply_patch`；
3. `web_search`、`web_fetch`；
4. `memory_search`、`load_skill`；
5. `exec` 只在 Docker sandbox 可用且显式开启时提供。

文件工具必须经过统一的 `WorkspacePathResolver`：

- 只允许 workspace 相对路径；
- 拒绝盘符、Unix 绝对路径和 `..` 穿越；
- 对最近已存在祖先做 `realpath`，阻止符号链接逃逸；
- 文件大小、输出字符数和执行时长均有限制；
- 错误结果不泄露宿主绝对路径。

`exec` 不允许在 sandbox 不可用时静默回退宿主机。破坏性命令策略和模型输出脱敏作为独立 middleware 测试。

## 9. 分阶段实施

### M0：骨架与契约

- FastAPI、配置、依赖注入和健康检查；
- 领域模型、端口接口、fake LLM、fake tool；
- 事件 JSON schema 和最小前端适配；
- 单元测试先覆盖 `AgentLoop` 的 text/tool/失败/终态。

### M1：单进程可用链路

- OpenAI-compatible async SSE client；
- SQLite + SQLAlchemy async 持久化；
- `RunCoordinator` 的会话 FIFO、全局 semaphore、超时和取消；
- `read_file`、`list_dir`、`write_file`；
- `/api/chat/stream`、history、subscribe、cancel。

验收：同一会话连续提交按 FIFO 执行；模型断开或工具异常仍收到 `error + done`；SSE 断开重连不会重复持久化事件。

### M2：可解释的 Agent Runtime

- 循环保护、预算、输出退化保护和工具结果截断；
- 上下文 token 估算、handoff 压缩和 provider overflow 重试；
- `web_search/fetch`、记忆文件和按需技能；
- trace/run detail 接口和结构化日志。

### M3：安全与交付能力

- Docker sandbox `exec`；
- 多模态图片附件；
- 敏感信息运行级收集与统一脱敏；
- workspace deliverable 校验和基础 eval cases。

### M4：多实例演进

- Redis/数据库队列；
- lease、续租、fencing token 和过期接管；
- Redis event bus 与多实例 SSE；
- MCP 和渠道运行时。

## 10. 设计决策与面试口径

- 这是 Agora Agent Runtime 的 Python 独立实现，核心展示点是 Agent loop、运行协调、工具安全、上下文管理和可恢复流式事件。
- 不把 Java 的生产指标写成 Python 的运行结果；Python 版的性能只能通过本地压测和可复现实验说明。
- 不把文件读取工具称为 RAG。Agora 的会话记忆是文件/关键词检索；文档 chunk、embedding、向量检索和 rerank 放在独立 Ragent 项目。
- 第一阶段只保留一个真正有深度的 `AgentLoop` 和一个真正有深度的 `RunCoordinator` 接口，外围 adapter 可替换、可 fake，保证测试围绕接口而不是穿透实现。
- Python 版不需要 Java 兼容层。前端依赖的是 HTTP/SSE 数据契约，后端可以直接按 Python 的异步模型重新实现。
