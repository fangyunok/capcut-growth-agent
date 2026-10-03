"""Evidence -> draft -> deterministic checks -> human review bundle."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import Client

from .agent_loop import run_llm_agent
from .generation import CompatibleApiGenerator, OfflineTemplateGenerator, QwenOllamaGenerator
from .mcp_server import create_server
from .schemas import Brief, Draft, RunResult
from .resources import PROJECT_ROOT, DEFAULT_KNOWLEDGE, DEFAULT_RULES, DEFAULT_OUTPUT_ROOT


def _trace(step: str, started: float, **details: Any) -> dict:
    return {
        "step": step,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "at_utc": datetime.now(timezone.utc).isoformat(),
        **details,
    }


async def _tool(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool {name} failed: {result.content}")
    payload = getattr(result, "structured_content", None)
    if payload is None:
        blocks = getattr(result, "content", [])
        text_blocks = [block.text for block in blocks if getattr(block, "type", "") == "text"]
        if not text_blocks:
            raise RuntimeError(f"MCP tool {name} returned no structured result")
        payload = json.loads(text_blocks[0])
    if not isinstance(payload, dict):
        raise TypeError(f"MCP tool {name} returned {type(payload).__name__}, expected object")
    return payload


def _run_id(brief: Brief) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", brief.id)[:70]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{safe_id}"


def _broken_claim_links(draft: Draft) -> list[str]:
    locations: dict[str, str] = {
        "title": draft.title,
        "meta_description": draft.meta_description,
        "h1": draft.h1,
        "intro": draft.intro,
    }
    locations.update({f"body[{i}]": text for i, text in enumerate(draft.body)})
    locations.update({f"social_posts[{i}]": text for i, text in enumerate(draft.social_posts)})
    broken = []
    for claim in draft.claim_uses:
        passage = locations.get(claim.location, "")
        if claim.sentence.casefold() not in passage.casefold():
            broken.append(f"{claim.fact_id}@{claim.location}")
    return broken


def _persist_result(result: RunResult, output_root: Path) -> Path:
    run_dir = output_root / result.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "bundle.json").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "review.md").write_text(_render_review(result), encoding="utf-8")
    return run_dir


async def run_brief(
    brief: Brief,
    *,
    mode: str = "offline",
    knowledge_path: Path = DEFAULT_KNOWLEDGE,
    rules_path: Path = DEFAULT_RULES,
    output_root: Path | None = None,
) -> tuple[RunResult, Path]:
    """Run one brief and persist a review package; no publishing occurs."""

    if mode not in {"offline", "api", "qwen"}:
        raise ValueError("mode must be offline, api, or qwen")
    output_root = output_root or DEFAULT_OUTPUT_ROOT
    run_id = _run_id(brief)

    if mode in {"qwen", "api"}:
        # The model chooses its own evidence/rules tool calls within agent_loop.
        # Persist failed calls too, so the CLI and web UI can show a useful status.
        generator = (
            QwenOllamaGenerator.from_environment()
            if mode == "qwen"
            else CompatibleApiGenerator.from_environment()
        )
        agent = await run_llm_agent(
            brief,
            generator,
            knowledge_path,
            rules_path,
        )
        result = RunResult(run_id=run_id, brief=brief, **agent)
        return result, _persist_result(result, output_root)

    trace: list[dict] = []
    facts: list[dict] = []
    checks: dict = {}
    generation = None
    draft = None
    status = "insufficient_evidence"
    server = create_server(knowledge_path, rules_path)

    async with Client(server) as client:
        started = time.perf_counter()
        rules = await _tool(client, "get_editorial_rules", {"locale": brief.locale})
        trace.append(_trace("get_editorial_rules", started, locale=brief.locale))

        started = time.perf_counter()
        search = await _tool(
            client,
            "search_knowledge",
            {"query": f"{brief.feature} {brief.seed_keyword}", "top_k": 5},
        )
        # Exact feature match prevents a lexical near miss from becoming evidence.
        facts = [fact for fact in search.get("results", []) if fact.get("feature") == brief.feature]
        trace.append(
            _trace(
                "search_knowledge",
                started,
                returned=len(search.get("results", [])),
                selected_fact_ids=[fact["id"] for fact in facts],
            )
        )

        if facts:
            started = time.perf_counter()
            generator = OfflineTemplateGenerator()
            generation = await asyncio.to_thread(generator.generate, brief, facts, rules)
            draft = generation.draft
            trace.append(
                _trace(
                    "generate_draft",
                    started,
                    mode=generation.mode,
                    model=generation.model,
                    prompt_version=generation.prompt_version,
                    input_tokens=generation.input_tokens,
                    output_tokens=generation.output_tokens,
                )
            )

            started = time.perf_counter()
            draft_text = "\n".join(
                [draft.title, draft.meta_description, draft.h1, draft.intro]
                + draft.body
                + draft.social_posts
            )
            fact_ids = sorted({claim.fact_id for claim in draft.claim_uses})
            checks = await _tool(
                client,
                "check_draft",
                {"text": draft_text, "fact_ids": fact_ids, "locale": brief.locale},
            )
            retrieved_ids = {fact["id"] for fact in facts}
            checks["unretrieved_fact_ids"] = sorted(set(fact_ids) - retrieved_ids)
            checks["missing_claim_links"] = not bool(draft.claim_uses)
            checks["broken_claim_links"] = _broken_claim_links(draft)
            checks["retrieved_evidence_only"] = not checks["unretrieved_fact_ids"]
            status = (
                "pending_review"
                if checks.get("passed")
                and not checks["unretrieved_fact_ids"]
                and not checks["missing_claim_links"]
                and not checks["broken_claim_links"]
                else "needs_revision"
            )
            trace.append(_trace("check_draft", started, status=status, checks=checks))
        else:
            checks = {
                "passed": False,
                "reason": "No verified fact card matches the requested feature.",
                "manual_review_required": True,
            }
            trace.append(
                {
                    "step": "stop_on_missing_evidence",
                    "duration_ms": 0,
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                    "feature": brief.feature,
                }
            )

    result = RunResult(
        run_id=run_id,
        brief=brief,
        status=status,
        facts=facts,
        draft=draft,
        checks=checks,
        generation=generation,
        trace=trace,
    )
    return result, _persist_result(result, output_root)


def _render_review(result: RunResult) -> str:
    brief = result.brief
    lines = [
        f"# Human review: {brief.id}",
        "",
        f"- Status: **{result.status}**",
        f"- Locale: {brief.locale}",
        f"- Feature: {brief.feature}",
        f"- Run ID: `{result.run_id}`",
        f"- Prototype notice: {result.disclaimer}",
        *(
            [
                f"- Generation: `{result.generation.mode}`"
                + (f" using `{result.generation.model}`" if result.generation.model else "")
            ]
            if result.generation else []
        ),
        "",
        "## Draft",
        "",
    ]
    if result.draft:
        draft = result.draft
        lines += [
            f"**Title:** {draft.title}",
            "",
            f"**Meta description:** {draft.meta_description}",
            "",
            f"# {draft.h1}",
            "",
            draft.intro,
            "",
        ]
        for paragraph in draft.body:
            lines.extend([paragraph, ""])
        lines.extend(["## Social posts", ""])
        for index, post in enumerate(draft.social_posts, start=1):
            lines.extend([f"{index}. {post}", ""])
        sources = {fact["id"]: fact for fact in result.facts}
        lines.extend(["## Claim-to-source map", ""])
        for claim in draft.claim_uses:
            source = sources.get(claim.fact_id)
            url = source["source_url"] if source else "UNRETRIEVED SOURCE"
            lines.extend(
                [
                    f"- `{claim.location}` — {claim.sentence}",
                    f"  - Evidence `{claim.fact_id}`: {url}",
                ]
            )
        lines.append("")
    else:
        explanation = {
            "insufficient_evidence": "No draft produced: verified feature evidence was insufficient.",
            "model_error": "No draft produced: the model service or response protocol failed.",
            "needs_revision": "No valid draft was produced within the tool and revision limits.",
        }.get(result.status, "No draft was produced.")
        lines.extend([explanation, ""])

    lines.extend(["## Evidence cards", ""])
    for fact in result.facts:
        lines.extend(
            [
                f"### {fact['id']}",
                "",
                f"- Source: {fact['source_title']} — {fact['source_url']}",
                f"- Checked: {fact['checked_at']}",
                f"- Source excerpt: “{fact['source_quote']}”",
                f"- Availability note: {fact.get('availability_note', '')}",
                "",
            ]
        )
    lines += [
        "## Deterministic checks",
        "",
        "```json",
        json.dumps(result.checks, ensure_ascii=False, indent=2),
        "```",
        "",
        "These checks do not prove semantic factual correctness. A person must compare each claim to its linked source before any use.",
        "",
        "## Agent execution trace",
        "",
        "```json",
        json.dumps(result.trace, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Editor decision",
        "",
        "- [ ] Every product claim is supported by the linked source passage.",
        "- [ ] Language and local availability are appropriate for the target audience.",
        "- [ ] The page adds useful guidance beyond restating product features.",
        "- [ ] Title, metadata, and social copy are accurate and readable.",
        "- [ ] Approved for publication by an authorized reviewer.",
        "",
    ]
    return "\n".join(lines)
