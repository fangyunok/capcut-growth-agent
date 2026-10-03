"""Single-user local editorial UI with bounded asynchronous audit jobs."""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .approval import approve_audit, load_approved_copy
from .audit import AuditBundle, AuditRequest, run_audit
from .jobs import JobManager, JobQueueFull
from .knowledge import KnowledgeBase
from .media import copy_digest
from .pipeline import run_brief
from .resources import DEFAULT_AUDIT_KNOWLEDGE, DEFAULT_AUDIT_RULES, DEFAULT_KNOWLEDGE, DEFAULT_OUTPUT_ROOT
from .schemas import Brief


_MAX_BODY = 32768
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_STATES = {"queued": "等待处理", "running": "正在核查", "completed": "处理完成", "failed": "处理失败", "interrupted": "任务中断"}
_AUDIT_STATES = {"pending_review": "等待人工确认", "insufficient_evidence": "资料不足", "needs_revision": "仍需修订", "model_error": "模型调用失败"}
_ISSUE_LABELS = {"unsupported": "缺少依据", "overstated": "表述夸大", "needs_review": "需要核查"}
_STYLE = """
<style>
:root{color-scheme:light;--ink:#17283a;--blue:#275fe8;--muted:#60738a;--line:#e1e8f0}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,'Microsoft YaHei',sans-serif;color:var(--ink);background:#f3f6fb}
nav{background:#fff;border-bottom:1px solid var(--line);padding:18px max(24px,calc((100vw - 1100px)/2));display:flex;gap:24px;align-items:center}nav strong{margin-right:auto}nav a{font-size:14px;text-decoration:none}
main{max-width:1100px;padding:34px 24px 48px;margin:auto}h1{font-size:32px;letter-spacing:-.8px;margin:8px 0 12px}h2{font-size:20px;margin:0 0 16px}h3{font-size:16px;margin:0 0 12px}p{line-height:1.65;margin:12px 0}a{color:var(--blue)}
.eyebrow{color:var(--blue);font-size:12px;font-weight:700;letter-spacing:1px}.muted{color:var(--muted)}.layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:22px;align-items:start}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;margin:20px 0;box-shadow:0 4px 18px #182b4606}.layout .card{margin:0}.field-pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}
label{display:block;font-size:14px;font-weight:600;margin:16px 0 7px}input,textarea,select{display:block;width:100%;padding:11px 12px;border:1px solid #c8d3df;border-radius:8px;font:inherit;background:#fff;color:var(--ink)}textarea{min-height:126px;resize:vertical}input:focus,textarea:focus,select:focus{outline:3px solid #275fe824;border-color:var(--blue)}button{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:12px 20px;font:inherit;font-weight:600;cursor:pointer;margin-top:18px}.badge{display:inline-block;border-radius:20px;padding:5px 11px;font-size:13px;background:#edf2fc;color:#2459b6}.ok{background:#eaf7ef;color:#216348}.warn{background:#fff3df;color:#84520d}.error{background:#fbecea;color:#983c32}.copy{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85;background:#f6f8fc;border:1px solid #e5ebf3;border-radius:10px;padding:18px}.issue{border-left:3px solid #e8ae42;padding:8px 0 8px 15px;margin:15px 0}.source{border-top:1px solid var(--line);padding:18px 0}.source:last-child{padding-bottom:0}.source blockquote{border-left:3px solid #b8cafa;padding-left:14px;margin:12px 0;color:#42576f;line-height:1.7}.check{display:flex;gap:10px;align-items:flex-start;line-height:1.7}.check input{width:18px;min-width:18px;margin-top:4px}.history{display:block;padding:13px 0;border-bottom:1px solid var(--line);text-decoration:none}.history span{display:block;margin-top:6px;font-size:12px;color:var(--muted)}details{margin:20px 0}summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2f8;border-radius:10px;padding:18px;max-height:600px;overflow:auto;line-height:1.6}code{font-size:12px;overflow-wrap:anywhere}footer{font-size:12px;color:var(--muted);margin-top:28px}video{width:100%;max-height:75vh;background:#111;border-radius:12px}
@media(max-width:720px){.layout,.field-pair{grid-template-columns:1fr}main{padding:24px 16px}.card{padding:18px}h1{font-size:27px}nav{padding:16px;gap:12px}nav strong{font-size:14px}}
</style>
"""


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title>{_STYLE}</head><body>"
        "<nav><strong>Claim Studio · 营销审校</strong><a href='/'>文案审核</a><a href='/generate'>旧版生成示例</a></nav>"
        f"<main>{content}<footer>本地单用户演示 · 审校建议需要人工核对 · 审核者为本地署名</footer></main></body></html>",
        status_code=status_code,
    )


