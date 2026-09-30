# 全球化增长内容 Agent：CapCut 公开资料案例

这是一个独立作品项目，用 CapCut 官方公开页面作为产品事实来源，生成英语（美国）和西班牙语（西班牙）的 SEO 页面草稿与社媒文案。项目与 CapCut、剪映或字节跳动没有隶属或合作关系，也没有连接其内部系统。所有草稿都留给人工审稿，不会自动发布。

## 工作方式

`qwen` 是真实模型模式：Ollama 运行 `qwen3:4b-instruct`，模型根据 brief 自主决定何时调用本地 MCP 的 `search_knowledge` 和 `get_editorial_rules`。应用限制工具调用次数、检查工具参数，在模型提交结构化草稿后调用 `check_draft`，生成带事实来源和调用轨迹的审核包。若模型选定了已检索的事实卡和有效正文位置、却把引用句抄错，程序会把引用句对齐到该位置的实际可见文本，并在轨迹记录原句和对齐后的句子。确定性检查覆盖事实卡 ID、禁用短语、无来源数字和引用位置；**引用对齐和规则通过都无法证明语义上得到来源支持**，编辑人员仍需逐条核对。

`offline` 使用人工整理的英、西双语事实意译和固定模板，不调用模型。它用于检查数据、MCP、审核包和拒绝证据不足请求的流程，不能代表 Qwen 的内容质量。`api` 使用同一套模型选工具的 Agent 循环，可连接自有的 Chat Completions 兼容服务；需要设置 `GROWTH_API_BASE`、`GROWTH_MODEL`，若服务启用鉴权再设置 `GROWTH_API_KEY`。兼容服务必须支持工具调用。

`data/knowledge.json` 收录少量已核对的 CapCut 功能事实及短来源片段；`data/editorial_rules.json` 是项目自建演示规则，双语意译也不是 CapCut 官方文案。检索只覆盖这个小知识库，不会实时搜索网络。缺少对应功能事实时应停止生成或要求补充证据。

## Windows 本机启动

准备 Python 3.11 或更新版本，并从 [Ollama 官方 Windows 下载页](https://ollama.com/download/windows)安装 Ollama。Windows 安装版通常会在后台提供本地服务；若使用独立命令行版且服务未运行，另开 PowerShell 执行 `ollama serve`。项目默认模型是 [Ollama 模型库中的 `qwen3:4b-instruct`](https://ollama.com/library/qwen3:4b-instruct)，通过本机 `http://127.0.0.1:11434/v1` 接入。Ollama 官方文档说明了 [本地 Chat Completions 兼容接口](https://docs.ollama.com/api/openai-compatibility)和[工具调用](https://docs.ollama.com/capabilities/tool-calling)。

在项目目录执行首选启动脚本：

```powershell
cd capcut-growth-agent
powershell -ExecutionPolicy Bypass -File .\scripts\start_local.ps1
```

脚本会检查 Ollama、在需要时启动服务并拉取模型；如果项目还没有 `.venv`，它会创建虚拟环境并安装项目依赖，然后启动 Web 页面。首次拉取模型需要网络和磁盘空间。终端显示地址后，在浏览器打开 [http://127.0.0.1:7860](http://127.0.0.1:7860)。页面默认选择“千问真实模型”；可切到“离线模板流程检查”对比运行方式。

也可以手工准备环境并运行：

```powershell
cd capcut-growth-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
ollama pull qwen3:4b-instruct
.\.venv\Scripts\python.exe -m growth_agent serve --host 127.0.0.1 --port 7860
```

如果已有 `.venv` 但缺少 Web 依赖，重跑 `.\.venv\Scripts\python.exe -m pip install -e .`。`ollama ls` 可检查已下载模型；`http://127.0.0.1:11434/v1/models` 可检查模型服务。启动脚本与 Web 服务会占用当前终端，关闭终端即停止 Web 页面。

### 命令行运行与人工决定

`run` 默认使用 `qwen` 模式；`demo` 默认使用 `offline` 模式，跑六条样例 brief，包括一条资料不足的配音宣传请求：

```powershell
.\.venv\Scripts\python.exe -m growth_agent run --id en-auto-captions-tutorial
.\.venv\Scripts\python.exe -m growth_agent run --id es-bilingual-captions --mode qwen
.\.venv\Scripts\python.exe -m growth_agent run --id en-auto-captions-tutorial --mode offline
.\.venv\Scripts\python.exe -m growth_agent demo
```

每次运行写入 `runs/运行编号/bundle.json` 与 `review.md`。`pending_review` 只表示程序规则检查通过，**不表示事实已经由人工批准**。编辑人员查看来源和文案后，可记录决定；这个命令只写 `editor_decision.json`，不会发布内容：

```powershell
.\.venv\Scripts\python.exe -m growth_agent review --run-dir .\runs\YOUR_RUN_ID --decision approved --reviewer YourName --notes "Checked each claim against its source"
```

`GROWTH_QWEN_BASE` 和 `GROWTH_QWEN_MODEL` 可覆盖默认的本地服务地址与模型名；远端 AutoDL 的接入步骤见 [AutoDL 配置](docs/AUTODL_SETUP.md)。

## 评价与边界

`data/demo_briefs.jsonl` 用于演示和流程检查，不是模型准确率样本。`data/eval_briefs.jsonl` 与 `data/eval_gold.json` 提供 24 条冻结的评测输入和预期事实 ID，可运行：

```powershell
.\.venv\Scripts\python.exe -m growth_agent eval-run --mode offline
.\.venv\Scripts\python.exe -m growth_agent eval-run --mode qwen
```

`eval-run` 将每条运行结果和 `report.json` 放在独立的 `runs/eval-...` 目录。去除本机路径后的 v2、v4 报告分别在 [`reports/v2.json`](reports/v2.json) 和 [`reports/v4.json`](reports/v4.json)。报告中的事实 ID 覆盖、禁词命中和正确弃答是**确定性流程指标**，不能证明引文在语义上支持文案，也不能代替人工质量评估。本机 CPU 完整跑 24 条 Qwen brief 可能很慢，可在 AutoDL GPU 上按远端步骤运行。正式比较单次 Prompt、一次性 RAG 与工具 Agent 时，应冻结同一套资料和 brief，并增加盲审、重复运行、耗时与资源记录；当前不报告未经验证的质量提升或 SEO 增长数字。`docs/STATE.md` 记录了 AutoDL 实测结果和版本差异。

[Google 的生成式 AI 内容指南](https://developers.google.com/search/docs/fundamentals/using-gen-ai-content)要求关注准确性、质量与相关性，也提示批量生成无价值页面的风险。[Google 多语言站点指南](https://developers.google.com/search/docs/specialty/international/managing-multi-regional-sites)建议用独立 URL 和 `hreflang` 处理正式发布的多语言页面。本项目只生成草稿；排名、流量和转化需要真实站点及相应数据另行衡量。
