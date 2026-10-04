# AutoDL 上运行开源权重 Qwen 审校 Agent

本地保留项目代码、MCP 知识库、Web 页面和审核包；AutoDL 只运行模型服务。`growth_agent audit` 用 Qwen 经 Chat Completions 自主选择产品资料检索和编辑规则工具，本地程序执行工具、生成审校建议并做确定性核验，结果进入人工审稿。服务器无需上传本项目的数据或代码。

**部署状态（2026-10-04）：** 实例为 RTX 3090 24 GB，已安装 Ollama 并拉取 `qwen3:4b-instruct`；新版审校已通过本机 `11435` SSH 隧道完成真实端到端评测（开发集与留出集各 20 条，结果见 [项目状态](STATE.md)）。以下命令用于实例连接或新实例部署；已有模型时无需重复下载。

先在 AutoDL 控制台开一台 Linux GPU 实例，选择能运行目标模型的镜像，并复制控制台给出的 SSH 主机与端口。先短时验证，实际机器与租价以控制台当时显示为准。[AutoDL SSH 说明](https://api.autodl.com/docs/ssh/) · [AutoDL 计费说明](https://www.autodl.com/docs/price/)

## GPU 选型与时间预算

**显存需求**：`qwen3:4b-instruct` 是 Q4 量化、文件约 2.5 GB，8K 上下文下的运行区间约 3–4 GB 显存。**8 GB 以上的卡都能跑**，本项目历史使用 RTX 3090 24 GB。

| 卡型 | 参考租价（2026-10 查询） | 参考生成速度 | 建议 |
| --- | --- | --- | --- |
| RTX 3090 24 GB | 约 1.5–2 元/时 | 约 36 tok/s | **性价比首选，本项目够用** |
| RTX 4090 24 GB | 约 2.3–3.5 元/时 | 约 48 tok/s | 需要更快时再选 |
| 无卡模式 | 约 0.1 元/时 | — | **准备与下载阶段用它** |

租价与速度随平台和时间变化，以控制台当时显示为准。按秒计费，**关机不计费**。

**时间预算**（按 3090 估算，实际取决于模型工具调用是否稳定）：

| 阶段 | 计费模式 | 预计 |
| --- | --- | --- |
| 装 `zstd`、装 Ollama、检查 GPU | 无卡模式 | 10–15 分钟 |
| `ollama pull qwen3:4b-instruct`（约 2.5 GB） | 无卡模式 | 5–15 分钟 |
| 冒烟测试：`doctor` ＋ 单条 `audit` | GPU | 3–5 分钟 |
| `audit-eval --suite curated`（开发集 20 条） | GPU | 10–25 分钟 |
| `audit-eval --suite test`（留出集 20 条） | GPU | 10–25 分钟 |
| 调试余量 | GPU | 30–60 分钟 |

**合计约 1.5–2 小时 GPU 计费时间，3090 上约 3 元。** 含准备的挂钟时间约 2–3 小时。

**省钱要点**：装环境、拉模型、传文件全部在**无卡模式**下做完，再切到 GPU 计费；跑完立刻关机。数据盘在关机后可能继续计费，任务结束请一并释放。

**风险提示**：4B 模型的工具调用与结构化输出稳定性有限，可能出现不调用工具、JSON 校验失败或触发返修上限。这**正是本次端到端验证要测量的内容**，不是流程失败——`run_audit` 会把每种情况落盘成不同 `status`，统计时保留它们的分母。

## 路径一：AutoDL 上运行 Ollama

### 1. 在 AutoDL 终端安装和启动

通过 AutoDL 提供的 SSH 命令进入实例，检查 GPU，并按 [Ollama Linux 安装文档](https://docs.ollama.com/linux)安装：

```bash
nvidia-smi
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y zstd
curl -fsSL https://ollama.com/install.sh | sh
curl -fsS http://127.0.0.1:11434/api/tags
```

当前使用的 AutoDL 镜像缺少 `zstd`，因此安装前需要补齐。也可把项目中的 `scripts/autodl_model_setup.sh` 上传到服务器并执行；脚本检查 `zstd`、Ollama 服务和模型是否已存在。

安装脚本可能已启动 Ollama 服务。如果最后一条命令能返回模型列表，就不要再次运行 `ollama serve`。如果服务未启动，在远端终端执行下面两条；`ollama serve` 会持续占用该终端。AutoDL 建议用 `tmux` 或 `screen` 保持长时间运行的程序。[AutoDL SSH 说明](https://api.autodl.com/docs/ssh/)

```bash
tmux new -s growth-ollama
ollama serve
```

在 `tmux` 内按 `Ctrl+B`、`D` 可返回普通终端。另开一个远端终端下载模型并检查服务：

```bash
ollama pull qwen3:4b-instruct
ollama ls
curl -fsS http://127.0.0.1:11434/v1/models
```

`qwen3:4b-instruct` 是 [Ollama 官方模型库中的模型标签](https://ollama.com/library/qwen3:4b-instruct)。首次下载和载入需要时间；能否稳定运行取决于实例的显存、内存和网络。

### 2. 在 Windows 本机建立 SSH 隧道

本机 PowerShell 中，把主机名和端口改为 AutoDL 控制台显示的值。这里用本机 `11435` 转发到远端 `11434`，避免本机 Ollama 已占用 `11434`：

```powershell
$sshHostName = "替换为AutoDL控制台显示的主机名"
$sshPortNumber = 12345
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:11435:127.0.0.1:11434 -p $sshPortNumber "root@$sshHostName"
```

保持该窗口打开。在第二个本机 PowerShell 窗口检查：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11435/v1/models"
```

如果本机 `11434` 空闲，也可以把隧道左侧端口改成 `11434`，并把下面的 `GROWTH_QWEN_BASE` 改为 `http://127.0.0.1:11434/v1`。[AutoDL SSH 隧道文档](https://api.autodl.com/docs/ssh_proxy/)给出相同的本地端口转发方式。

### 3. 本机运行 Agent

项目依赖按 [README 本机启动步骤](../README.md)安装。在第二个 PowerShell 窗口设置环境变量并运行一条审校样例：

```powershell
cd claim-studio
$env:GROWTH_QWEN_BASE = "http://127.0.0.1:11435/v1"
$env:GROWTH_QWEN_MODEL = "qwen3:4b-instruct"
.\.venv\Scripts\python.exe -m growth_agent audit --id obs-virtual-camera-en --mode qwen
```

也可以在同一个窗口启动 Web 页面，然后打开 `http://127.0.0.1:7860`；网页的“千问真实模型”会使用刚设置的远端服务：

```powershell
.\.venv\Scripts\python.exe -m growth_agent serve --host 127.0.0.1 --port 7860
```

查看输出的 `audit_review.md`、`audit_bundle.json` 和工具调用轨迹。`pending_review` 表示待人工审稿；即使规则检查通过，也要核对每条产品主张与链接来源。结束时关闭隧道和模型服务，并在 AutoDL 控制台检查实例状态与计费。

旧版从零生成流程的 24 条评测保留供历史对比；它不评价新版审校任务：

```powershell
.\.venv\Scripts\python.exe -m growth_agent eval-run --mode qwen
```

## 路径二：已有 vLLM 镜像时使用 Qwen2.5（可选）

如果 AutoDL 镜像已经支持 vLLM，也可以保留远端 vLLM 服务。关键是启用自动工具选择，并为 Qwen2.5 指定 `hermes` 工具解析器。`--mode qwen` 与 `--mode api` 在当前代码中都会进入模型选工具的 Agent 循环；以下沿用 `--mode qwen` 和 `GROWTH_QWEN_*`，以便与 Ollama 路径一致。vLLM 官方说明了 [Qwen2.5 的解析器](https://docs.vllm.ai/en/latest/features/tool_calling/)和 [GPU 安装方式](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)；模型详情见 [Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)。

若镜像未安装 vLLM，先在兼容的 CUDA/Python 环境按官方安装方式准备；常见命令如下，具体轮子仍需匹配镜像：

```bash
python -m pip install -U uv
uv venv --python 3.12 --seed --managed-python /root/growth-vllm
source /root/growth-vllm/bin/activate
uv pip install vllm --torch-backend=auto
```

在远端启动服务；若 `8000` 被占用，替换为其他远端端口：

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --dtype half --max-model-len 8192 \
  --gpu-memory-utilization 0.85 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

本机另开隧道并设置模型信息；`8001` 是本机端口，可自行选择空闲端口：

```powershell
$sshHostName = "替换为AutoDL控制台显示的主机名"
$sshPortNumber = 12345
ssh -N -L 8001:127.0.0.1:8000 -p $sshPortNumber "root@$sshHostName"
```

在第二个 PowerShell 窗口运行：

```powershell
cd claim-studio
$env:GROWTH_QWEN_BASE = "http://127.0.0.1:8001/v1"
$env:GROWTH_QWEN_MODEL = "Qwen/Qwen2.5-7B-Instruct"
.\.venv\Scripts\python.exe -m growth_agent audit --id obs-virtual-camera-en --mode qwen
```

这一路径尚未在本项目的 AutoDL 实例上验证；服务能启动也不等于草稿质量达标。正式评价需记录模型版本、服务配置、硬件、真实耗时和人工标注结果。
