# Agora 设计方案（FastClaw 架构 + Hermes Agent 核心）

> Agora 是独立的 Python Agent Factory。总体架构、资源模型、部署边界和前端产品形态参考同级项目 `../fastclaw`；`frontend/` 中已复制的页面直接沿用其页面和交互。Agent loop、上下文工程、工具容错和记忆/技能装配参考同级项目 `../hermes-agent`。不复制参考项目的具体实现细节。

## 1. 目标与原则

Agora 创建、配置和运行多用户 Agent。每个 Agent 拥有独立身份文件、模型配置、技能、工具策略、工作区和会话；平台负责认证、资源隔离、模型访问、沙箱、事件推送和持久化。

核心原则：

1. FastClaw 决定“平台怎么组织”：Gateway、Agent Manager、Store、Provider、Workspace、Sandbox、Channels、Skills、Plugins。
2. Hermes 决定“单次任务怎么思考”：Context Builder、ReAct loop、tool loop guard、compaction、retry、memory/skill 注入。
3. 前端沿用 FastClaw 的页面和交互，只通过 `frontend/src/lib/api.ts` 连接 Agora；HTTP/SSE 是 Agora 自己定义的前端适配契约。
4. SQLite 优先保证单机可用，接口和表结构为 PostgreSQL、Redis、多实例演进预留边界。

## 2. 参考范围与明确排除

### 2.1 FastClaw 采用的部分

- Agent Factory：Agent、用户、提供商、技能、API key 和配置均为可管理资源；
- Gateway：统一 HTTP API、认证、路由、会话隔离和后台事件；
- Store：数据库作为 Agent/会话/配置/事件的事实来源，技能和大对象可放对象存储；
- Provider：多供应商和 OpenAI-compatible 适配，模型可按 Agent 覆盖；
- Workspace/Sandbox：工作区与执行环境分离，托管部署默认强制沙箱；
- Channels、Scheduler、Plugins、MCP 作为可插拔边界，而非塞进 Agent loop；
- 配置采用“启动参数 + 数据库运行时配置”，不依赖一个不断膨胀的 JSON 配置文件；
- CLI 与 Web 使用同一 Store 和 service 层，避免两套业务逻辑。

### 2.2 Hermes 采用的部分

- `Agent` 的单次运行状态机和多轮工具循环；
- system prompt、身份文件、会话历史、记忆、技能和工具能力的上下文装配；
- 工具调用参数聚合、结果结构化、超时、重试、失败回灌；
- 步数/时间/token 预算、重复调用检测、退化输出保护；
- 上下文压缩（首尾保留 + handoff 摘要），不改写原始消息；
- 文件读取的分页、行号、字符截断、脱敏、重复读取抑制；
- provider/tool/memory/skill adapter 边界。

### 2.3 不复制的内容

不复制 FastClaw 的 Go 实现、Vite/CLI 进程模型或其具体部署代码；不复制 Hermes 的 CLI、TUI、gateway 进程模型。Agora 以 Python async/FastAPI 为实现技术，保留 FastClaw 的产品分层和行为边界。

## 3. 总体架构

```text
Browser / CLI / Channels / External API
                    |
                 Gateway
       auth, routing, SSE, rate limit
                    |
              Application Services
 AgentManager  SessionService  ConfigService  SkillService
                    |
       +------------+------------+-------------+
       |            |            |             |
    Agent        Store       Provider       Workspace
    Manager      (DB)       Registry        Store
       |                         |             |
   AgentRuntime              LLM adapters   Sandbox
   (Hermes loop)              (responses/    (docker/e2b)
                              chat)
                    |
          EventHub + durable events
```

建议目录：

```text
src/agora/
├─ gateway/                 # FastAPI 路由、中间件、认证、SSE
├─ application/             # Agent/session/config/skill/channel service
├─ agent/                    # Hermes-inspired Agent、Context、Compactor、LoopGuard
├─ provider/                # Provider registry、Responses/Chat adapters、retry
├─ store/                   # SQLite/PostgreSQL repository、migration、事务
├─ workspace/               # 路径解析、文件工具、对象存储和历史
├─ sandbox/                 # Docker/E2B executor、hydrate/sync、策略
├─ channels/                # Web 与 Telegram/Discord/Slack 等适配器
├─ skills/                  # 全局/Agent/用户技能加载与安装
├─ plugins/                 # JSON-RPC/MCP 外部工具边界
├─ domain/                  # 资源模型、事件、错误码、策略
└─ main.py                  # 装配 Gateway 和运行时
```

## 4. 资源模型与隔离

FastClaw 风格的资源关系为：

