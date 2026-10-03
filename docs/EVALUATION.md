# 审校评测与基线对照

## 数据和指标边界

`data/audit_cases.jsonl` / `audit_gold.json` 为 5 条开发样例。`audit_eval_cases.jsonl` / `audit_eval_gold.json` 为另行编写的 20 条中英用例：正常产品主张、夸大性能、无来源数字、缺失功能、错误产品及合成的指令注入文本。标注包含预期弃答、必须删除的字面片段与相关来源 ID。

这 20 条用例是在需求及来源可见时设计，**不是盲测、独立评测或未见测试集**。当前没有新版真实模型结果，自动化单元测试中的脚本化模型不得计入模型评测。

程序自动报告：流程状态达标比例、缺证据正确弃答比例、指定风险片段删除比例、引用 ID／文本位置合法比例。分子和分母同时保存，单例异常也留在统计中；批次不会跳过失败请求，已有报告不会覆盖。引用结构合法不代表来源在语义上支持该主张。

## 两种策略如何比较

`agent` 由模型选择 MCP 资料检索与语言规则工具；`rag` 固定执行一次资料检索并读取规则，再要求模型输出审校 JSON。两者共用模型、输入、资料库、输出结构、程序校验和重试上限；资料按请求的产品和功能隔离。缺证据的固定检索不调用模型。这里的资料检索是关键词检索，尚未实现向量、混合检索或 reranker。

先确认模型服务可用，再分别执行。以下路径须为尚不存在的新报告：

```powershell
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy agent --mode qwen --report reports/audit-agent-real.json
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy rag --mode qwen --report reports/audit-rag-real.json
```

每个审核包记录模型名、prompt 版本、策略、耗时、模型／工具调用次数及服务返回的 token 数；未返回 token 时保留 `null`。工具次数包含资料／规则和文案检查调用。比较时需记录服务版本、硬件及是否冷启动；一次运行的耗时不能当作稳定性能结论。自部署服务的 token 数也不能直接等同 API 账单成本。

## 人工评审

对相同输入的两份结果隐藏策略标签，逐句核查：原文主张是否被识别；修订文案的主张是否被链接来源支持；是否误删有用信息或引入新承诺；中文／英文是否适合目标读者。分别记录判定、来源和修改原因。人工确认只绑定已经核对的资料快照与文本。

没有人工评审前，不报告“事实准确率”“可发布率”。没有真实用户实验，不报告转化提升。`reports/v2.json`、`reports/v4.json` 属于旧版草稿生成任务，不能作为新版改进幅度；旧记录见 [历史评测](LEGACY_CAPCUT_EVALUATION.md)。
