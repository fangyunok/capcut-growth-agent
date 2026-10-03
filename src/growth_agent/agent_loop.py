"""Bounded Qwen tool-use loop for source-backed growth drafts.

The model chooses when to call the two read-only MCP tools. The application
validates every tool request, performs the calls, and checks the final draft.
This module never publishes content or treats rule checks as factual review.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp import Client
from pydantic import ValidationError

from .generation import CompatibleApiGenerator
from .mcp_server import create_server
from .schemas import Brief, Draft, GenerationResult


MAX_MODEL_TURNS = 6
MAX_TOOL_CALLS = 6
MAX_REVISIONS = 2
PROMPT_VERSION = "qwen-tool-agent-v4"

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Find source-backed product fact cards for the requested feature.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short feature and search-intent query"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 5},
                    "product_id": {"type": "string", "description": "Exact product ID when auditing a specific product"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_editorial_rules",
            "description": "Read editorial constraints for the brief's exact locale.",
            "parameters": {
                "type": "object",
                "properties": {"locale": {"type": "string", "enum": ["en-US", "zh-CN", "es-ES"]}},
                "required": ["locale"],
                "additionalProperties": False,
            },
        },
    },
]


def _chat_complete(
    generator: CompatibleApiGenerator,
    messages: list[dict[str, Any]],
    final_json: bool = False,
) -> dict[str, Any]:
    """Allow tool selection first, then require a JSON completion for the draft."""
    payload = {
        "model": generator.model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 1800 if final_json else 450,
        "stream": False,
    }
    if final_json:
        payload["response_format"] = {"type": "json_object"}
    else:
        payload["tools"] = _TOOLS
        payload["tool_choice"] = "auto"
    request = urllib.request.Request(
        f"{generator.base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {generator.api_key}"} if generator.api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=generator.timeout) as reply:
            response = json.load(reply)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Model service returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Cannot reach model service") from exc
    if not isinstance(response, dict):
        raise ValueError("Model service returned a non-object response")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Model choices must contain an object")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("Model message must be an object")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise ValueError("Model content must be a string or null")
    if message.get("tool_calls") is not None and not isinstance(message["tool_calls"], list):
        raise ValueError("Model tool_calls must be a list or null")
    if response.get("usage") is not None and not isinstance(response["usage"], dict):
        raise ValueError("Model usage must be an object or null")
    return response


async def _tool(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool {name} failed")
    payload = getattr(result, "structured_content", None)
    if payload is None:
        blocks = [
            block.text for block in getattr(result, "content", [])
            if getattr(block, "type", "") == "text"
        ]
        if not blocks:
            raise ValueError(f"MCP tool {name} returned no result")
        payload = json.loads(blocks[0])
    if not isinstance(payload, dict):
        raise ValueError(f"MCP tool {name} returned a non-object result")
    return payload


def _validated_arguments(name: str, raw: Any, locale: str) -> dict[str, Any]:
    if name not in {"search_knowledge", "get_editorial_rules"}:
        raise ValueError("tool is not on the allowlist")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError("tool arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if name == "search_knowledge":
        if set(arguments) - {"query", "top_k", "product_id"}:
            raise ValueError("search_knowledge received unknown arguments")
        query = arguments.get("query")
        top_k = arguments.get("top_k", 5)
        product_id = arguments.get("product_id", "")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("query must contain 1 to 200 characters")
        if type(top_k) is not int or not 1 <= top_k <= 5:
            raise ValueError("top_k must be an integer from 1 to 5")
        if not isinstance(product_id, str) or len(product_id) > 80:
            raise ValueError("product_id must be a string up to 80 characters")
        return {"query": query.strip(), "top_k": top_k, "product_id": product_id}
    if set(arguments) != {"locale"} or arguments["locale"] != locale:
        raise ValueError("rules locale must match the brief locale")
    return {"locale": locale}


def _draft_text(draft: Draft) -> str:
    return "\n".join(
        [draft.title, draft.meta_description, draft.h1, draft.intro]
        + draft.body + draft.social_posts
    )


def _model_fact(fact: dict[str, Any], locale: str) -> dict[str, Any]:
    """Keep the verified card for review while sending only drafting evidence."""
    localized = fact.get("localized_statement") or {}
    return {
        "id": fact["id"],
        "feature": fact["feature"],
        "statement": localized.get(locale, fact.get("statement", "")),
        "source_quote": fact["source_quote"],
        "source_url": fact["source_url"],
        "availability_note": fact.get("availability_note", ""),
    }


def _claim_errors(draft: Draft, fact_ids: set[str]) -> list[str]:
    locations = {
        "title": draft.title,
        "meta_description": draft.meta_description,
        "h1": draft.h1,
        "intro": draft.intro,
        **{f"body[{i}]": value for i, value in enumerate(draft.body)},
        **{f"social_posts[{i}]": value for i, value in enumerate(draft.social_posts)},
    }
    errors = []
    if not draft.claim_uses:
        errors.append("At least one source-backed claim is required")
    for claim in draft.claim_uses:
        if claim.fact_id not in fact_ids:
            errors.append(f"Unretrieved fact ID: {claim.fact_id}")
        passage = locations.get(claim.location)
        if passage is None or claim.sentence.casefold() not in passage.casefold():
            errors.append(f"Claim text is absent from {claim.location}")
    return errors


def _align_claim_uses(draft: Draft, fact_ids: set[str]) -> tuple[Draft, list[dict[str, str]]]:
    """Point a model-selected citation at its actual visible passage.

    This repairs only a structural copy error. It does not judge whether the
    passage is supported by the selected source; that remains human review.
    Unknown fact IDs and invalid locations are deliberately left unchanged.
    """
    locations = {
        "title": draft.title,
        "meta_description": draft.meta_description,
        "h1": draft.h1,
        "intro": draft.intro,
        **{f"body[{i}]": value for i, value in enumerate(draft.body)},
        **{f"social_posts[{i}]": value for i, value in enumerate(draft.social_posts)},
    }
    aligned = []
    changes = []
    for claim in draft.claim_uses:
        passage = locations.get(claim.location)
        if (
            claim.fact_id in fact_ids
            and passage is not None
            and claim.sentence.casefold() not in passage.casefold()
        ):
            aligned.append(claim.model_copy(update={"sentence": passage}))
            changes.append({
                "fact_id": claim.fact_id,
                "location": claim.location,
                "original_sentence": claim.sentence,
                "aligned_sentence": passage,
            })
        else:
            aligned.append(claim)
    return draft.model_copy(update={"claim_uses": aligned}), changes


def _parse_draft(content: Any) -> Draft:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Model returned no draft text")
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return Draft.model_validate(json.loads(candidate))


def _abstention_reason(content: Any) -> str | None:
    """Recognize a deliberate JSON abstention, never free-form refusal text."""
    if not isinstance(content, str):
        return None
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"abstain", "reason"}
        or payload.get("abstain") is not True
    ):
        return None
    reason = payload.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        return None
    return reason.strip()


def _result(
    *,
    facts: dict[str, dict],
    draft: Draft | None,
    checks: dict[str, Any],
    trace: list[dict[str, Any]],
    generation: GenerationResult | None,
    status: str,
) -> dict[str, Any]:
    return {
        "facts": list(facts.values()),
        "draft": draft,
        "checks": checks,
        "trace": trace,
        "generation": generation,
        "status": status,
    }


async def run_llm_agent(
    brief: Brief,
    generator: CompatibleApiGenerator,
    knowledge_path: Path,
    rules_path: Path,
) -> dict[str, Any]:
    """Let Qwen choose bounded evidence tools and revise against deterministic checks.

    `pending_review` still means a human must verify every factual claim. Endpoint
    failures return `model_error`; missing verified feature evidence returns
    `insufficient_evidence`; exhausted repairs return `needs_revision`.
    """
    facts: dict[str, dict] = {}
    rules: dict[str, Any] | None = None
    checks: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    tool_calls_used = 0
    revisions = 0
    searches = 0
    last_draft: Draft | None = None
    last_generation: GenerationResult | None = None
    started = time.perf_counter()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a growth content drafting agent for human review. First call "
                "search_knowledge and get_editorial_rules; use only their returned "
                "public fact cards for product facts. Search for the brief's exact "
                "feature. Do not invent price, availability, performance, rankings, "
                "or outcomes. If evidence is missing, do not invent a draft. "
                "If the brief requires an unsupported promise or mandatory claim, "
                "call both tools first, then output exactly "
                '{"abstain": true, "reason": "specific missing evidence"}. '
                "After reading tools, output exactly one JSON object with keys: "
                "title, meta_description, h1, intro, body (1-4 strings), "
                "social_posts (exactly two strings), claim_uses (objects with "
                "fact_id, sentence, location). Every product fact needs a claim_uses "
                "entry. Copy each claim sentence verbatim from its named field "
                "(title, meta_description, h1, intro, body[0], social_posts[0], etc.); "
                "the safest choice is the entire short field. Never use body or "
                "social_posts without a numeric index. "
                "Keep the entire JSON concise: 1-2 short body strings, two short "
                "social posts, and 1-3 claim_uses. Avoid numerals unless a fact "
                "explicitly supports them. Use the brief's requested language. "
                "No Markdown fences or extra commentary."
            ),
        },
        {"role": "user", "content": json.dumps(brief.model_dump(), ensure_ascii=False)},
    ]

    server = create_server(knowledge_path, rules_path)
    async with Client(server) as client:
        for turn in range(1, MAX_MODEL_TURNS + 1):
            call_started = time.perf_counter()
            try:
                response = await asyncio.to_thread(
                    _chat_complete, generator, messages, bool(facts) and rules is not None
                )
                usage = response.get("usage") or {}
                input_tokens += int(usage.get("prompt_tokens") or 0)
                output_tokens += int(usage.get("completion_tokens") or 0)
                message = response["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise ValueError("Model message was not an object")
            except (RuntimeError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                trace.append({
                    "step": "model_error", "turn": turn,
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc)[:200],
                })
                return _result(
                    facts=facts, draft=None,
                    checks={"passed": False, "reason": "model_endpoint_or_protocol_error"},
                    trace=trace, generation=None, status="model_error",
                )
            trace.append({
                "step": "model_response", "turn": turn,
                "duration_ms": round((time.perf_counter() - call_started) * 1000),
                "tool_calls": len(message.get("tool_calls") or []),
                "finish_reason": response["choices"][0].get("finish_reason"),
                "output_chars": len(message.get("content") or ""),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })

            calls = message.get("tool_calls") or []
            if calls:
                if not isinstance(calls, list) or tool_calls_used + len(calls) > MAX_TOOL_CALLS:
                    checks = {"passed": False, "reason": "tool_call_limit_reached"}
                    break
                # The assistant's tool-call message must precede matching tool replies.
                messages.append({
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": calls,
                })
                for call in calls:
                    tool_calls_used += 1
                    call_id = call.get("id") if isinstance(call, dict) else None
                    function = call.get("function") if isinstance(call, dict) else None
                    name = function.get("name") if isinstance(function, dict) else ""
                    raw_args = function.get("arguments") if isinstance(function, dict) else None
                    if not isinstance(call_id, str) or not call_id:
                        checks = {"passed": False, "reason": "malformed_tool_call"}
                        break
                    try:
                        args = _validated_arguments(name, raw_args, brief.locale)
                        payload = await _tool(client, name, args)
                        if name == "search_knowledge":
                            searches += 1
                            # Lexical near misses must never become product evidence.
                            matching = [
                                item for item in payload.get("results", [])
                                if isinstance(item, dict)
                                and item.get("feature") == brief.feature
                                and isinstance(item.get("id"), str)
                            ]
                            payload = {**payload, "results": matching, "count": len(matching)}
                            facts.update({item["id"]: item for item in matching})
                            payload = {
                                "query": args["query"],
                                "count": len(matching),
                                "results": [_model_fact(item, brief.locale) for item in matching],
                            }
                        else:
                            rules = payload
                            payload = {
                                "locale": brief.locale,
                                "tone": rules.get("tone", ""),
                                "forbidden_phrases": rules.get("forbidden_phrases", []),
                                "cta": rules.get("cta", ""),
                            }
                        trace.append({
                            "step": "tool_call", "turn": turn, "tool": name,
                            "selected_fact_ids": list(facts) if name == "search_knowledge" else [],
                        })
                    except (ValueError, RuntimeError, TypeError) as exc:
                        payload = {"error": str(exc)}
                        trace.append({
                            "step": "tool_rejected", "turn": turn, "tool": name,
                            "error_type": type(exc).__name__,
                        })
                    except Exception as exc:
                        payload = {"error": "MCP tool failed"}
                        trace.append({
                            "step": "tool_failed", "turn": turn, "tool": name,
                            "error_type": type(exc).__name__,
                        })
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": name,
                        "content": json.dumps(payload, ensure_ascii=False),
                    })
                if checks.get("reason") == "malformed_tool_call":
                    break
                if searches >= 2 and not facts:
                    checks = {"passed": False, "reason": "no_verified_feature_evidence"}
                    return _result(
                        facts=facts, draft=None, checks=checks, trace=trace,
                        generation=None, status="insufficient_evidence",
                    )
                continue

            # Structured abstention is valid only after the model inspected
            # both evidence search and locale rules, even if no fact matched.
            abstention = _abstention_reason(message.get("content"))
            if abstention is not None and searches > 0 and rules is not None:
                checks = {
                    "passed": False,
                    "reason": "model_abstained",
                    "model_reason": abstention,
                    "manual_review_required": True,
                    "input_tokens": input_tokens or None,
                    "output_tokens": output_tokens or None,
                }
                trace.append({"step": "model_abstained", "turn": turn})
                return _result(
                    facts=facts, draft=None, checks=checks, trace=trace,
                    generation=None, status="insufficient_evidence",
                )

            # The model may not bypass evidence and editorial rules.
            if not facts or rules is None:
                if searches and not facts and (revisions >= MAX_REVISIONS or turn == MAX_MODEL_TURNS):
                    checks = {"passed": False, "reason": "no_verified_feature_evidence"}
                    return _result(
                        facts=facts, draft=None, checks=checks, trace=trace,
                        generation=None, status="insufficient_evidence",
                    )
                if revisions >= MAX_REVISIONS:
                    checks = {"passed": False, "reason": "required_tools_not_used"}
                    break
                missing = []
                if not facts:
                    missing.append("search_knowledge for exact feature evidence")
                if rules is None:
                    missing.append("get_editorial_rules")
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({
                    "role": "user",
                    "content": "Before drafting, call the missing tools: " + ", ".join(missing),
                })
                revisions += 1
                trace.append({"step": "revision_requested", "reason": "missing_tools"})
                continue

            try:
                draft = _parse_draft(message.get("content"))
                draft, alignment_changes = _align_claim_uses(draft, set(facts))
                claim_errors = _claim_errors(draft, set(facts))
            except (ValueError, ValidationError) as exc:
                draft = None
                alignment_changes = []
                claim_errors = [
                    "Draft output reached the model token limit before JSON completed"
                    if response["choices"][0].get("finish_reason") == "length"
                    else f"Draft schema is invalid: {str(exc)[:350]}"
                ]
            if draft is not None:
                last_draft = draft
                last_generation = GenerationResult(
                    draft=draft, mode="qwen_tool_agent", model=generator.model,
                    prompt_version=PROMPT_VERSION,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    input_tokens=input_tokens or None,
                    output_tokens=output_tokens or None,
                )
                try:
                    checks = await _tool(
                        client, "check_draft",
                        {
                            "text": _draft_text(draft),
                            "fact_ids": [item.fact_id for item in draft.claim_uses],
                            "locale": brief.locale,
                        },
                    )
                except (RuntimeError, ValueError, TypeError):
                    checks = {"passed": False, "reason": "draft_check_failed"}
                checks["claim_errors"] = claim_errors
                checks["auto_aligned_claim_uses"] = len(alignment_changes)
                checks["retrieved_evidence_only"] = not any(
                    item.startswith("Unretrieved fact ID") for item in claim_errors
                )
                checks["passed"] = bool(checks.get("passed")) and not claim_errors
                if alignment_changes:
                    trace.append({
                        "step": "claim_alignment", "turn": turn,
                        "changes": alignment_changes,
                        "semantic_support_verified": False,
                    })
                trace.append({"step": "check_draft", "turn": turn, "passed": checks["passed"]})
                if checks["passed"]:
                    return _result(
                        facts=facts, draft=draft, checks=checks, trace=trace,
                        generation=last_generation, status="pending_review",
                    )
            else:
                checks = {"passed": False, "claim_errors": claim_errors}

            if revisions >= MAX_REVISIONS:
                checks["reason"] = (
                    "model_output_truncated"
                    if response["choices"][0].get("finish_reason") == "length"
                    else "revision_limit_reached"
                )
                break
            # A valid draft with a bad claim map needs the exact source fields
            # to copy from. Invalid or truncated JSON stays out of the retry.
            if draft is not None and claim_errors:
                prior = json.dumps(draft.model_dump(), ensure_ascii=False)
                retry_context = "Previous structured draft: " + prior[:4200]
            else:
                retry_context = "The previous draft attempt failed validation."
            messages.append({"role": "assistant", "content": retry_context})
            messages.append({
                "role": "user",
                "content": (
                    "Regenerate the complete concise JSON draft. Keep only claims "
                    "supported by retrieved fact IDs. For each claim_uses item, "
                    "copy sentence exactly from the named draft field. Use "
                    "body[0] or social_posts[0], never body or social_posts. "
                    "Keep one claim per short field and obey editorial rules. "
                    "Validation feedback: "
                    + json.dumps(checks, ensure_ascii=False)[:900]
                ),
            })
            revisions += 1
            trace.append({"step": "revision_requested", "reason": "draft_check_failed"})

    if not facts:
        checks = checks or {"passed": False, "reason": "no_verified_feature_evidence"}
        status = "insufficient_evidence" if searches else "needs_revision"
    else:
        checks = checks or {"passed": False, "reason": "model_turn_limit_reached"}
        status = "needs_revision"
    if last_generation is not None:
        last_generation = last_generation.model_copy(update={
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "input_tokens": input_tokens or None,
            "output_tokens": output_tokens or None,
        })
    return _result(
        facts=facts, draft=last_draft, checks=checks, trace=trace,
        generation=last_generation, status=status,
    )
