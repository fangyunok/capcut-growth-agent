# 项目状态（2026-09-30）

这是基于 CapCut 官方公开资料的独立双语增长内容 Agent 作品，与 CapCut 无合作或内部系统连接。程序只生成待审核草稿，不自动发布。

## 已实现并实测

- 本地 Python 程序通过 Chat Completions 连接 AutoDL 上的 Ollama `qwen3:4b-instruct`；模型自主调用本地 MCP 的 `search_knowledge` 和 `get_editorial_rules`，然后提交结构化草稿。
- 事实库有 16 条官方来源卡；支持英语和西班牙语。应用限制工具、回合、修订次数，对草稿运行 `check_draft`，导出 `bundle.json`、`review.md`、工具轨迹及人工决定记录。
- 缺乏确切功能证据或要求无依据保证时，可输出 `insufficient_evidence`，不产生草稿。引用句抄错时，程序只在模型选定了已检索事实 ID 和有效正文位置的情况下对齐到实际正文，轨迹保留原句；这不证明语义支持。
- AutoDL 实测 GPU：RTX 3090 24 GB。远端 Ollama 模型 `qwen3:4b-instruct` 已拉取，并通过 SSH 隧道在本机 `http://127.0.0.1:11435/v1` 提供接口。本地 Web 已启动于 `http://127.0.0.1:7860`，`/health` 为 `ok`，真实 Qwen 表单 POST 返回 200 和待审核包。隧道及 Web 取决于当前终端会话，断开后按 `docs/AUTODL_SETUP.md` 重启。
- 19 项自动化测试通过。三条独立演示：英文和西班牙语成功生成 `pending_review` 审核包，西语配音夸大请求返回 `insufficient_evidence` 且无草稿。

## 真实模型评测

冻结的 24 条英/西双语 brief 与 gold 标签位于 `data/eval_briefs.jsonl`、`data/eval_gold.json`。指标只量度流程与证据 ID 对齐，不代表人工判定的事实正确率、营销效果或增长收益。

| 版本 | 报告目录 | 流程成功 | 必需证据 ID 覆盖 | 正确弃答 | 禁止短语命中 | 轨迹耗时 P50 / P95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| v2，原始严格引用 | `reports/v2.json` | 18/24 | 14/18 | 6/6 | 0/36 | 6.289 / 17.428 秒 |
| v4，带可审计引用对齐 | `reports/v4.json` | 21/24 | 18/18 | 6/6 | 0/36 | 6.103 / 19.033 秒 |

v4 的 3 个失败是 `eval-04-es`、`eval-05-es`、`eval-08-es`，状态均为 `needs_revision`：模型把位置写成没有索引的 `body`，部分主张也未出现在草稿正文中，程序保留失败而不猜测位置。v3 仅改提示词，首批结果更差，已提前终止，因此没有完整报告。v4 重复使用同一套 brief，属于**回归测试**，不是新的盲测或独立泛化证明；引用对齐会提高结构性指标，但也不能证明引用真正支持文本。

人工抽查 `eval-01-en` 发现“no downloads”“no advanced skills”等营销表述尚需来源核对。这说明待审核状态不能当作可发布状态；正式简历表述应写“构建可审计的草稿与拒答流程、完成 24 条流程回归”，不要写“内容准确率 100%”或“提升流量”。

## 运行与交接

保持远端 Ollama 和 SSH 隧道运行，在项目目录执行：

```powershell
$env:GROWTH_QWEN_BASE = 'http://127.0.0.1:11435/v1'
$env:GROWTH_QWEN_MODEL = 'qwen3:4b-instruct'
.\.venv\Scripts\python.exe -m growth_agent serve --host 127.0.0.1 --port 7860
```

浏览器打开 `http://127.0.0.1:7860`。命令行单条运行：

```powershell
.\.venv\Scripts\python.exe -m growth_agent run --id en-auto-captions-tutorial --mode qwen
```

AutoDL 隧道重建、模型安装与评测命令见 `docs/AUTODL_SETUP.md`。完整运行包在 `runs/` 中（被 `.gitignore` 忽略）；去除本机路径后的两个评测报告位于 `reports/`，可随代码公开。下一步是人工审稿样本与作品演示整理，然后再做第二个项目。