```text
User -> Agent -> (Provider, Skills, Workspace, Policy)
              -> Session(user, channel, chat, project)
              -> AgentRun -> Messages / Events / ToolExecutions
```

- `Agent` 是可配置、可共享、可复制的产品资源，不等同于一个 Python 对象；
- `Session` 按 `(user_id, agent_id, channel, account_id, chat_id, project_id)` 隔离；
- 用户的 `USER.md`、`MEMORY.md` 和会话属于用户分区；Agent 的 `SOUL.md`、`IDENTITY.md`、公共技能按 Agent 共享；
- Agent Manager 负责加载/热更新 Agent 配置，单次运行只持有不可变的 `ResolvedAgentConfig`；
- 所有读写先经过授权和 scope resolver，不能由工具或模型自行拼接数据库/宿主路径。

## 5. Agent 核心（Hermes 边界）

### 5.1 对外接口

```python
Agent.run(command: RunCommand, sink: EventSink) -> RunResult
Context.open(command) -> ConversationContext
LLMClient.complete(request) -> ModelResponse
Tool.execute(arguments, context) -> ToolResult
```

`Agent` 不依赖 FastAPI、SQLAlchemy 或具体 Provider。Gateway 通过 application service 创建运行并订阅事件；Store、Provider、Workspace、Sandbox 由 ports 注入。

### 5.2 单次运行状态机

```text
CREATED -> QUEUED -> RUNNING -> MODEL_CALL
                         |          |
                         |          +-- TEXT -> FINALIZE -> SUCCEEDED
                         |          +-- TOOL_CALL -> EXECUTE -> MODEL_CALL
                         +-- CANCELLED / TIMED_OUT / LIMIT_REACHED / FAILED
```

规则：每轮检查取消、deadline、预算；工具异常转换为可回灌的结构化失败结果；所有终态持久化并发送唯一 `done`。同一会话 FIFO，跨会话由全局并发 semaphore/queue 控制。

Hermes 的 loop guard 检测重复 `(tool, arguments_hash)`、连续失败和无进展文本；达到硬阈值时停止工具并执行一次无工具最终合成。最终合成失败返回明确的失败状态，不能伪造成功。

### 5.3 Context Engine

上下文按以下顺序装配：系统策略 → Agent 身份文件 → 工具目录 → 技能摘要 → 用户记忆 → 会话历史 → 当前消息。超过 token/字符预算时执行压缩，保留最新任务和必要工具结果，生成 handoff 摘要；原始消息和事件永不被压缩覆盖。

### 5.4 长期记忆与后台自我进化

Agora 采用 Hermes 的“后台复盘”思路实现 Agent 的长期记忆能力。这里的自我进化不是修改 Agent 代码，而是从已完成的对话中提炼可跨会话复用的信息，并在后续会话中重新注入。

记忆分为两个作用域：

- `user_memory`：用户身份、长期偏好、沟通方式、工作习惯和明确要求；
- `agent_memory`：项目事实、环境约定、工具经验和经验证的工作方法。

每个用户回合完成后递增记忆复盘计数，达到 `review_interval_turns`（默认 10）时，异步启动一个独立的 Background Review Agent。它接收当前会话的只读消息快照，判断是否存在值得长期保存的内容，并且只允许调用记忆管理工具。复盘过程不写入主会话、不影响当前回复，失败、超时或被新回合取消都不能阻塞用户请求；复盘 Agent 不得再次触发复盘，避免递归。

复盘提示重点判断：用户是否表达了稳定偏好或个人信息、是否明确要求记住某项内容，以及当前任务是否产生了未来仍有价值的项目经验。一次性任务叙述、未验证的失败尝试和临时环境问题不应进入长期记忆。

记忆在新会话启动时加载，经过安全扫描、脱敏和大小限制后生成冻结的 memory snapshot，再注入 system/context。当前会话内新写入的记忆默认不修改已发送的 system prompt，以保持 prompt cache 稳定。会话切换、会话结束或 Agent 回收时，再执行一次会话级提炼，确保不足一个复盘周期的最后一段对话不会丢失。

记忆写入必须经过 Store service，支持新增、替换、删除、去重、版本校验、审计和用户主动编辑；不能让后台 Agent 直接写宿主路径。所有记忆严格按 `(user_id, agent_id)` 隔离，跨用户和跨 Agent 不共享，外部记忆 Provider 通过同一 MemoryProvider port 接入。

## 6. Provider 与模型协议

Provider Registry 支持 OpenAI Responses、Chat Completions 及其他 adapter。Provider 配置保存在 Store（API key 加密或引用环境变量），启动环境只提供端口、存储、沙箱等 bootstrap 参数。

