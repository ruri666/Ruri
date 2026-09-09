# Persistent Life Agent

Persistent Life Agent 是一个持续生活、持续行动并维护长期自传体记忆的 Agent 原型。它不是只在用户发消息时响应的普通 Chatbot：时间由 Heartbeat 推进，行为以 Activity 为单位组织，事实写入 append-only Raw Timeline/Raw Dialogue，长期记忆压缩到人类可读的文本树，再按需构造 Self Context 和 User Context。

默认 persona 是轻小说作家。

## V1 已实现

- 确定性 `Clock`、`TestClock`、Heartbeat 和重启/elapsed-time recovery。
- Large/Medium/Small Goal、Schedule、`reserved_free` State。
- 动态任意深度 Activity Tree。
- Activity 与 Execution Segment 分离；中断会关闭 Segment，但保留原 Activity 为 `interrupted`。
- 可配置 `expected_end_time` 检查和可选、可复现的 seeded random-event hook。
- 完整 Agent/User Raw Dialogue JSONL，支持 record id、范围和时间查询。
- 单文件 Markdown Activity/Temporal/Conversation Memory Tree。
- `core_memory.md` 与 `user_core_memory.md`，只通过显式 merge 更新。
- Live Summary + Final Summary，摘要节点带 Raw/source refs；子 Activity 完成只更新直接 parent。
- Self Context、User Context、Active Activity Path、Temporal Active Path、计划快照和 Raw budget。
- Activity/Temporal/Conversation/Mixed Router，以及 Summary 不足时的 Raw fallback。
- provider-neutral `LLMProvider` 与无网络 `MockLLMProvider`。

## 为什么不用向量数据库

V1 的事实源必须可读、可审计、可通过 Git diff 检查。Activity、Temporal 和 Conversation Memory 各自是一份完整的 Markdown 树文件；Retriever 先判断 Memory 类型，再从粗 Summary 节点向子树展开，最后通过 Raw pointer 回溯 JSONL。没有 FAISS、Milvus、Chroma、embedding database 或独立 Activity index。

## 文件结构

```text
agent_root/
  manifest.json
  config/runtime.json
  plans/goals.json
  plans/schedule.json
  timeline/YYYY/MM/YYYY-MM-DD.jsonl
  state/state.json
  state/activities.json
  memory/
    core_memory.md
    user_core_memory.md
    activity_memory.md
    temporal_memory.md
    conversation_memory.md
    agent_raw_dialogue.jsonl
    user_raw_dialogue.jsonl
```

树文件是“一个文件内的一棵树”，不是目录树。Raw JSONL 只追加，不覆盖；Summary 是派生压缩层，不替代 Raw。

## 运行 Demo

```powershell
$env:PYTHONPATH = "src"
python examples/demo.py --root var/demo_agent
```

Demo 会模拟：起床 → 写第二章大纲 → Heartbeat → 身体不适随机事件 → 中断 → 休息 → replan → 恢复写作 → 用户消息，并在 `var/demo_agent/memory/` 生成全部文件。

## 运行 Web UI

启动一个只监听本机的标准库 HTTP 服务：

```powershell
$env:PYTHONPATH = "src"
python -m artificial_life.webui --root var/web_agent --port 8765
```

然后打开 `http://127.0.0.1:8765/`。页面提供对话、状态、Activity、Goals、最近
Timeline 和显式 Heartbeat 按钮。发送消息会写入 User Raw Dialogue，并通过当前
`LLMProvider` 生成一条 Agent Dialogue；只有点击 Heartbeat（或宿主调用
`agent.heartbeat()`）才会推进生活时间。默认 provider 是无网络的
`MockLLMProvider`，因此回复会显示“已记录”式确定性确认。

API 也可以被其他前端使用：`GET /api/state`、`GET /api/dialogue`、
`GET /api/timeline`、`POST /api/message` 和 `POST /api/heartbeat`。

## 运行测试

```powershell
python -m pytest -q
```

测试覆盖 Raw append-only、树保存/加载、任意深度 Activity、Execution Segment、中断/恢复、Summary direct-parent 更新、Temporal/Conversation Episode、Context 内容、Retriever Raw fallback、Heartbeat expected-end、seeded random hook 和完整 Demo 主链。

## 当前限制

- 默认使用标准库和 `MockLLMProvider`，没有真实 OpenAI/provider adapter。
- Web UI 目前是仅监听本机的标准库服务；没有认证、多用户、分布式运行、数据库或高并发 writer。
- Summary 当前由 Mock provider 生成确定性文本；真实语义总结需后续 adapter。
- User Core 只在显式 `merge_user_core` 时更新，不会未经批准从单条对话自动写入长期事实。
- Random event 默认概率为 0，必须显式配置 seed 和 probability。
- Context 使用字符预算近似 token budget，尚未接入具体 tokenizer。

## Roadmap

1. 增加真实 LLM adapter、typed planning 和 Summary factual validation。
2. 增加更严格的 Summary provenance、冲突处理、checkpoint/replay fault tests。
3. 增加 CLI/daemon 调度器、真实 provider outage fallback 和更完整的 replan policy。
4. 保持文本树为 canonical memory；任何搜索索引都只能是可删除的 derived cache。

架构细节见 [docs/artificial_life_architecture.md](docs/artificial_life_architecture.md) 和 [docs/persistent_life_agent_v1_plan.md](docs/persistent_life_agent_v1_plan.md)。
