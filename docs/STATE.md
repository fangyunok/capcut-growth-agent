# v0.5.0 项目状态（2026-10-05）

## 当前定位

面向增长团队的中英产品营销文案事实审校，并在人工确认后把该版本文案与用户素材合成字幕视频。默认使用 OBS 官方公开资料演示，核心审校可按 `product_id` 替换资料库；与 OBS 或剪映无合作。旧版 CapCut 英／西草稿生成仍可使用。

## 已验证

- **148 项自动化测试通过**（15 个测试文件）。覆盖模型协议、按产品／功能隔离、缺证据停止、引用与数字检查、版本确认、后台队列、网页提交、媒体路径、检索策略的工厂分发／中英分词／RRF 融合／向量管道、服务层 HTTP 契约、OTEL 与 Prometheus 降级路径、语料版本化与审批门，以及两套审校用例集的结构与标注自洽性。
- **CI 矩阵：Windows + Ubuntu × Python 3.11 / 3.12**；含源码测试、独立目录安装验证、wheel 打包与真实 FFmpeg 集成测试。
- 18 项媒体测试包含真实 FFmpeg 渲染和完整解码。独立媒体示例产出三秒、360×640、24 fps 的模板 MP4，使用程序生成的占位图片；无模型文案、无 TTS、无配音。成品可在 [README](../README.md) 查看。
- 网页提供原文、问题、修订文案和来源对照；同一审核页读到的完整资料快照及文案摘要共同绑定确认。来源在打开页面后改变，旧页面不能确认新资料；确认后修改文本或资料，原确认也失效。
- 后台最多接受 8 条未完成任务、2 条并发，任务状态落盘；重启把未结束任务标记为中断，不自动重复调用。
- 提供模型选工具的 `agent` 与固定一次检索的 `rag` 两种策略，记录调用次数、服务返回的 token 数与运行时间。
- wheel 随包附带 15 份公开示例数据；离开源码目录也能加载资料与 MCP 默认配置。

### 检索链路

五种策略（词法／BM25／向量／混合／混合＋重排）经同一接口切换，**MCP 工具签名不变**，默认仍是原始词法实现以保持基线。知识库 690 张真实事实卡来自 110 个公开 OBS 文档页面，每张卡的引文都由程序校验为来源页面的逐字子串。主题级评测（60 条自然语言查询）：

| 策略 | Hit@5 | 英文 | 中文 | 产品泄漏 | 60 条耗时 |
| --- | --- | --- | --- | --- | --- |
| `lexical` | 10.0% | 20.0% | 0.0% | 0 | 5.5 s |
| `bm25` | 15.0% | 30.0% | 0.0% | 0 | 0.4 s |
| `vector` | 28.3% | 26.7% | 30.0% | 0 | 31.7 s |
| `hybrid` | 31.7% | 33.3% | 30.0% | 0 | 3.4 s |
| `hybrid-rerank` | **43.3%** | **53.3%** | 33.3% | 0 | 114.6 s |

产品泄漏在所有策略上恒为 0（硬约束）。详见 [检索结果](RETRIEVAL_RESULTS.md)。

### 审校用例

`--suite curated` 为开发集（20 条），`--suite test` 为留出集（20 条，写于检索改造之后、未参与任何调参）。两套中英各半、互不重叠，合并后覆盖全部 7 张事实卡；套件完整性由 9 项测试约束（不硬编码条数）。另有结构层对抗扩集 200 条（10 bucket × 20），确定性护栏 100% 通过，并通过 `scripts/bootstrap_ci.py` 计算 bootstrap 95% CI。

### 真实 Qwen 端到端评测（2026-10-05）

模型经 Ollama 运行在 AutoDL 的 RTX 3090 24 GB 上，本机经 SSH 隧道调用，检索走默认词法策略、语料为 7 张精标卡。**两个模型均关闭 thinking**（no-think 变体 + 请求参数双保险），确保在同一输出约束下比较。

| 指标 | qwen3:4b-instruct 开发集 | qwen3:4b-instruct 留出集 | qwen3:14b 开发集 | qwen3:14b 留出集 |
| --- | --- | --- | --- | --- |
| 流程成功 | 9/20 = 45.0% | 15/20 = 75.0% | 19/20 = **95.0%** | 20/20 = **100.0%** |
| 缺证据正确弃答 | 4/4 = 100% | 4/4 = 100% | 3/4 = 75% | 4/4 = **100%** |
| 必须删除的风险短语被删除 | 10/28 = 35.7% | 18/21 = 85.7% | 28/28 = **100%** | 21/21 = **100%** |
| 端到端耗时 | 194 s | 158 s | 793 s | 693 s |

