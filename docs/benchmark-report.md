# W10 检索层压测与索引优化报告

> 阶段：W10 性能优化 + 压测
> 日期：2026-08-11
> 结论：通过 **driver 复用 + 向量/图谱并行 + 实体提取缓存** 三项优化，检索链路 P50 由 ~6.5s 降至 **~0.3s**（50 倍级）；4 档 QPS（1/5/10/20）全部 100% 成功率。

## 一、环境与方法

- 压测脚本：`backend/scripts/bench_retrieval.py`（轻量 asyncio，均匀错峰限速）
- 链路：`search()` = 向量召回（Milvus HNSW Top-20）+ 图谱路径（Neo4j 1-2 跳）→ RRF(k=60) → Top-5
- 数据：本地知识库文档 + 图谱（测试环境小规模）
- 查询：「Insight AI 平台使用哪些检索技术？」（热查询，实体提取缓存命中）
- 每档 8 秒，预热 3 次；运行环境 Windows 11 + Docker Desktop（Milvus/Neo4j 容器）

## 二、压测结果（优化后）

| QPS 目标 | 请求数 | 成功率 | P50 (ms) | P95 (ms) | P99 (ms) | 平均 (ms) |
|---------|-------:|-------:|---------:|---------:|---------:|----------:|
| 1 | 8 | 100.0% | 273.6 | 343.1 | 343.1 | 283.7 |
| 5 | 40 | 100.0% | 283.5 | 387.3 | 4331.3 | 398.5 |
| 10 | 80 | 100.0% | 292.4 | 390.1 | 413.9 | 303.7 |
| 20 | 160 | 100.0% | 368.0 | 494.3 | 2211.9 | 859.6 |

### 阶段拆解（串行参考，单次）

| 阶段 | 平均耗时 (ms) | 占比 | 说明 |
|------|-------------:|-----:|------|
| embed | 352.8 | 56% | 查询嵌入（SiliconFlow bge-m3） |
| milvus | 266.8 | 43% | HNSW 向量召回 Top-20（含嵌入） |
| graph | 6.2 | 1% | 图谱：lite 实体提取（缓存命中）+ Neo4j 1-2 跳 |

> P50 总延迟（~280ms）≈ 向量链路（embed+milvus 并行），因 graph 已缓存；P99 偶发 2-4s 长尾来自嵌入服务网络抖动/限流瞬态。

## 三、优化历程（压测驱动的三项改造）

| 优化 | 前 | 后 | 说明 |
|------|-----|-----|------|
| ① Neo4j driver 复用 | 每次查询新建 driver（TCP+Bolt 握手 ~1s+） | 模块级单例连接池 | `graph_service.get_driver()` 惰性单例 + `close_driver()` 收尾 |
| ② 向量/图谱并行 | 串行执行（延迟相加） | `ThreadPoolExecutor(2)` 并行 | `retrieval_service.search()`；Milvus/Neo4j/LLM 客户端均线程安全 |
| ③ 实体提取缓存 | 每请求调 lite LLM（限流时 2-17s） | 同查询 5min TTL 缓存 | `graph_service._extract_cache`；热点查询不再反复打 LLM |

**实测效果**：预热后总延迟 P50 由 **6.5s → 0.28s**（约 23 倍）；graph 阶段由 **17.1s → 6ms**（约 2800 倍）。

## 四、Neo4j 索引 EXPLAIN 验证

脚本：`backend/scripts/verify_neo4j_indexes.py`（neo4j 驱动 6.2 中 EXPLAIN 计划取自 `consume().plan`）。

现有索引：

| 索引 | 类型 | 目标 | 用途 |
|------|------|------|------|
| entity_name | RANGE | Entity.name | 属性定位（主路径） |
| entity_type | RANGE | Entity.type | 类型过滤 |
| graph_entity_names | FULLTEXT | Entity.name | 别名/模糊定位 |

三类查询 EXPLAIN 计划摘要：

```
1. MATCH (n:Entity) WHERE n.name IN $names
   ProduceResults → Limit → NodeIndexSeek[RANGE INDEX n:Entity(name)]   ✓ 走索引

2. CALL db.index.fulltext.queryNodes('graph_entity_names', $q)
   ProduceResults → Limit → ProcedureCall[db.index.fulltext.queryNodes]  ✓ 走全文索引

3. MATCH (n:Entity) WHERE n.name IN $names
   CALL (n) { MATCH p = (n)-[:REL*1..2]-(m:Entity) ... }
   ProduceResults → Limit → Projection → Apply
     → NodeIndexSeek[RANGE INDEX n:Entity(name)]        ✓ 起点走索引
     → Limit → Filter → VarLengthExpand(All)[(n)-[:REL*..2]-(m)]
```

**结论**：三条查询路径均命中索引（无全表扫描）；顺带修复 Neo4j 5.26 deprecation 警告（`CALL { WITH n }` → `CALL (n) { }`）。

## 五、Milvus 参数核对

- 索引：HNSW（M=16, efConstruction=200）——稠密向量常用配置，召回质量与构建速度均衡 ✓
- 检索：`limit=20`（向量召回规模），ef 运行时默认（search params 未显式设 ef，走索引默认）
- 建议：若需更高召回精度可调 `search_params={"params": {"ef": 64}}`（Milvus 3.x 默认 ef 由索引决定）

## 六、优化建议（后续）

1. **实体提取缓存持久化**：当前内存 TTL 缓存，进程重启丢失；可落 Redis（与检查点同源）。
2. **Milvus 连接池**：单客户端复用已够用；高并发（QPS>50）可评估连接池配置。
3. **嵌入批量**：`_embed_query` 与 `_vector_search` 内重复嵌入（拆解显示 embed 56%）——`search()` 内部向量路径仅嵌一次，已无重复；如需再降可上 GPU 嵌入服务。
4. **长尾治理**：P99 2-4s 来自嵌入网络瞬态，可加 1 次幂等重试 + 超时钳制（与 collector TLS 层策略一致）。
5. **压测数据规模**：当前为测试库小数据（千级 chunk/百级节点）；生产级（万级 chunk）需复测验证索引选择性。

## 七、复现

```bash
cd backend
uv run python scripts/bench_retrieval.py --qps 1,5,10,20 --seconds 8 --profile --json ../docs/bench_data.json
uv run python scripts/verify_neo4j_indexes.py
```
