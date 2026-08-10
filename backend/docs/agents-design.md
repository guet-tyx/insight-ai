# Insight AI 多智能体系统设计文档（W5）

## 一、拓扑结构（Supervisor-Worker 层次化协作）

```
                     ┌──────────────────────────────────────────────┐
                     │           Supervisor Agent（路由中枢）          │
                     │  · 不持有具体工具（Handoff 语义）               │
                     │  · LLM 结构化路由：collector/research/         │
                     │    analyst/finish + 子任务指令                 │
                     │  · 最大循环熔断：MAX_ITERATIONS=6              │
                     └──────┬──────────┬──────────┬──────────┬──────┘
              next=collector│ next=research│ next=analyst│ next=finish
                            ▼          ▼          ▼          ▼
                     ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐
                     │ Collector │ │ Research │ │ Analyst  │ │ END  │
                     │ 子图       │ │ 子图      │ │ 子图     │ │      │
                     │ 私有状态   │ │ 私有状态  │ │ 私有状态 │ │      │
                     └─────┬────┘ └────┬─────┘ └────┬─────┘ └──────┘
                           └───────────┴──────┬──────┘
                                   ── 循环回 supervisor ──
   W8 预留：finish 前插入 human_review 节点（interrupt() 卡点，
   挂起状态机等人工 Command(resume) 恢复）
```

## 二、状态 Schema 与 Reducer 机制

### 全局状态（GlobalState，父图 checkpoint 完整记录）

| 字段 | 类型 | Reducer | 说明 |
|---|---|---|---|
| `messages` | `list[BaseMessage]` | `add_messages` | 消息历史，增量追加不覆盖 |
| `next_node` | `str` | 覆盖 | Supervisor 路由指针 |
| `task_requirement` | `str` | 覆盖 | 用户复合指令（Supervisor 拆解依据） |
| `raw_artifacts` | `list[dict]` | `operator.add` | Collector 采集产出（增长型） |
| `extracted_entities` | `list[dict]` | `operator.add` | Research 实体/片段产出（W7 图谱补全） |
| `final_report` | `str` | 覆盖 | Analyst Markdown 报告 |
| `human_feedback` | `str` | 覆盖 | W8 HITL 反馈预留 |
| `iteration` | `int` | `operator.add`（每轮写 1） | 循环计数 → 熔断 |

### 子图私有状态（隔离，不进入父图 checkpoint）

| 子图 | 私有字段 | 隔离意义 |
|---|---|---|
| CollectorState | `task_requirement / url / raw_artifacts / retry_count / browser_payload` | 重试计数、浏览器执行细节不污染全局命名空间 |
| ResearchState | `task_requirement / query / semantic_chunks / extracted_entities` | 检索中间变量子图内消化 |
| AnalystState | `raw_artifacts / extracted_entities / final_report` | 报告草稿私有，仅终稿上抛 |

父图仅在子图 END 后读取其**暴露字段**（子图输出通道），中间变量天然隔离
（LangGraph 子图 state 通道裁剪）。

## 三、流转时序（一次完整情报分析任务）

```
用户指令 → START → supervisor
  ① supervisor: LLM 意图识别 → next=collector, task="采集 example.com..."
  ② collector 子图: 浏览器采集 → raw_artifacts=[{url, data}] → 回 supervisor
  ③ supervisor: 素材已齐 → next=analyst
  ④ analyst 子图: LLM 生成 Markdown 报告（[1] 引用溯源）→ final_report → 回 supervisor
  ⑤ supervisor: 报告完成 → next=finish → END（W8: 先经 HITL 卡点）
```

## 四、熔断与防御（计划风险表落地）

- **最大 Loop 熔断**：`iteration >= MAX_ITERATIONS(6)` → Supervisor 强制 `finish`
  （防节点无限死循环与 Token 失控）
- 子图异常不抛出：Collector 采集失败 → 产物字段 `{error}`，Analyst 如实报告
- 检查点：**AsyncRedisSaver**（redis-stack-server，RediSearch 模块），
  7 天 TTL；Redis 不可用自动降级 MemorySaver（告警日志）

## 五、与计划第 5 周交付项对照

| 计划交付项 | 落地文件 |
|---|---|
| Supervisor-Worker 协作拓扑结构设计 | `agents/graph.py` + 本文档 |
| SupervisorAgent 路由节点 / 意图识别 / Handoff 链 | `agents/supervisor.py`（with_structured_output 结构化路由替代工具链，一次调用完成决策，省 Token） |
| 全局与私有状态 Schema / Reducer | `agents/state.py` |
| 状态检查点持久化机制 | `core/checkpointer.py`（Redis + Memory 降级） |
| 节点无限死循环熔断 | `supervisor.py` MAX_ITERATIONS + iteration Reducer |
| API 事件流 | `api/v1/agents.py`（POST /runs SSE：stage/token/done/error） |

## 六、已知边界（后续周次演进）

- **W6**：Collector 深化 —— RSS 路由、动态代理池、多步 ReAct（已完成）
- **W7**：Research 深化 —— Neo4j 实体/关系抽取、Cypher 1-2 跳查询、
  RRF 融合（已完成：research 子图检索升级为 hybrid，extracted_entities
  产出含 graph 路径计数，Analyst 可引用图谱路径）
- **W8**：finish 前插入 `human_review`（interrupt + Command(resume)），
  chat 前端接入 HITL 审核界面
- 检查点多 worker 部署：uvicorn 多进程时 AsyncRedisSaver 天然共享（无需
  单例 Agent 模式限制；chat 单 Agent 已在同一检查点体系内）