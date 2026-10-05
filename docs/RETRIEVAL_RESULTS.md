# 检索策略实测结果

本页记录**实际测量到的数字**与测量口径。

## 测量条件

| 项目 | 取值 |
| --- | --- |
| 知识库 | 690 张事实卡，84 个主题，`data/audit_knowledge_obs.json` |
| 查询集 | 60 条（30 英文 / 30 中文），30 个主题，`data/retrieval_queries.jsonl` |
| 嵌入模型 | `BAAI/bge-small-zh-v1.5` |
| 重排模型 | `BAAI/bge-reranker-base` |
| 推理位置 | 本机 CPU（无 GPU）；嵌入维度 512 |
| 参数 | `top_k=5`，RRF `k=60`，融合候选 10 |
| 判定 | 预期主题出现在 top-5 即命中；泄漏＝返回了非本请求产品的卡 |

## 结果

| 策略 | Hit@5 | 英文 Hit@5 | 中文 Hit@5 | MRR | 产品泄漏 | 60 条总耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| `lexical` | 10.0% | 20.0% | **0.0%** | 0.067 | 0 | 5.5 s |
| `bm25` | 15.0% | 30.0% | **0.0%** | 0.096 | 0 | 0.4 s |
| `vector` | 28.3% | 26.7% | 30.0% | 0.171 | 0 | 31.7 s |
| `hybrid` | 31.7% | 33.3% | 30.0% | 0.182 | 0 | 3.4 s |
| `hybrid-rerank` | **43.3%** | **53.3%** | 33.3% | **0.318** | 0 | 114.6 s |

## 五个观察

**1. 中文查询是引入稠密检索的直接依据。** 事实卡只有英文原文，词法与 BM25 在中文查询上找不到任何可匹配的词元，因此返回空结果。向量检索依靠嵌入模型的跨语言能力达到 30.0%。这条差异是"要不要引入稠密检索"的直接依据。

**2. 重排是最大增益来源。** 融合把 Hit@5 从词法的 10.0% 提到 31.7%，而加上 cross-encoder 重排后进一步提升到 43.3%。英文分组提升更明显：33.3% → 53.3%（+20 个百分点）。原因是 bi-encoder 分开编码查询与文档，只能比较"整体像不像"，而 cross-encoder 联合编码二者，能判断"是否真的回答了这个问题"。

**3. 质量是有代价的，代价可以量化。** 重排把 60 条查询的耗时从 3.4 秒推到 114.6 秒，约 34 倍。这是本项目里最清晰的延迟—质量权衡，也是"重排只作用于融合后的 10 条候选、而不是 690 张全库"的设计依据。

**4. 产品泄漏在所有策略上恒为 0。** 检索层无论哪种实现都强制按 `product_id` 隔离，稠密检索也不例外。这是硬约束，不是可调指标。

**5. 当前基线与演进方向。** 最好的策略在主题级评测上达到 **43.3%**（`hybrid-rerank`），相对词法基线提升 **4.3 倍**，构成一个可复现、可对比的基线。演进方向：引入多语言嵌入（bge-m3）与 GPU 重排，预期进一步提升中文分组的绝对值，并写入同一张对照表。

## 测量条件与后续计划

- **耗时口径：同进程内可比，跨进程不可比。** 每个进程启动都要重新加载嵌入模型（约 100 MB）与重排模型（1.1 GB），这部分不受向量缓存影响，且随操作系统页缓存与 CPU 状态浮动两三倍。同一策略两次运行测得 3.4 s 与 60.3 s 而指标完全相同，就是这个问题。因此**比较改动效果时一律在同一进程内进行**——批处理优化就是这样测出 1.16 倍的。
- **性能口径**：单次运行、单机 CPU，未控制冷启动；重跑请保持同机同配置。
- **指标口径**：命中预期主题表示检索找对了文档；来源在语义上是否支持某句文案，由人工核查环节判定。
- **标注口径**：每条查询只接受一个预期主题，而部分查询在语义上存在多个合理答案（例如限幅与压缩同属动态范围控制）。因此**上表数字是保守估计**，实际语义命中会高于此。
- **下一步测量**：bge-m3 与多语言重排模型预期能提高中文分组的绝对值，计划在 GPU 上补测并写入同一张对照表。
- **口径分离**：本页只评测检索层，与 `audit-eval` 的流程指标是两套独立测量，分开报告、不互相换算。

## 复现

```powershell
python -m pip install -e ".[embed]"
$env:HF_ENDPOINT = 'https://hf-mirror.com'
python -m growth_agent topic-eval --retrieval lexical       --report reports/topic-lexical.json
python -m growth_agent topic-eval --retrieval bm25          --report reports/topic-bm25.json
python -m growth_agent topic-eval --retrieval vector        --report reports/topic-vector.json
python -m growth_agent topic-eval --retrieval hybrid        --report reports/topic-hybrid.json
python -m growth_agent topic-eval --retrieval hybrid-rerank --report reports/topic-hybrid-rerank.json
```

卡级评测（面向 `audit_eval_cases.jsonl` 的 gold 事实 ID）用 `retrieval-eval`。
