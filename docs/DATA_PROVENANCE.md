# 数据来源声明 (Data Provenance)

本文档说明 `claim-studio` 仓库内所有事实卡的来源、校验流程与可对外公开的依据。理解"数据从哪来、怎么校验、能不能公开"是评估本项目可信度的基础。

---

## 1. 总览

| 字段 | 值 |
|---|---|
| 数据来源 | 第三方公开技术文档（OBS Project 官方知识库） |
| 抽取方式 | 模型无关的程序化管线（BFS 抓取 + 句子筛选 + 原文逐字校验） |
| 卡数量 | 自动抽取库 690 张 / 84 主题；早期精标库 7 张（保留为开发样本） |
| 是否含 PII | 否 |
| 是否含内部数据 | 否 |
| 是否可对外公开 | **是**（已在 GitHub 公开仓库内） |

---

## 2. 数据从哪来

### 2.1 主库：`data/audit_knowledge_obs.json`

- **抓取目标站**：`obsproject.com` 的官方知识库（公开访问、无登录、无反爬限制）
- **抓取方式**：BFS 广度优先，从 5 个种子页面出发，遵守站点 `robots.txt`，**只抓静态 HTML**（不调用任何登录态接口）
- **覆盖范围**：导出、录制、直播、AI 滤镜、音频、转场、快捷键等 84 个文档主题，共 110 个真实页面
- **抽取流程**：
  1. `src/growth_agent/corpus.py` — HTML 解析、正文提取、URL 去重
  2. `src/growth_agent/catalog_builder.py` — 句子级筛选（60~400 字符、能力动词判定）+ 去重

### 2.2 精标库：`data/audit_knowledge.json`

- **7 张人工精标卡**：早期开发期手写，用于演示与开发期单测
- 用途：保留为开发样本与回归用例，不作为生产评测语料

---

## 3. 三重校验流程（每张卡都过）

| 校验项 | 实现位置 | 通过标准 |
|---|---|---|
| **逐字子串校验** | `catalog_builder.quote_is_verifiable` | `source_quote` 必须是 `source_url` 页面的逐字子串，找不到就丢弃 |
| **产品白名单** | `product_id ∈ {obs-studio}` | 不在白名单的页面丢弃，防止混进无关产品 |
| **去重** | 按 `source_quote` 哈希 | 同一原文只保留一条，避免重复卡 |

> 关键设计：抽取管线 **不调用任何 LLM**。它只做机械匹配，因此可复现、零成本、不会幻觉。

---

## 4. 每张卡的字段含义

```json
{
  "id": "obs-export-002",
  "product_id": "obs-studio",
  "product_name": "OBS Studio",
  "feature": "export",
  "topic": "导出参数",
  "claim": "OBS 支持最高 4K / 60fps 的视频导出",
  "source_quote": "...支持最高 4K / 60fps 的视频导出...",
  "source_url": "https://obsproject.com/kb/export",
  "checked_at": "2026-10-04"
}
```

| 字段 | 含义 |
|---|---|
| `id` | 卡编号（一个事实一个 id） |
| `product_id` / `product_name` | 所属产品（白名单校验的硬约束） |
| `feature` | 来源页面的 URL slug，可直接定位回一篇原文 |
| `topic` | 主题分类（导出 / 直播 / AI …） |
| `claim` | 该卡"声称"的事实（一句话） |
| `source_quote` | 原文逐字引用（必须是 `source_url` 页面的真实子串） |
| `source_url` | 文档出处链接（可点开复核） |
| `checked_at` | 程序校验日期 |

---

## 5. 可以对外公开的依据

| 维度 | 结论 |
|---|---|
| **数据来源** | OBS Project 官方公开文档，无版权限制、可公开引用 |
| **逐字子串** | 每张卡的 `source_quote` 都链接到 `source_url`，任何第三方可独立复核 |
| **无 PII** | 抓取过程不涉及用户账号、邮箱、手机号等个人信息 |
| **无内部信息** | 不含 OBS Project 内部 SOP、未公开的 NDA 内容、API 密钥 |
| **自动化校验** | 抽取流程代码开源（`corpus.py` + `catalog_builder.py`），可被独立审计 |
| **不混产品** | 产品白名单硬约束，不会把 capcut / 剪映 / 其他产品的内容混进来 |

---

## 6. 引用本数据集的推荐方式

```text
Fact cards sourced from OBS Project public documentation (obsproject.com),
extracted via model-free BFS crawl + verbatim quote validation. Each card's
source_quote is a literal substring of its source_url.
```

---

## 7. 已知边界与演进方向

| 边界 | 演进方向 |
|---|---|
| 单产品覆盖（仅 OBS） | 接入更多产品公开文档（须先与各产品站点的 robots.txt 合规） |
| 静态快照（无持续更新） | 建立 `versions/v1/v2/...` 快照机制与漂移告警（见 `docs/SERVICE.md` 路线图） |
| 仅文本事实卡 | 引入多模态事实卡（截图、表格、参数表）以覆盖视觉化能力描述 |
| 程序抽取无人工复核 | 引入人机协作流程：自动抽取 → 抽样人工复核 → 不通过则丢弃 |

---

## 8. 复现方式

任何人可在自己环境复现：

```bash
# 1. 拉取页面
python -m growth_agent.corpus --out runs/corpus/

# 2. 抽取并校验
python -m growth_agent.catalog_builder \
    --corpus runs/corpus/ \
    --out data/audit_knowledge_obs.json
```

每张卡都能在 `source_url` 页面上找到对应的 `source_quote` 原文。