class _Problem(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


class SameOriginMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope['type'] == 'http' and scope['method'] == 'POST':
            request = Request(scope)
            origin = request.headers.get('origin')
            allowed = request.headers.get('sec-fetch-site', '').lower() not in {'cross-site', 'same-site'}
            if origin is not None:
                try:
                    source, target = urlsplit(origin), urlsplit(str(request.url))
                    def port(url):
                        return url.port or (443 if url.scheme == 'https' else 80)
                    allowed = allowed and source.scheme in {'http', 'https'} and not source.username and not source.password
                    allowed = allowed and not source.path and not source.query and not source.fragment
                    allowed = allowed and (source.scheme, source.hostname, port(source)) == (target.scheme, target.hostname, port(target))
                except ValueError:
                    allowed = False
            if not allowed:
                response = JSONResponse({'error': 'origin_rejected', 'message': '请从本机页面提交该请求。'}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _body(request: Request) -> bytes:
    content_length = request.headers.get('content-length')
    if content_length is not None:
        try:
            if int(content_length) < 0 or int(content_length) > _MAX_BODY:
                raise _Problem(413, '请求内容过大。')
        except ValueError:
            raise _Problem(400, '请求长度无效。')
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY:
            raise _Problem(413, '请求内容过大。')
        chunks.append(chunk)
    return b''.join(chunks)


async def _form(request: Request) -> dict[str, str]:
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
        raise _Problem(415, '该入口需要普通表单提交。')
    try:
        fields = parse_qs((await _body(request)).decode('utf-8'), keep_blank_values=True, max_num_fields=20, errors='strict')
    except (UnicodeError, ValueError):
        raise _Problem(400, '表单格式无效。')
    if any(len(values) != 1 for values in fields.values()):
        raise _Problem(400, '表单字段不能重复。')
    return {key: values[0] for key, values in fields.items()}


def _audit_paths() -> tuple[Path, Path]:
    return Path(os.getenv('GROWTH_AUDIT_KNOWLEDGE', str(DEFAULT_AUDIT_KNOWLEDGE))), Path(os.getenv('GROWTH_AUDIT_RULES', str(DEFAULT_AUDIT_RULES)))


def _run_dir(request: Request, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('运行编号无效。')
    root = request.app.state.output_root
    target = (root / run_id).resolve()
    if not target.is_relative_to(root):
        raise ValueError('运行目录无效。')
    return target


def _fixed_file(run_dir: Path, name: str) -> Path:
    target = (run_dir / name).resolve()
    if not target.is_relative_to(run_dir):
        raise ValueError('产物路径无效。')
    return target


def _json_file(path: Path) -> dict:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('产物过大，无法在网页查看。')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('产物格式无效。')
    return value


def _options(value, choices: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f'{label} 选项无效。')
    return value


async def _submit(request: Request, record: dict, mode: str, strategy: str) -> dict:
    audit_request = AuditRequest.model_validate(record, strict=True)
    json.dumps(audit_request.model_dump(), ensure_ascii=False).encode('utf-8')
    mode = _options(mode, {'qwen', 'api'}, '模型模式')
    strategy = _options(strategy, {'agent', 'rag'}, '审校策略')
    knowledge, rules = _audit_paths()
    async def operation() -> dict:
        runner = request.app.state.audit_runner or run_audit
        result, directory = await runner(
            audit_request, mode=mode, strategy=strategy, knowledge_path=knowledge,
            rules_path=rules, output_root=request.app.state.output_root, model_transport='async',
        )
        safe_dir = _run_dir(request, result.run_id)
        if Path(directory).resolve() != safe_dir or not _fixed_file(safe_dir, 'audit_bundle.json').is_file():
            raise ValueError('审核产物没有保存在本地运行目录。')
        return {'run_id': result.run_id, 'result_status': result.status}
    return await request.app.state.jobs.submit(audit_request.model_dump(), operation, mode=mode, strategy=strategy)


def _job_view(record: dict) -> dict:
    value = {**record, 'status_url': f"/api/jobs/{record['job_id']}", 'page_url': f"/jobs/{record['job_id']}"}
    if isinstance(record.get('run_id'), str) and _RUN_ID.fullmatch(record['run_id']):
        value['review_url'] = f"/audits/{record['run_id']}"
    return value


async def index(request: Request) -> HTMLResponse:
    try:
        knowledge, _ = _audit_paths()
        features = sorted({fact['feature'] for fact in KnowledgeBase.from_file(knowledge).all()})
        recent = await request.app.state.jobs.recent()
    except (OSError, ValueError):
        return _page('无法读取资料', '<h1>无法读取产品资料或任务目录</h1><p>请检查本机产品资料配置和输出目录。</p>', 503)
    options = ''.join(f"<option value='{_esc(feature)}'{(' selected' if feature == 'virtual-camera' else '')}>{_esc(feature)}</option>" for feature in features)
    history = ''.join(f"<a class='history' href='/jobs/{_esc(record['job_id'])}'>{_esc(record.get('request', {}).get('product_name', '审核任务'))}<span>{_esc(_STATES[record['status']])} · {_esc(_AUDIT_STATES.get(record.get('result_status'), 'Qwen' if record['mode'] == 'qwen' else '兼容 API'))}</span></a>" for record in recent)
    return _page('营销文案事实审校', f"""
    <div class='eyebrow'>EVIDENCE BEFORE EXPORT</div><h1>每一句营销承诺，都有据可查</h1>
    <p class='muted'>提交已有文案，获取问题定位、修订建议和来源。核对完成后，确认具体文案版本供短视频制作使用。</p>
    <div class='layout'><section class='card'><h2>创建文案审核</h2><form method='post' action='/audit'>
    <div class='field-pair'><div><label>产品 ID</label><input name='product_id' required value='obs-studio' maxlength='80'></div><div><label>产品名称</label><input name='product_name' required value='OBS Studio' maxlength='100'></div></div>
    <div class='field-pair'><div><label>语言</label><select name='locale'><option value='zh-CN'>中文</option><option value='en-US'>English (US)</option></select></div><div><label>功能主题</label><select name='feature'>{options}</select></div></div>
    <div class='field-pair'><div><label>投放渠道</label><select name='channel'><option value='social_post'>社媒文案</option><option value='landing_page'>落地页文案</option></select></div><div><label>模型服务</label><select name='mode'><option value='qwen'>本地／远端 Qwen</option><option value='api'>已配置的兼容 API</option></select></div></div>
    <label>目标读者</label><input name='audience' required maxlength='200' value='第一次使用产品的内容创作者'>
    <label>待审核文案</label><textarea name='original_copy' required maxlength='4000'>OBS 的虚拟摄像头适用于所有会议软件，保证视频通话永远不卡顿。</textarea>
    <button type='submit'>开始核查</button></form></section>
    <aside><section class='card'><h2>你会拿到什么</h2><p>① 原句中需要核查的主张</p><p>② 中英文修订建议</p><p>③ 原始资料与引用对应关系</p><p>④ 人工确认后的文案版本</p><p class='muted'>没有对应产品资料时会停止修订。引用和规则检查通过，仍需要人工判断来源是否支持文案。</p></section><section class='card' style='margin-top:20px'><h2>最近的本地任务</h2>{history or '<p class="muted">还没有任务，提交一条文案开始。</p>'}</section></aside></div>
    """)


async def audit_run(request: Request):
    try:
        form = await _form(request)
        mode, strategy = form.pop('mode', 'qwen'), form.pop('strategy', 'agent')
        form.setdefault('id', 'web-audit')
        record = await _submit(request, form, mode, strategy)
        return RedirectResponse(f"/jobs/{record['job_id']}", status_code=303)
    except _Problem as exc:
        return _page('提交失败', f"<h1>提交失败</h1><p>{_esc(exc.message)}</p><a href='/'>返回编辑</a>", exc.status)
    except (ValidationError, ValueError) as exc:
        return _page('输入需要修改', f"<h1>输入需要修改</h1><p>{_esc(str(exc))}</p><a href='/'>返回编辑</a>", 400)
    except JobQueueFull as exc:
        return _page('队列已满', f"<h1>当前任务较多</h1><p>{_esc(exc)}</p><a href='/'>查看已有任务</a>", 429)
    except OSError:
        return _page('无法保存任务', '<h1>无法保存任务</h1><p>请检查本地输出目录的写入权限。</p>', 503)


async def api_audit(request: Request) -> JSONResponse:
    try:
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise _Problem(415, '使用 application/json 提交审核请求。')
        payload = json.loads(await _body(request))
        if not isinstance(payload, dict) or set(payload) - {'request', 'mode', 'strategy'} or not isinstance(payload.get('request'), dict):
            raise _Problem(400, 'JSON 需要 request 对象以及可选 mode、strategy。')
        record = await _submit(request, payload['request'], payload.get('mode', 'qwen'), payload.get('strategy', 'agent'))
        return JSONResponse(_job_view(record), status_code=202)
    except _Problem as exc:
        return JSONResponse({'error': 'invalid_request', 'message': exc.message}, status_code=exc.status)
    except (UnicodeError, json.JSONDecodeError):
        return JSONResponse({'error': 'invalid_json', 'message': '请求不是有效 JSON。'}, status_code=400)
    except (ValidationError, ValueError):
        return JSONResponse({'error': 'invalid_request', 'message': '请检查审核字段、语言、渠道、模型模式和策略。'}, status_code=400)
    except JobQueueFull as exc:
        return JSONResponse({'error': 'queue_full', 'message': str(exc)}, status_code=429)
    except OSError:
        return JSONResponse({'error': 'output_unavailable', 'message': '本地输出目录不可写。'}, status_code=503)


async def api_job(request: Request) -> JSONResponse:
    try:
        record = await request.app.state.jobs.get(request.path_params['job_id'])
        return JSONResponse(_job_view(record))
    except (FileNotFoundError, ValueError, TypeError):
        return JSONResponse({'error': 'job_not_found'}, status_code=404)
    except OSError:
        return JSONResponse({'error': 'job_metadata_unavailable'}, status_code=503)


async def job_page(request: Request) -> HTMLResponse:
    try:
        record = await request.app.state.jobs.get(request.path_params['job_id'])
    except (OSError, ValueError, TypeError):
        return _page('找不到任务', '<h1>找不到这条本地任务</h1><a href="/">返回审核页面</a>', 404)
    view = _job_view(record)
    error = f"<p class='warn'>{_esc(record['error']['message'])}</p>" if record.get('error') else ''
    result = f"<p><a href='{_esc(view['review_url'])}'>查看文案与来源</a></p>" if 'review_url' in view else ''
    script = ''
    if record['status'] in {'queued', 'running'}:
        script = """<script>
        async function refreshJob(){try{const response=await fetch(STATUS_URL,{cache:'no-store'});if(!response.ok)throw new Error();const job=await response.json();document.getElementById('job-status').textContent=job.status==='queued'?'等待处理':job.status==='running'?'正在核查':'处理结束';if(['completed','failed','interrupted'].includes(job.status)){if(job.review_url){location.assign(job.review_url);return;}document.getElementById('job-note').textContent=job.error?job.error.message:'任务已结束。';return;}}catch(error){document.getElementById('job-note').textContent='暂时无法读取任务状态，将继续重试。';}setTimeout(refreshJob,1500);}setTimeout(refreshJob,800);
        </script>""".replace('STATUS_URL', json.dumps(view['status_url']))
    return _page('审核任务', f"<div class='eyebrow'>LOCAL AUDIT JOB</div><h1>正在准备你的审核包</h1><div class='card'><p id='job-status' class='badge'>{_esc(_STATES[record['status']])}</p><p>{_esc(record['request'].get('product_name', '审核任务'))} · {_esc(record['request'].get('locale', ''))}</p><p id='job-note' class='muted'>任务记录已保存在本机，关闭页面后可按编号查看。</p>{error}{result}<p><code>{_esc(record['job_id'])}</code></p><a href='/'>返回文案编辑</a></div>{script}")


def _source_link(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {'http', 'https'} and parsed.netloc and not parsed.username:
            return f"<a href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>查看原始资料 ↗</a>"
    except (ValueError, TypeError):
        pass
    return f"<span class='muted'>{_esc(url)}</span>"


async def audit_review(request: Request) -> HTMLResponse:
    try:
        directory = _run_dir(request, request.path_params['run_id'])
        bundle_file = _fixed_file(directory, 'audit_bundle.json')
        if bundle_file.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('审核产物过大。')
        snapshot = bundle_file.read_bytes()
        bundle = AuditBundle.model_validate(json.loads(snapshot))
        audit_snapshot_digest = hashlib.sha256(snapshot).hexdigest()
        if bundle.run_id != request.path_params['run_id']:
            raise ValueError('审核编号不匹配。')
    except (OSError, ValueError, TypeError):
        return _page('找不到审核包', '<h1>找不到有效的审核包</h1><a href="/">返回审核页面</a>', 404)
    proposal = bundle.proposal
    messages = {'pending_review': '资料与字面检查已完成，请逐条核对来源后确认。', 'insufficient_evidence': '没有检索到该产品及功能的足够资料，未生成可确认文案。', 'needs_revision': '修订尚未通过检查，当前内容不能确认。', 'model_error': '模型服务调用失败。运行记录已保存，未冒充生成成功。'}
    issues = ''.join(f"<article class='issue'><span class='badge warn'>{_esc(_ISSUE_LABELS[issue.kind])}</span><p><strong>“{_esc(issue.quote)}”</strong></p><p>{_esc(issue.reason)}</p></article>" for issue in proposal.issues) if proposal else ''
    facts = {fact['id']: fact for fact in bundle.facts if isinstance(fact.get('id'), str)}
    citations = ''.join(f"<div class='source'><p>“{_esc(citation.quote)}”</p><p class='muted'>对应资料：<code>{_esc(citation.fact_id)}</code></p>{_source_link(str(facts.get(citation.fact_id, {}).get('source_url', '未找到来源')))}</div>" for citation in proposal.citations) if proposal else ''
    sources = ''.join(f"<article class='source'><h3>{_esc(fact.get('source_title', fact['id']))}</h3><p class='muted'><code>{_esc(fact['id'])}</code> · 核对日期 {_esc(fact.get('checked_at', '未记录'))}</p><blockquote>{_esc(fact.get('source_quote', ''))}</blockquote><p>{_esc(fact.get('availability_note', ''))}</p>{_source_link(str(fact.get('source_url', '')))}</article>" for fact in facts.values())
    approval = ''
    if proposal and bundle.status == 'pending_review' and bundle.revised_checks.get('passed') is True:
        version = copy_digest(proposal.revised_copy)
        try:
            approval_record = _json_file(_fixed_file(directory, 'audit_approval.json'))
            if approval_record.get('audit_digest') != audit_snapshot_digest:
                raise ValueError('该确认不对应页面显示的资料快照。')
            approved = load_approved_copy(directory)
            if approved.copy_version != version:
                raise ValueError('该确认不对应页面显示的文案版本。')
            approval = f"<section class='card'><h2>当前版本已确认</h2><p class='badge ok'>已确认 · {_esc(approved.reviewer)}</p><p>该文案可供本地分镜与视频制作使用。</p><code>{_esc(version[:16])}</code></section>"
        except FileNotFoundError:
            approval = f"""<section class='card'><h2>确认当前修订稿</h2><p class='muted'>检查每条产品主张与来源，以及翻译和适用范围；确认仅对当前文字和资料有效。</p><form method='post' action='/audits/{_esc(bundle.run_id)}/approve'><input type='hidden' name='expected_version' value='{_esc(version)}'><input type='hidden' name='audit_digest' value='{_esc(audit_snapshot_digest)}'><label>审核者署名</label><input name='reviewer' required maxlength='100' placeholder='填写你的名字'><label>审核备注（可选）</label><textarea name='notes' maxlength='2000' style='min-height:70px'></textarea><label class='check'><input type='checkbox' name='source_checked' value='yes' required><span>我已核对修订稿中的主张及来源，并确认这一版本。</span></label><button type='submit'>确认文案版本</button></form></section>"""
        except (OSError, ValueError, TypeError):
            approval = "<section class='card warn'><h2>旧确认已失效</h2><p>文案或来源与确认记录不匹配，请重新创建审核任务。</p></section>"
    trace = _esc(json.dumps({'strategy': bundle.strategy, 'checks': bundle.revised_checks, 'usage': bundle.usage, 'trace': bundle.trace}, ensure_ascii=False, indent=2))
    return _page('文案与来源审核', f"<div class='eyebrow'>REVIEW BEFORE CONFIRMATION</div><h1>{_esc(bundle.request.product_name)} · 文案审核</h1><p class='badge {'ok' if bundle.status == 'pending_review' else 'warn'}'>{_esc(_AUDIT_STATES[bundle.status])}</p><p class='muted'>{_esc(messages[bundle.status])}</p><div class='layout'><section class='card'><h2>原始文案</h2><div class='copy'>{_esc(bundle.request.original_copy)}</div><h2 style='margin-top:24px'>需要核查的主张</h2>{issues or '<p class="muted">没有可展示的问题列表；仍需人工检查原文。</p>'}</section><section class='card'><h2>建议修订稿</h2><div class='copy'>{_esc(proposal.revised_copy if proposal else '没有产生可审阅的修订稿。')}</div><h2 style='margin-top:24px'>主张与来源对应</h2>{citations or '<p class="muted">没有可展示的引用。</p>'}</section></div><section class='card'><h2>本次检索到的资料</h2>{sources or '<p class="muted">没有对应产品资料。</p>'}</section>{approval}<details><summary>查看开发记录与字面检查</summary><pre>{trace}</pre></details><p><a href='/'>再审核一条文案</a></p>")


async def audit_approve(request: Request):
    try:
        form = await _form(request)
        if set(form) - {'expected_version', 'audit_digest', 'reviewer', 'notes', 'source_checked'} or form.get('source_checked') != 'yes':
            raise ValueError('请先明确确认你已经逐条核对来源。')
        version = form.get('expected_version', '')
        if not re.fullmatch(r'[a-f0-9]{64}', version):
            raise ValueError('确认版本无效，请重新打开审核页面。')
        expected_audit_digest = form.get('audit_digest', '')
        if not re.fullmatch(r'[a-f0-9]{64}', expected_audit_digest):
            raise ValueError('资料快照版本无效，请重新打开审核页面。')
        directory = _run_dir(request, request.path_params['run_id'])
        _fixed_file(directory, 'audit_bundle.json')
        _fixed_file(directory, 'audit_approval.json')
        approve_audit(directory, reviewer=form.get('reviewer', ''), expected_version=version, expected_audit_digest=expected_audit_digest, notes=form.get('notes', ''))
        return RedirectResponse(f"/audits/{request.path_params['run_id']}", status_code=303)
    except _Problem as exc:
        return _page('无法确认', f"<h1>无法确认</h1><p>{_esc(exc.message)}</p>", exc.status)
    except (OSError, ValueError, TypeError) as exc:
        return _page('无法确认', f"<h1>无法确认当前版本</h1><p>{_esc(exc)}</p><a href='/'>返回审核页面</a>", 409)


async def media_video(request: Request):
    try:
        directory = _run_dir(request, request.path_params['run_id'])
        metadata = _json_file(_fixed_file(directory, 'media_bundle.json'))
        if metadata.get('run_id') != request.path_params['run_id'] or metadata.get('status') != 'completed' or not isinstance(metadata.get('artifacts'), dict) or metadata['artifacts'].get('video') != 'video.mp4':
            raise ValueError('没有完成的视频。')
        video = _fixed_file(directory, 'video.mp4')
        if not video.is_file():
            raise FileNotFoundError()
        return FileResponse(video, media_type='video/mp4', headers={'X-Content-Type-Options': 'nosniff'})
    except (OSError, ValueError, TypeError):
        return JSONResponse({'error': 'video_not_found'}, status_code=404)


async def generate_form(request: Request) -> HTMLResponse:
    features = sorted({fact['feature'] for fact in KnowledgeBase.from_file(DEFAULT_KNOWLEDGE).all()}) + ['ai-dubbing']
    options = ''.join(f"<option value='{_esc(feature)}'>{_esc(feature)}</option>" for feature in features)
    return _page('旧版生成示例', f"""<h1>旧版增长内容生成示例</h1><p class='muted'>使用 CapCut 公开资料，生成结果进入人工审核。</p><section class='card'><form method='post' action='/run'><label>模式</label><select name='mode'><option value='qwen'>真实 Qwen 模型</option><option value='offline'>离线模板流程检查</option></select><label>语言</label><select name='locale'><option value='en-US'>English (US)</option><option value='es-ES'>Español (ES)</option></select><label>功能主题</label><select name='feature'>{options}</select><label>受众</label><input name='audience' required value='small business owners making short tutorials'><label>内容目标</label><textarea name='intent' required>Explain how to add and review captions in a short tutorial.</textarea><label>关键词</label><input name='seed_keyword' required value='how to add captions to a video'><label>行动引导</label><input name='cta' required value="Explore CapCut's current features"><button type='submit'>运行生成流程</button></form></section>""")


async def run(request: Request) -> HTMLResponse:
    try:
        form = await _form(request)
        mode = _options(form.pop('mode', 'qwen'), {'qwen', 'offline', 'api'}, '模式')
        form.setdefault('id', 'web-brief')
        brief = Brief.model_validate(form, strict=True)
        result, directory = await run_brief(brief, mode=mode, output_root=request.app.state.output_root)
        review_path = _fixed_file(_run_dir(request, result.run_id), 'review.md')
        review_text = review_path.read_text(encoding='utf-8')
        return _page('旧版审核包', f"<h1>旧版审核包</h1><p class='badge'>{_esc(result.status)}</p><div class='card'><pre>{_esc(review_text)}</pre></div><a href='/generate'>再次生成</a>")
    except _Problem as exc:
        return _page('运行失败', f"<h1>运行失败</h1><p>{_esc(exc.message)}</p>", exc.status)
    except (ValidationError, ValueError, RuntimeError, OSError) as exc:
        return _page('运行失败', f"<h1>运行失败</h1><p>{_esc(exc)}</p><a href='/generate'>返回旧版表单</a>", 400)


async def review(request: Request) -> HTMLResponse:
    try:
        path = _fixed_file(_run_dir(request, request.path_params['run_id']), 'review.md')
        text = path.read_text(encoding='utf-8')
        return _page('旧版审核包', f"<h1>旧版审核包</h1><div class='card'><pre>{_esc(text)}</pre></div>")
    except (OSError, ValueError):
        return _page('找不到审核包', '<h1>找不到审核包</h1>', 404)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({'service': 'growth-agent', 'status': 'ok', 'scope': 'local_single_user'})


def create_app(*, output_root: Path = DEFAULT_OUTPUT_ROOT, audit_runner=None, max_pending: int = 8, concurrency: int = 2, job_timeout_seconds: float = 240) -> Starlette:
    manager = JobManager(output_root, max_pending=max_pending, concurrency=concurrency, timeout_seconds=job_timeout_seconds)
    @asynccontextmanager
    async def lifespan(application):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()
    application = Starlette(lifespan=lifespan, routes=[
        Route('/', index, methods=['GET']), Route('/audit', audit_run, methods=['POST']),
        Route('/api/audits', api_audit, methods=['POST']), Route('/api/jobs/{job_id}', api_job, methods=['GET']),
        Route('/jobs/{job_id}', job_page, methods=['GET']), Route('/audits/{run_id}', audit_review, methods=['GET']),
        Route('/audits/{run_id}/approve', audit_approve, methods=['POST']), Route('/media/{run_id}/video', media_video, methods=['GET']),
        Route('/generate', generate_form, methods=['GET']), Route('/run', run, methods=['POST']),
        Route('/runs/{run_id}', review, methods=['GET']), Route('/health', health, methods=['GET']),
    ])
    application.state.output_root = manager.output_root
    application.state.jobs = manager
    application.state.audit_runner = audit_runner
    application.add_middleware(SameOriginMiddleware)
    return application


app = create_app()
