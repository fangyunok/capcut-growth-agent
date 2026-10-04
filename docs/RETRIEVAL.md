# 检索链路：从词法匹配到混合检索

事实核查的上限由证据检索决定：如果正确的产品资料没有被召回，后面的审校、改写和人工确认都是在错误的证据上进行的。这份文档说明这一层如何实现、如何评测，以及哪些地方仍然不够好。

## 每一层都可以单独替换

检索被抽成 `src/growth_agent/retrieval/` 包，`GROWTH_RETRIEVAL` 环境变量选择策略。**MCP 工具 `search_knowledge` 的签名没有改动**，因此同一套应用、同一套输出结构可以在不同策略下评测，只有排序模型发生变化。

| 策略 | 实现 | 依赖 |
| --- | --- | --- |
| `lexical` | 加权词面重叠（原始实现，作为基线） | 无 |
| `bm25` | Okapi BM25，中文按字符 bigram 切分 | 无 |
| `vector` | bge 向量 + 余弦相似度 | `[embed]` |
| `hybrid` | BM25 与向量经 RRF 融合 | `[embed]` |
| `hybrid-rerank` | RRF 融合后用 cross-encoder 重排 | `[embed]` |

## 三个设计决策

**1. 保留词法实现，而不是替换它。** 没有固定基线就无法声称混合检索带来了改进。原实现被原样搬进 `retrieval/lexical.py`，行为不变（有回归测试锁定），所有新策略都是相对它测量的。

**2. 用 RRF 融合，而不是加权分数求和。** BM25 分数无上界，余弦相似度有界，直接相加需要一个没有合理取值的归一化常数，而且任一组件被替换后这个常数的含义会静默改变。RRF 只使用排名位次，对量纲差异免疫。

**3. 稠密检索必须配相关性阈值。** 余弦相似度对任意两段文本都有定义，因此向量检索对完全不相关的查询也会返回结果。实测中"quantum chromodynamics"仍返回 3 张卡（0.31–0.34）。若不设下限，本项目的核心保证——缺证据时停止而不是硬答——就会失效。阈值由 `GROWTH_VECTOR_MIN_SCORE` 配置，并单独评测。

## 评测分两级

**卡级**（`retrieval-eval`）：用用例的功能主题作为查询，检查 gold 事实卡是否被召回。指标为 Recall@k、MRR、Hit@1、产品泄漏、缺证据误召回。

**主题级**（`topic-eval`）：60 条自然语言查询，按人提问的方式书写，不使用知识库自身的 slug。命中判定为正确主题出现在 top-k。**结果按语种分组**，因为中文查询面向英文证据正是词法检索完全无法处理的场景。

## 评测数据

- 知识库：690 张事实卡，来自 110 个真实 OBS 公开文档页面，覆盖 84 个主题
- 查询集：60 条（30 英文 / 30 中文），覆盖 30 个主题
- 每张卡的 `source_quote` 都是其来源页面的**逐字子串**，由程序校验

结果以 `docs/RETRIEVAL_RESULTS.md` 为准。所有数字均为本机 CPU 推理实测，未使用 GPU。

## 延迟与批处理

分阶段测量后，瓶颈只有一个：**重排占端到端耗时的 97%**。矩阵相似度几乎免费（690×512 矩阵乘 0.006 秒），稀疏检索在毫秒级。

据此只做了一件无损优化——把多组候选对拍平后按批前向。同进程实测 60 组、831 对候选，从 105.8 秒降到 91.6 秒（**1.16 倍**），**排名完全一致**。

被测量否定的方向：`max_length` 不是杠杆。候选对实际只有 27–104 个 token，512 / 256 / 128 三档的差异落在噪声内，调它只会看起来像做了优化。

**耗时数字不可跨进程比较。** 每次进程启动要加载约 1.2 GB 的模型权重（嵌入 ~100 MB ＋ 重排 1.1 GB），不受向量缓存影响，且随操作系统页缓存与 CPU 状态浮动两三倍。这与 [构建实录](BUILD_LOG.md) 困难 14 是同一件事。

**真实使用路径是"一条文案审校一次"，约 14 对候选、约 1.8 秒，不需要 GPU。** 批量评测的 92 秒也可以接受；只有要验证 bge-m3 的收益时才需要 GPU 资源。

## 已知局限

- **绝对水平仍低**：最好的策略在主题级评测上 Hit@5 也只在三成左右。这说明"句子级卡片 + 自然语言查询"这条链路还不足以支撑生产使用。
- **嵌入模型偏小**：本机使用 `bge-small-zh-v1.5`（CPU 可跑）。换用 bge-m3 或部署到 GPU 预期会有明显提升，但**尚未测量**。
- **标注严格**：主题级查询只接受单一预期主题，而部分查询在实际语义上有多个合理答案（例如限幅与压缩）。因此报告的指标偏低，属于保守估计。
- **语料只有英文原文**：事实卡没有中文意译，中文查询只能依靠嵌入模型的跨语言能力，这也正是词法策略在该分组得分为零的原因。

## 复现

```powershell
# 只安装核心依赖即可跑词法与 BM25
python -m pip install -e .
python -m growth_agent topic-eval --retrieval lexical
python -m growth_agent topic-eval --retrieval bm25

# 向量与混合策略需要可选依赖
python -m pip install -e ".[embed]"
$env:GROWTH_EMBED_BACKEND = 'local'
$env:HF_ENDPOINT = 'https://hf-mirror.com'
python -m growth_agent topic-eval --retrieval vector
python -m growth_agent topic-eval --retrieval hybrid
python -m growth_agent topic-eval --retrieval hybrid-rerank
```

也支持 `GROWTH_EMBED_BACKEND=api` 指向 OpenAI 兼容端点（含自建 Ollama 的 `/v1`）。语料构建见 `growth_agent.corpus` 与 `growth_agent.catalog_builder`。
