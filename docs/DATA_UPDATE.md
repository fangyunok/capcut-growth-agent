# 数据更新与版本管理

本文档描述 claim-studio 知识库的离线/在线更新流程：抓取 → 抽取 → 版本化 → 漂移告警 → 人工审批 → 激活。

## 完整工作流

```text
scripts/update_corpus.py          # 跑抓取 + 抽取 → versions/<name>/
        │
        ▼
scripts/drift_alert.py            # 比对上一版本 → 17 个 alarmed features
        │
        ▼
人工 review diff.json + drift-report.md
        │
        ▼
scripts/approval_gate.py --sign-by <name> --activate <name>
        │   ← 默认拒绝覆盖（exit 3）；要覆盖必须 --force-replace
        ▼
registry.json 写 active / active_signed_by / active_activated_at
        │
        ▼
SIGNATURES.jsonl 追加一行审计记录
```

## 一、生成新版本

```bash
# 离线模式（fixture）：拷贝 data/audit_knowledge_obs.json 作为新快照
# 用于 CI / 演示 / 沙箱。drift_pct 模拟上游编辑抖动
python scripts/update_corpus.py --drift-pct 0

# 在线模式：真去抓 obsproject.com/kb 的公开页面
python scripts/update_corpus.py --mode real --max-pages 110
```

输出：
```
[update] wrote v3: facts=712 +25/-3/~5 in 47.2s -> .../versions/v3
[update] activate with: python scripts/approval_gate.py --sign-by <name> --activate v3
```

每个版本目录固定三件套：

| 文件 | 用途 |
|---|---|
| `facts.jsonl` | 当前版本的全部事实卡（一行一条 JSON） |
| `manifest.json` | 元数据：生成时间、卡数、源 URL 列表 |
| `diff.json` | 与上一版本对比的 added / removed / changed |

## 二、漂移告警

```bash
python scripts/drift_alert.py --threshold 0.20
```

输出 `versions/drift-report.json` + `versions/drift-report.md`：
- 每个 feature 维度的卡数 before/after/delta/delta%
- 超过 `--threshold`（默认 20%）的特征被标记 alarmed
- 当前默认行为：**只生成报告，不阻断**。审批环节才是阻断点。

## 三、人工审批门

```bash
# 第一次激活：当前 active=null，任意版本都可激活
python scripts/approval_gate.py --sign-by your-name --activate v3 --note "JIRA-1234"

# 已有 active 时默认拒绝覆盖（exit 3）
python scripts/approval_gate.py --sign-by your-name --activate v3
# → refusing to replace active version 'v1' with 'v3' without --force-replace.

# 必须显式 --force-replace 才能覆盖
python scripts/approval_gate.py --sign-by your-name --activate v3 \
    --force-replace --note "approved in code review"
```

`SIGNATURES.jsonl` 每行一条：

```json
{"version":"v3","signer":"fangyunok","signed_at":"2026-10-04T13:25:00Z","note":"approved in code review"}
```

## 四、版本目录结构

```
versions/
├── registry.json            # 全版本清单 + 当前 active + 审批人
├── SIGNATURES.jsonl         # 审计日志（每次激活一行，永不删）
├── v1/
│   ├── facts.jsonl
│   ├── manifest.json
│   └── diff.json
├── v2/
│   ├── facts.jsonl
│   ├── manifest.json
│   └── diff.json
├── drift-report.json        # 最近一次 drift_alert 输出
└── drift-report.md
```

## 五、灾难回滚

```bash
# 假设 v3 是当前 active，发现问题要回滚到 v2
python scripts/approval_gate.py --sign-by your-name --activate v2 \
    --force-replace --note "rollback from v3 to v2"
```

回滚本身也会被记入 SIGNATURES.jsonl，整条链路可审计。

## 六、与 CI 的集成建议

```yaml
# .github/workflows/nightly-update.yml
name: nightly-corpus-update
on:
  schedule:
    - cron: '0 3 * * *'
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e .
      - run: python scripts/update_corpus.py --mode real --max-pages 110
      - run: python scripts/drift_alert.py
      - uses: actions/upload-artifact@v4
        with:
          name: corpus-snapshot
          path: versions/
      # 关键：CI 不自动激活，永远走人工审批门
      - run: |
          echo "::notice::New snapshot ready: review versions/drift-report.md and activate via approval_gate.py"
```

> **重要**：CI 不自动激活。新版本必须由人类在看完 `diff.json` + `drift-report.md` 之后显式执行 `approval_gate.py`。

## 七、演进路径

- **真线抓取**：当前已支持 `--mode real`（实跑 `corpus.discover`），但生产环境的请求频率控制、robots.txt 遵守、超时策略可进一步打磨。
- **卡级历史**：当前只比较"卡是否变化"，不存具体字段级 diff；下一步可加 `card_history.jsonl` 记录每次重抓时哪些字段被改。
- **审批人身份验证**：当前 `--sign-by` 仅做记录，不做身份核验；接 SSO/GitHub 身份时在此加断言。
- **撤销 (revoke)**:当前只能"用更早版本覆盖"，不能"撤销一个版本使其永远无法被激活"。下一步可加 `versions/BLOCKED.txt`.