当前 `api.aijws.com/v1` 的 Responses 请求采用：

```json
{"model":"gpt-5.6-luna","input":"...","stream":false}
```

适配器必须兼容字符串 `input` 和消息数组两种形式，并从 `output_text` 或 `output[].content[].text` 提取文本。连接错误、HTTP 429/5xx 使用有上限的退避重试；HTTP 4xx 返回稳定 provider 错误，不回退 mock 数据。

## 7. Gateway、API 与事件

Gateway 只做认证、授权、DTO 映射、限流和事件传输。前端继续直接复用 FastClaw 的 `frontend/` 页面，只在 `frontend/src/lib/api.ts` 维护请求/响应映射。

首批契约：`/api/status`、`/api/me`、`/api/agents`、`/api/tools`、`/api/chat/stream`、`/api/chat/subscribe`、`/api/chat/history`、`/api/chat/sessions`、`/api/runs/{id}` 取消。它们是 Agora API，由当前前端适配层使用，不要求参考项目的 URL、数据库或响应对象兼容。

持久化事件使用会话内单调 `seq`；`content_delta` 是不落库的瞬时事件，`content/tool_call/tool_result/error/done` 先写库后广播。SSE 订阅先注册实时监听、再回放 `since` 之后的事件、最后切换 live，避免丢事件和重复消费。

## 8. Store、Workspace 与 Sandbox

核心表：`users`、`agents`、`providers`、`configs`、`skills`、`memories`、`memory_review_runs`、`sessions`、`session_messages`、`session_events`、`agent_runs`、`tool_executions`、`api_keys`。SQLite 是默认实现，Repository 不向上层暴露 SQL；PostgreSQL 和 Redis 只替换 adapter。

Workspace 负责相对路径、symlink escape、大小/时间限制、原子写入和版本校验。`read_file` 采用 offset/limit、稳定行号、完整行字符截断、`next_offset`、sha256、敏感内容脱敏和重复读取抑制；`write_file/edit_file/apply_patch` 使用 `expected_sha256` 和两阶段校验。`exec` 只能通过 Sandbox，禁止无沙箱时静默执行宿主命令。

Sandbox 由 per-user/agent/session executor pool 管理。Docker 为本地默认，E2B 为云端 adapter；hydrate/sync 负责工作区与远程沙箱同步，运行时不把沙箱实现泄漏到 Agent loop。

## 9. 分阶段路线

### M0：FastClaw 资源骨架

建立 Gateway、Application Service、Agent Manager、Repository ports、Provider Registry；迁移现有 SQLite 数据；`frontend` 继续使用现有页面；补认证、Agent CRUD、status/capabilities。

### M1：Hermes Agent 可用链路

实现 Context Engine、Responses/Chat provider、Agent loop、工具注册和 `list_dir/read_file/write_file/edit_file`；完成会话 FIFO、取消、预算、事件持久化和 SSE 恢复。验收以真实 `gpt-5.6-luna` 请求为准，不允许 mock 成功。

### M2：平台能力

加入 Agent/用户技能层、用户记忆与 Agent 记忆、后台记忆复盘、会话结束提炼、web tools、provider 重试和上下文压缩；实现 API key、运行时配置、CLI 与 Web 共用 service。首期长期记忆以结构化 Store 为事实来源，文件导入/导出作为可选适配，不把全量历史会话周期性重放给模型。

### M3：隔离与扩展

接入 Docker/E2B sandbox、MCP/plugin、channels、scheduler、对象存储和多模态附件；增加敏感信息审计、usage/quota 和 trace。

### M4：多实例

PostgreSQL + Redis queue/event bus、lease/fencing、跨实例 SSE、运行接管和水平扩展。单进程锁不宣称分布式一致性。

## 10. 关键决策

- FastClaw 是总体架构和前端产品形态来源；Hermes 仅是 Agent 核心运行时来源。
- `/api/chat/*` 只是当前前端适配契约，不代表对参考项目后端接口的兼容承诺。
- Agent loop、Store、Provider、Workspace、Sandbox 均通过稳定端口隔离，便于 fake 测试和替换实现。
- 长期记忆采用异步后台复盘：按完成回合计数触发，不做无界全量历史扫描；后台复盘只读会话快照、受限工具集和独立预算，记忆写入按用户与 Agent 隔离并可审计、撤销。
- RAG（切分、Embedding、向量检索、rerank）不塞入 Agora；Agora 的 memory 是会话/文件级能力。
- 任何 provider 不可达或模型不支持都必须返回可解释错误和 `done(state=failed)`，绝不返回 mock 数据。
