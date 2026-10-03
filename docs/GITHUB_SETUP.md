# GitHub 交付与复现

仓库用于展示可以复现的工程实现和演示数据。网页服务运行在本机或自行配置的服务器；GitHub Pages 只能托管静态文件，不能直接运行这个 Python 后端或模型。

## 安装与验证

项目支持 Python 3.11+，CI 覆盖 Python 3.11、3.12 的 Ubuntu 和 Windows。首次安装需要访问 Python 包源。

```powershell
git clone https://github.com/fangyunok/capcut-growth-agent.git
cd capcut-growth-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m growth_agent demo --mode offline
```

Linux/macOS 将 Python 路径换为 `.venv/bin/python`。离线 `demo` 是固定模板流程，用于验证检索、规则、保存和人工审核流程；它不调用大模型，不能代表真实模型的生成质量。

视频导出还需要 FFmpeg。没有系统 FFmpeg 时，可安装可选依赖 `.\.venv\Scripts\python.exe -m pip install -e ".[media]"`，使用包内提供的平台二进制。`python -m growth_agent media-demo` 使用程序创建的占位图片和模板文案检查视频合成；这些素材不是模型生成或产品功能证明。

真实审校需要自行运行支持工具调用的模型服务，再设置模型地址。已有本机 Ollama 或 AutoDL SSH 隧道可直接复用，具体配置见 [AutoDL 配置](AUTODL_SETUP.md)。启动本机页面：

```powershell
$env:GROWTH_QWEN_BASE = 'http://127.0.0.1:11434/v1'
$env:GROWTH_QWEN_MODEL = 'qwen3:4b-instruct'
.\.venv\Scripts\python.exe -m growth_agent serve --host 127.0.0.1 --port 7860
```

打开 `http://127.0.0.1:7860`。`/health` 检查网页进程是否存活，不证明模型服务可以使用。运行审核仍需要可访问的模型。代码默认读取进程环境变量，**不会自动加载 `.env`**；`.env.example` 是配置字段参考。

## Wheel 安装

```powershell
.\.venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir dist .
python -m venv .venv-wheel
.\.venv-wheel\Scripts\python.exe -m pip install .\dist\product_claim_review_agent-0.3.0-py3-none-any.whl
```

Wheel 内包含演示资料和评测输入文件。安装后从其他目录运行也能找到演示数据，运行结果默认写到当前工作目录的 `runs/`；可通过 `GROWTH_RUNS_DIR` 指定其他目录。安装不会向 `site-packages` 写运行结果。

开发时顶层 `data/` 是可编辑样例；发布时 `src/growth_agent/data/` 是 wheel 打包样例。编辑公开样例后应同步这两处文件；CI 会逐个比较文件内容，避免发布过时资料。企业内部资料可以用 CLI `--knowledge/--rules/--cases` 参数或网页的 `GROWTH_AUDIT_KNOWLEDGE/GROWTH_AUDIT_RULES` 指定，放在被忽略的 `local-data/` 或仓库外即可。

## Docker 本机运行

Dockerfile 构建 Python 网页服务，安装 FFmpeg 和中文字体，并以普通用户启动。镜像不含模型权重、Ollama 服务、API 密钥或 SSH 配置。本机没有 Docker 时，可以继续使用上面的 Python 安装方式。

```powershell
docker build -t product-claim-review-agent .
docker volume create growth-agent-runs
docker run --rm --name growth-agent -p 127.0.0.1:7860:7860 --add-host=host.docker.internal:host-gateway -v growth-agent-runs:/app/runs -e GROWTH_QWEN_BASE=http://host.docker.internal:11434/v1 -e GROWTH_QWEN_MODEL=qwen3:4b-instruct product-claim-review-agent
```

若模型经本机端口 `11435` 的 SSH 隧道提供，将上面的地址改为 `http://host.docker.internal:11435/v1`。容器中的 `127.0.0.1` 指向容器自身。宿主机模型服务或隧道需要能接受 Docker 网关的连接；Windows/macOS Docker Desktop 通常可通过 `host.docker.internal` 访问宿主机服务。Linux 仅绑定宿主机 `127.0.0.1` 的服务可能无法经网关访问，可在同机 Linux 上使用 `--network host`，去掉 `-p` 和 `--add-host`，将模型地址设回 `http://127.0.0.1:11434/v1`，并显式使用 `growth-agent serve --host 127.0.0.1 --port 7860` 作为容器命令。

如使用自有文件，另加只读挂载和对应环境变量，例如 `-v /absolute/path/to/local-data:/data:ro -e GROWTH_AUDIT_KNOWLEDGE=/data/my_knowledge.json`。命名卷持久化结果；绑定自己的结果目录时，该目录需要允许容器 UID `10001` 写入。

## CI 检查范围

每次 push、pull request 或手动运行 CI，工作流会执行测试、核对样例副本、构建 wheel，再创建全新虚拟环境并切换到仓库外验证 wheel。它检查资料读取、默认 MCP 实例、离线 CLI、默认结果写入、网页 `/health` 和表单；Ubuntu runner 额外安装 FFmpeg，使媒体集成测试可以真正执行。

CI 安装可选 `media` 依赖，并要求真实 FFmpeg 测试实际执行且不跳过；Ubuntu 使用系统 FFmpeg，Windows 可以使用 Python 包中的二进制。仓库外的 wheel 验证还实际运行 `media-demo`，确认视频文件产出。

CI 不需要模型 API 密钥，不下载模型权重，也不验证真实模型审校效果。真实模型回归和失败案例应另行记录在项目状态及评测文档中。CI 已写入仓库；远端是否通过，以 GitHub Actions 的实际执行结果为准。

本机已在 Windows、Python 3.12.14 上构建并安装 0.3.0 wheel，从仓库外完成复现检查：11 个打包样例、20 条审校回归输入和对应标签可读取；CLI 生成六份离线流程包；可选 FFmpeg 实际输出并完整解码模板 MP4；网页健康页、主表单、旧版表单和视频预览返回成功。检查不依赖模型密钥，模板视频明确标注没有使用模型和 TTS。Docker 本机未构建验证。

## 发布到 GitHub

已配置的仓库地址：`https://github.com/fangyunok/capcut-growth-agent.git`。修改代码后先检查文件列表和差异：

```powershell
git status --short
git diff --check
git diff --stat
```

确认公开文件仅含代码、公开样例和可分享的文档，再提交并推送：

```powershell
git add .
git commit -m "Build reproducible bilingual marketing review agent"
git push -u origin main
```

认证失败时，可以通过 GitHub CLI 的 `gh auth login` 或 Git Credential Manager 登录自己的 GitHub 账户。不要把 token 写进 remote URL、脚本或文档。不要提交 `.env`、SSH 密钥、`.ssh_known_hosts`、运行包、企业资料或模型权重；当前忽略规则已经覆盖这些常见本地文件。

本项目代码采用 MIT License。引用的 OBS/CapCut 资料、Qwen 模型和第三方依赖保留各自权利与许可；代码许可不意味着这些外部素材也采用 MIT。

参考：[GitHub 官方 Python CI 指南](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)、[setuptools 包数据指南](https://setuptools.pypa.io/en/stable/userguide/datafiles.html)。
