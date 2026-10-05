# 模型规模对照：qwen3:4b-instruct vs qwen3:14b

审校链路的端到端质量上限由底层模型决定。这份文档给出同一套 pipeline、同一份语料、同一组 prompt 下，**只切换模型规模**时的质量与延迟变化，作为部署选型的依据。

## 实验设置

| 维度 | 配置 |
|---|---|
| 模型 | `qwen3:4b-instruct`（small）/ `qwen3:14b`（large） |
| 推理后端 | Ollama 0.35.1，单卡 RTX 3090，CUDA |
| 套件 | `curated`（开发集 20 条）+ `test`（留出集 20 条），中英各半 |
| 语料 | `data/audit_knowledge.json`（精标卡，`feature` 与用例一一对应） |
| 检索策略 | `lexical` 基线 |
| Agent 策略 | `agent`（模型自主调用 MCP 工具） |
| 判定口径 | `workflow_success`，由确定性 gold 标签打分 |

### 关于 thinking

qwen3 系列默认开启 chain-of-thought。若放任其输出 `<think>` 块，token 预算会被推理过程吃光、`content` 被截断成非 JSON，导致审校整链路失败。本对照对**两个模型都关闭 thinking**，保证它们在同一输出约束下比较。

关闭需要两处同时生效：为模型创建 no-think 变体（`PARAMETER think false`），并在请求体带 `think: false`。

## 结果

| 套件 | 4b-instruct | 14b | Δ |
|---|---|---|---|
| `curated`（开发集） | 45.0%（9/20） | **95.0%**（19/20） | **+50.0pp** |
| `test`（留出集） | 75.0%（15/20） | **100.0%**（20/20） | **+25.0pp** |

分项指标：

| 指标 | 4b curated | 4b test | 14b curated | 14b test |
|---|---|---|---|---|
| 正确弃答（`correct_abstention`） | 100% | 100% | 75% | 100% |
| 风险措辞清除（`required_risk_removed`） | 35.7%（10/28） | 85.7%（18/21） | **100%** | **100%** |
| 端到端耗时 | 193.9s | 157.7s | 793.1s | 693.2s |

## 失败模式

两档模型的失败原因结构完全不同，这正是规模差异最有信息量的地方：

- **4b：`citation_invalid` 主导**。开发集 11 条失败里有 10 条、留出集 5 条失败里 5 条都伴随引文结构不合法。小模型能识别风险措辞（清除率尚可），但难以稳定产出合法 `claim_uses` 引用结构（事实 ID 与引文位置必须严格对应）。
- **14b：仅 1 条失败**，且是开发集的一条 `abstention_missed`（应在证据不足时弃答却给出了结论）。留出集**零失败**，风险措辞清除率在开发集与留出集上均达 100%。

## 延迟-质量权衡

14b 的总耗时是 4b 的 **4.2 倍**（1486s vs 352s）。结合质量差异，可以给出明确的选型建议：

- **质量优先**（发布前把关、离线批审）→ 14b
- **吞吐优先**（在线草稿预筛、成本敏感）→ 4b，但需接受引文结构校验失败率偏高

## 可复现

```bash
# 远端 Ollama 启动后建立隧道
python scripts/ssh_tunnel.py --ssh-port <port>

# 为两个模型创建 no-think 变体
curl -s http://127.0.0.1:11435/api/create -H "Content-Type: application/json" \
  -d '{"model":"qwen3:14b-nothink","from":"qwen3:14b","parameters":{"think":false}}'
curl -s http://127.0.0.1:11435/api/create -H "Content-Type: application/json" \
  -d '{"model":"qwen3:4b-instruct-nothink","from":"qwen3:4b-instruct","parameters":{"think":false}}'

# 跑对照
.venv/Scripts/python.exe scripts/model_size_ab.py \
  --base http://127.0.0.1:11435/v1 \
  --small qwen3:4b-instruct-nothink --large qwen3:14b-nothink \
  --output reports/model-size-ab.json --run-root runs/w3-model-size-ab
```

完整数值（含 `delta` 与 `by_failure` 分解）见 `reports/model-size-ab.json`。
