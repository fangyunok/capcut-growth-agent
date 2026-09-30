"""Small local editor interface for running and inspecting the agent."""

from __future__ import annotations

import html
import re
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .pipeline import DEFAULT_KNOWLEDGE, PROJECT_ROOT, run_brief
from .schemas import Brief
from .knowledge import KnowledgeBase


_STYLE = """
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:980px;margin:38px auto;padding:0 22px;color:#202937;background:#f6f8fb}
h1,h2{color:#1b477b}p{line-height:1.6}.card{background:#fff;border:1px solid #dde5ef;border-radius:12px;padding:24px;margin:20px 0;box-shadow:0 2px 10px #1b477b0c}
label{display:block;font-weight:600;margin:14px 0 5px}input,textarea,select{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #b8c6d7;border-radius:7px;font:inherit;background:#fff}textarea{min-height:82px}
button{background:#22599a;color:#fff;border:0;border-radius:8px;padding:12px 22px;font:inherit;cursor:pointer;margin-top:18px}.muted{color:#627083}.warn{background:#fff5df;padding:12px;border-radius:7px}.ok{background:#e9f8ee;padding:12px;border-radius:7px}
pre{white-space:pre-wrap;word-break:break-word;background:#f2f5f9;padding:18px;border-radius:7px;line-height:1.5;max-height:720px;overflow:auto}a{color:#1559a4}
</style>
"""


def _page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>{_STYLE}<body>{content}</body></html>"
    )


async def index(request: Request) -> HTMLResponse:
    facts = KnowledgeBase.from_file(DEFAULT_KNOWLEDGE).all()
    features = sorted({fact["feature"] for fact in facts}) + ["ai-dubbing"]
    options = "".join(
        f"<option value='{html.escape(feature)}'>{html.escape(feature)}</option>"
        for feature in features
    )
    content = f"""
    <h1>全球化增长内容 Agent</h1>
    <p class='muted'>基于 CapCut 公开资料的独立演示。生成结果只进入人工审核，不会自动发布。</p>
    <div class='card'><form method='post' action='/run'>
      <label>运行模式</label><select name='mode'><option value='qwen'>千问真实模型</option><option value='offline'>离线模板流程检查</option></select>
      <label>语言</label><select name='locale'><option value='en-US'>English (US)</option><option value='es-ES'>Español (ES)</option></select>
      <label>产品功能</label><select name='feature'>{options}</select>
      <label>受众</label><input name='audience' required value='small business owners making short tutorials'>
      <label>搜索意图／内容目标</label><textarea name='intent' required>Explain how to add and review captions in a short tutorial.</textarea>
      <label>种子关键词</label><input name='seed_keyword' required value='how to add captions to a video'>
      <label>行动引导</label><input name='cta' required value="Explore CapCut's current features">
      <button type='submit'>运行 Agent</button>
    </form></div>
    <p class='muted'>选择 ai-dubbing 可演示证据不足时停止生成。</p>
    """
    return _page("增长内容 Agent", content)


async def run(request: Request) -> HTMLResponse:
    form = await request.form()
    try:
        brief = Brief(
            id="web-brief",
            locale=str(form.get("locale", "en-US")),
            feature=str(form.get("feature", "auto-captions")),
            audience=str(form.get("audience", "")),
            intent=str(form.get("intent", "")),
            seed_keyword=str(form.get("seed_keyword", "")),
            cta=str(form.get("cta", "")),
        )
        mode = str(form.get("mode", "qwen"))
        if mode not in {"qwen", "offline"}:
            raise ValueError("Invalid mode")
        result, run_dir = await run_brief(brief, mode=mode)
        review = (run_dir / "review.md").read_text(encoding="utf-8")
    except (ValidationError, ValueError, RuntimeError) as exc:
        message = html.escape(str(exc))
        return _page(
            "运行失败",
            f"<h1>运行失败</h1><div class='card warn'>{message}</div>"
            "<p><a href='/'>返回输入页</a>。若选择千问模式，请检查模型服务、模型名称及 SSH 隧道。</p>",
        )
    status_class = "ok" if result.status == "pending_review" else "warn"
    return _page(
        "审核包",
        f"<h1>审核包</h1><p class='{status_class}'>状态：{html.escape(result.status)}。所有内容都需要人工核对。</p>"
        f"<p><a href='/'>再运行一条</a> · <a href='/runs/{html.escape(result.run_id)}'>打开审核包</a></p>"
        f"<div class='card'><pre>{html.escape(review)}</pre></div>",
    )


async def review(request: Request) -> HTMLResponse:
    run_id = request.path_params["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        return _page("无效运行编号", "<h1>无效运行编号</h1>")
    review_file = PROJECT_ROOT / "runs" / run_id / "review.md"
    if not review_file.is_file():
        return _page("未找到审核包", "<h1>未找到审核包</h1>")
    return _page(
        "审核包",
        f"<h1>审核包 {html.escape(run_id)}</h1><div class='card'><pre>{html.escape(review_file.read_text(encoding='utf-8'))}</pre></div>",
    )


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"service": "growth-agent", "status": "ok"})


app = Starlette(routes=[
    Route("/", index, methods=["GET"]),
    Route("/run", run, methods=["POST"]),
    Route("/runs/{run_id}", review, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
])