两个硬保证（缺证据弃答、引用结构校验）在两套用例上均满分。**留出集不低于开发集，说明结果未过拟合到开发集**。规模提升 3.5 倍带来开发集 +50.0pp、留出集 +25.0pp 的流程成功率，代价是 4.2 倍延迟。失败模式：4b 的失败几乎全部由 `citation_invalid`（引用结构不合法）导致，14b 仅 1 条失败。详见 [模型规模对照](MODEL_COMPARISON.md)。

**同一留出集上的策略对照**（`agent` 让模型自选工具 vs `rag` 固定检索一次）：让模型自行决定检索时机明显优于固定检索一次。

原始报告见 `reports/model-size-ab.json` 与 `runs/w3-model-size-ab/`（逐条运行包未入库）。

### 服务化与可观测性

- **FastAPI 服务层**：`POST /v1/audits`、`GET /v1/audits/{run_id}`、`GET /v1/healthz`、`GET /v1/readyz`，自动生成 OpenAPI 文档。每个响应回带 `X-Trace-Id` 与 `X-Duration-Ms`，全局并发上限默认 8。基准（200 / 400 请求，无模型）：p50 **53 ms**、p99 **54 ms**、QPS **18.8**、0 错误。13 项服务层测试。详见 [服务化部署](SERVICE.md)。
- **可观测性**：OTEL span（`audit_request` → `retrieval` / `decision` / `approval_gate`）+ Prometheus `/v1/metrics`；缺包时全部降级 no-op，不污染核心依赖。详见 [可观测性](OBSERVABILITY.md)。

### 数据版本化

`scripts/update_corpus.py` 重建版本快照 → `scripts/drift_alert.py` 计算 per-feature 漂移告警 → `scripts/approval_gate.py` 人工审批门（默认拒绝覆盖，需 `--force-replace --sign-by --note`，签名写入永久审计日志）。详见 [数据更新](DATA_UPDATE.md) 与 [数据来源](DATA_PROVENANCE.md)。

## 演进方向

- **扩大模型与策略对照面**。已完成 4B / 14B 规模对照（见上）；下一步按同一套用例扩展到更大模型，以及向量／混合／重排检索下的端到端表现。
- **补上人工语义判定**。`pending_review` 覆盖规则与引用结构，语义支持由人工核查环节判定；下一批结果计划抽样做人工标注，以给出事实准确率口径的指标。
- **扩大用例规模与标注独立性**。两套用例各 20 条，已能验证流程与标注自洽；下一步扩充样本并引入外部标注，使细分维度也能给出统计结论。
- **检索侧继续测量**。已在本机 CPU 上完成 `bge-small-zh-v1.5` 与 `bge-reranker-base` 的五策略对比。下一步在 GPU 上测量 bge-m3 与多语言重排模型，并继续压缩重排延迟。
- **部署与运维**。Docker 配置已提供；下一步补上镜像构建验证与并发压测，并把网页从本地单用户工具扩展为多用户审核。

## 真实模型验证复现

实际使用的配置：AutoDL RTX 3090 24 GB 运行 Ollama，本机经 SSH 隧道调用。注意 Ollama 在**无 GPU 时会回落到 4096 上下文**，因此必须确认 `nvidia-smi` 能看到 GPU 再跑评测。部署与选型细节见 [AutoDL 配置](AUTODL_SETUP.md)。

```powershell
$env:GROWTH_QWEN_BASE = 'http://127.0.0.1:11435/v1'
$env:GROWTH_QWEN_MODEL = 'qwen3:4b-instruct'
.\.venv\Scripts\python.exe -m growth_agent doctor
.\.venv\Scripts\python.exe -m growth_agent audit --id obs-virtual-camera-en --mode qwen
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite curated --strategy agent --mode qwen --output runs\audit-dev-agent
.\.venv\Scripts\python.exe -m growth_agent audit-eval --suite test --strategy agent --mode qwen --output runs\audit-test-agent
```

逐条检查审核包的引用、删改和语言质量，再执行 [评测方案](EVALUATION.md)。旧版 `reports/v2.json`、`reports/v4.json` 的 24 条数据仅评价历史 CapCut 草稿生成，不能移用到这项审校任务。
