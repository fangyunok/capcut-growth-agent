# 审校评测与基线对照

## 数据和指标边界

三套输入，角色不同：

| 文件 | 角色 | 条数 |
| --- | --- | --- |
| `audit_cases.jsonl` / `audit_gold.json` | 演示样例，用于跑通流程（`--suite dev`） | 5 |
| `audit_eval_cases.jsonl` / `audit_eval_gold.json` | **开发集**（`--suite curated`） | 20 |
| `audit_test_cases.jsonl` / `audit_test_gold.json` | **留出集**（`--suite test`） | 20 |

三套都覆盖正常产品主张、夸大性能、无来源数字、缺失功能、错误产品及合成的指令注入文本。标注包含预期弃答、必须删除的字面片段与相关来源 ID。每个套件内中英各半，两套互不重叠，也不与演示样例重叠。

**为什么要分开开发集与留出集**：开发集在调整 prompt 与规则时可以反复查看；留出集写于检索改造完成之后，**未参与本轮任何调参决策**，因此它的数字可以按样本外结果报告。

**但它仍然不是盲测。** 留出集的作者在编写时能看到来源卡片，所以它由本项目自行标注，不是独立第三方评测，也不是未见数据。自动化单元测试里的脚本化模型不得计入模型评测。

```powershell
# 开发集：调 prompt 时使用
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy agent --mode qwen --output runs\audit-dev-agent

# 留出集：只在报告最终数字时跑，且报告后不应据此改 prompt
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite test --strategy agent --mode qwen --output runs\audit-test-agent
```

报告写入 `--output` 目录下的 `report.json`；每条用例的运行包也在同一目录中，便于逐条人工核对。

程序自动报告：流程状态达标比例、缺证据正确弃答比例、指定风险片段删除比例、引用 ID／文本位置合法比例。分子和分母同时保存，单例异常也留在统计中；批次不会跳过失败请求，已有报告不会覆盖。引用结构合法是进入人工核查的前提，语义支持在人工环节判定。

## 两种策略如何比较

`agent` 由模型选择 MCP 资料检索与语言规则工具；`rag` 固定执行一次资料检索并读取规则，再要求模型输出审校 JSON。两者共用模型、输入、资料库、输出结构、程序校验和重试上限；资料按请求的产品和功能隔离。缺证据的固定检索不调用模型。资料检索提供五种可切换策略：`lexical`、`bm25`、`vector`、`hybrid`、`hybrid-rerank`，由 `--retrieval` 选择。

先确认模型服务可用，再分别执行。以下路径须为尚不存在的新报告：

```powershell
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy agent --mode qwen --report reports/audit-agent-real.json
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy rag --mode qwen --report reports/audit-rag-real.json
```

每个审核包记录模型名、prompt 版本、策略、耗时、模型／工具调用次数及服务返回的 token 数；未返回 token 时保留 `null`。工具次数包含资料／规则和文案检查调用。比较时需记录服务版本、硬件及是否冷启动——同机同配置下的重跑才具可比性。自部署服务的 token 数也不直接等同 API 账单成本。

## 人工评审

对相同输入的两份结果隐藏策略标签，逐句核查：原文主张是否被识别；修订文案的主张是否被链接来源支持；是否误删有用信息或引入新承诺；中文／英文是否适合目标读者。分别记录判定、来源和修改原因。人工确认只绑定已经核对的资料快照与文本。

报告口径：流程指标（规则通过率、缺证据弃答率、风险片段删除率、引用结构合法率）由程序自动统计；事实准确率与可发布率需在人工评审后给出。`reports/v2.json`、`reports/v4.json` 属于旧版草稿生成任务，口径不同；旧记录见 [历史评测](LEGACY_CAPCUT_EVALUATION.md)。

## 真实模型结果（2026-10-04 实测）

模型 `qwen3:4b-instruct`（Q4_K_M）运行在 AutoDL 的 RTX 3090 24 GB 上，经 Ollama（上下文 8192），本机 SSH 隧道调用，检索为默认词法策略。

| 指标 | 开发集 20 条 | 留出集 20 条 |
| --- | --- | --- |
| 流程成功（规则通过） | 8/20 = 40.0% | 13/20 = 65.0% |
| 缺证据正确弃答 | 4/4 = **100%** | 4/4 = **100%** |
| 必须删除的风险短语被删除 | 13/28 = 46.4% | 16/21 = 76.2% |
| 引用结构合规 | 7/7 = **100%** | 11/11 = **100%** |

执行期异常 0 条。**留出集高于开发集，说明结果未过拟合到开发集。**

**失败根因**：未通过用例的轨迹显示 `proposal_invalid`（`ValueError`）连续 3 次后触发返修上限——即模型输出的 JSON 未通过 schema 校验，**不是检索或弃答失效**。这些用例中检索到的证据卡是正确的。瓶颈在**结构化输出的稳定性**。

**策略对照（同一留出集）**：`agent` 让模型自选工具，流程成功 65.0%、风险短语删除 76.2%；`rag` 固定检索一次，为 35.0%、28.6%；缺证据弃答与引用结构两边同为 100%。**让模型自行决定检索时机明显优于固定检索一次。** 报告见 `reports/audit-test-rag-report.json`。

**下一步对照计划**：更大模型（14B 及以上）、向量／混合／重排检索下的端到端表现，均可在现有评测命令下直接切换，用例与指标口径不变。单条审校约 15 秒（约 5 次模型调用）。

原始报告：`reports/audit-dev-agent-report.json`、`reports/audit-test-agent-report.json`（逐条运行包在 `runs/` 下，未入库）。

## 检索层评测（独立于上面的流程评测）

上面的指标评价的是**审校流程**是否走对。检索质量是另一件事，用两个独立命令测量，两套口径**分开报告**：

- `retrieval-eval` — 卡级：用用例的功能主题作查询，检查 gold 事实卡是否被召回（Recall@k、MRR、Hit@1、缺证据误召回）。
- `topic-eval` — 主题级：60 条自然语言查询（30 英文 / 30 中文），检查正确文档主题是否进入 top-k，并按语种分组报告。

两种命令都用 `--retrieval` 选择策略（`lexical` / `bm25` / `vector` / `hybrid` / `hybrid-rerank`），因此可以在**同一输入、同一输出结构**下比较五种实现。实测数字与测量条件见 [检索结果](RETRIEVAL_RESULTS.md)，设计说明见 [检索链路](RETRIEVAL.md)。

检索层的产品泄漏（返回非本请求产品的卡）是硬约束，任何策略都必须为 0。
