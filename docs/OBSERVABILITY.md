# 可观测性

本文档描述 claim-studio 服务层的可观测性能力：trace、metrics、故障定位。

## 安装

```bash
pip install -e ".[observability]"
```

`[observability]` 额外装 3 个依赖：`opentelemetry-api`、`opentelemetry-sdk`、`prometheus-client`。
**不装也能跑**——`telemetry.py` 和 `metrics.py` 在缺包时全部降级为 no-op，
trace 无信息、metrics 端点返回空 body，但服务其余链路完全不受影响。

## Trace（OpenTelemetry）

每次 `POST /v1/audits` 请求被包成一个 root span `audit_request`，
并自动挂上客户端传的或服务端生成的 `trace_id`（与 HTTP header `X-Trace-Id` 一致）。
生产环境配置 OTLP exporter 后能在 Jaeger / Tempo / Honeycomb 看到完整调用链。

### Span 层级

| Span 名 | 父 span | 含义 |
|---|---|---|
| `audit_request` | (root) | 整个审核请求 |
| `retrieval` | `audit_request` | 知识库检索阶段（含 `query_count` 属性） |
| `decision` | `audit_request` | 模型推理阶段（含 `model`、`strategy` 属性） |
| `approval_gate` | `audit_request` | 单道围栏检查（含 `gate` 属性：forbidden_words / cross_product_leak / version_binding / prompt_injection / schema） |

### 关键属性

| 属性 | 含义 | 出现位置 |
|---|---|---|
| `trace_id` | 与响应 header `X-Trace-Id` 一致 | root |
| `product_id` | 审核目标产品 | root |
| `strategy` | `agent` 或 `rag` | root / decision |
| `mode` | `qwen` 或 `api` | root |
| `verdict` | 最终判定（pending_review / needs_revision / insufficient_evidence / model_error） | root |
| `gate` | 单道围栏名 | approval_gate |
| `query_count` | 检索阶段发出的原子声明数 | retrieval |
| `model` | 实际推理模型 | decision |

### 故障定位示例

> 运营反馈"今天通过率掉了 5%"。

1. 在 Tempo 里按 `verdict="needs_revision"` 过滤，看哪些 `gate` 增量最大
2. 再按 `gate="forbidden_words"` 看具体触发的关键词是不是新规则漏网
3. 按 `product_id` 切片看是不是某产品独占异常
4. 锁定根因后用 `trace_id` 在日志系统里拉完整 log

## Metrics（Prometheus）

`GET /v1/metrics` 暴露 Prometheus 文本格式，每次 scrape 拉取最新值。

### 指标表

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `audit_total` | Counter | `strategy`, `verdict` | 已完成审核计数（按 pipeline 策略与最终判定分桶） |
| `audit_latency_seconds` | Histogram | `strategy` | 端到端审核延迟直方图（0.05 ~ 30 秒） |
| `audit_in_flight` | Gauge | — | 当前正在处理的审核数 |
| `gate_failure_total` | Counter | `gate`, `failure_kind` | 各围栏失败计数（按 gate 名与具体原因细分） |

### 常用 PromQL

```promql
# QPS (近 1 分钟)
sum(rate(audit_total[1m]))

# 拒绝率
sum(rate(audit_total{verdict="needs_revision"}[5m]))
  / sum(rate(audit_total[5m]))

# P99 延迟
histogram_quantile(0.99, sum by (le, strategy) (rate(audit_latency_seconds_bucket[5m])))

# 飞涨的某道围栏
sum by (gate, failure_kind) (rate(gate_failure_total[5m]))
```

### 看板建议

| 面板 | PromQL |
|---|---|
| **Throughput** | `sum(rate(audit_total[1m]))` |
| **Reject rate** | `sum(rate(audit_total{verdict="needs_revision"}[5m])) / sum(rate(audit_total[5m]))` |
| **Latency p50/p95/p99** | `histogram_quantile(...)` × 3 |
| **In-flight** | `audit_in_flight` |
| **Top failing gates** | `topk(5, sum by (gate, failure_kind) (rate(gate_failure_total[5m])))` |

## 与现有 stub 的关系

- `X-Trace-Id` header 永远存在（即便没装 OTEL）——用于日志关联。
- `X-Duration-Ms` header 永远存在——客户端可粗略看延迟，无需 scrape Prometheus。
- 服务关闭后 in-flight 计数不会持久化——这是 Prometheus 模型的特性，不修。

## 演进路径

- **OTLP exporter 接线**：当前未预设 exporter；接 Tempo/Jaeger 时在 lifespan 里加 `provider.add_span_processor(OTLPSpanExporter(...))`。
- **结构化日志接入**：当前用 `logging`，接 Loki/ELK 时把 logger handler 切到 `python-json-logger`。
- **告警规则**：建议接 Alertmanager；模板示例：`RejectRate5m > 0.4 → page on-call`。