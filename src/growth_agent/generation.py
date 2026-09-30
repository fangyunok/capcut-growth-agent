"""Draft generation modes.

The offline mode is a deterministic demonstration, not an LLM. The API mode
uses a configurable Chat Completions-compatible endpoint when one is available.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import ValidationError

from .schemas import Brief, ClaimUse, Draft, GenerationResult


PROMPT_VERSION = "growth-draft-v1"


def _localized_fact(fact: dict[str, Any], locale: str) -> str:
    translations = fact.get("localized_statement", {})
    if locale not in translations:
        raise ValueError(f"Fact {fact.get('id')} has no reviewed {locale} wording")
    return str(translations[locale]).strip()


class OfflineTemplateGenerator:
    """Make an auditable preview from curated bilingual fact wording."""

    def generate(self, brief: Brief, facts: list[dict], rules: dict) -> GenerationResult:
        start = time.perf_counter()
        selected = facts[:2]
        if not selected:
            raise ValueError("At least one evidence fact is required")
        claim_uses: list[ClaimUse] = []
        body: list[str] = []
        for index, fact in enumerate(selected):
            sentence = _localized_fact(fact, brief.locale)
            body.append(sentence)
            claim_uses.append(
                ClaimUse(fact_id=fact["id"], sentence=sentence, location=f"body[{index}]")
            )

        cta = brief.cta.strip() or rules["cta"]
        if brief.locale == "es-ES":
            title = f"{brief.seed_keyword}: guía práctica | CapCut"
            h1 = brief.seed_keyword.capitalize()
            intro = (
                f"Para {brief.audience}, esta guía parte de una función descrita "
                "en las páginas públicas de CapCut."
            )
            body.append("Revisa la disponibilidad y el resultado antes de publicar.")
            social_posts = [
                f"{_localized_fact(selected[0], brief.locale)} {cta}.",
                f"¿Preparas un vídeo para {brief.audience}? Revisa el resultado antes de publicarlo. {cta}.",
            ]
            meta = f"Guía práctica sobre {brief.seed_keyword} para {brief.audience}."
        else:
            title = f"{brief.seed_keyword}: a practical guide | CapCut"
            h1 = brief.seed_keyword.capitalize()
            intro = (
                f"For {brief.audience}, this guide starts with a feature "
                "described in public CapCut pages."
            )
            body.append("Check current availability and review the result before publishing.")
            social_posts = [
                f"{_localized_fact(selected[0], brief.locale)} {cta}.",
                f"Making a video for {brief.audience}? Review the result before posting. {cta}.",
            ]
            meta = f"A practical guide to {brief.seed_keyword} for {brief.audience}."

        claim_uses.append(
            ClaimUse(
                fact_id=selected[0]["id"],
                sentence=_localized_fact(selected[0], brief.locale),
                location="social_posts[0]",
            )
        )

        draft = Draft(
            title=title,
            meta_description=meta,
            h1=h1,
            intro=intro,
            body=body,
            social_posts=social_posts,
            claim_uses=claim_uses,
        )
        return GenerationResult(
            draft=draft,
            mode="offline_template_demo",
            prompt_version="curated-template-v1",
            duration_ms=round((time.perf_counter() - start) * 1000),
        )


class CompatibleApiGenerator:
    """Use a user-supplied Chat Completions-compatible model endpoint."""

    generation_mode = "api"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 90):
        if not base_url or not model:
            raise ValueError("GROWTH_API_BASE and GROWTH_MODEL are required in api mode")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "CompatibleApiGenerator":
        return cls(
            base_url=os.getenv("GROWTH_API_BASE", ""),
            model=os.getenv("GROWTH_MODEL", ""),
            api_key=os.getenv("GROWTH_API_KEY", ""),
        )

    def generate(self, brief: Brief, facts: list[dict], rules: dict) -> GenerationResult:
        if not facts:
            raise ValueError("At least one evidence fact is required")
        evidence = [
            {
                "id": fact["id"],
                "approved_wording": _localized_fact(fact, brief.locale),
                "source_url": fact["source_url"],
                "source_quote": fact["source_quote"],
                "availability_note": fact.get("availability_note", ""),
            }
            for fact in facts
        ]
        system = (
            "You draft useful growth content for human editorial review. "
            "Use only the provided evidence for product facts. Do not invent pricing, "
            "performance, availability, integrations, rankings, or traffic outcomes. "
            "Return only one JSON object matching these keys exactly: title, "
            "meta_description, h1, intro, body (1-4 strings), social_posts "
            "(exactly 2 strings), claim_uses (objects with fact_id, sentence, location). "
            "For every factual sentence about the product, provide a claim_uses entry "
            "with its evidence fact ID and location. If evidence is insufficient, "
            "say so in the draft and avoid unsupported facts."
        )
        payload = {
            "brief": brief.model_dump(),
            "editorial_rules": rules,
            "evidence": evidence,
            "output_language": brief.locale,
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        start = time.perf_counter()
        total_input = 0
        total_output = 0
        last_error = ""
        for attempt in range(2):
            response = self._request(messages)
            usage = response.get("usage") or {}
            total_input += int(usage.get("prompt_tokens") or 0)
            total_output += int(usage.get("completion_tokens") or 0)
            try:
                content = response["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("Model response content was not a string")
                candidate = content.strip()
                if candidate.startswith("```"):
                    candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                draft = Draft.model_validate(json.loads(candidate))
                return GenerationResult(
                    draft=draft,
                    mode=self.generation_mode,
                    model=self.model,
                    prompt_version=PROMPT_VERSION,
                    duration_ms=round((time.perf_counter() - start) * 1000),
                    input_tokens=total_input or None,
                    output_tokens=total_output or None,
                )
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": str(response.get("choices", [{}])[0].get("message", {}).get("content", ""))})
                    messages.append({"role": "user", "content": f"Repair the JSON to match the schema. Validation error: {last_error}"})
        raise ValueError(f"Model did not return a valid draft after 2 attempts: {last_error}")

    def _request(self, messages: list[dict]) -> dict:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(
            {"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": 900},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                result = json.load(reply)
        except urllib.error.HTTPError as exc:
            details = exc.read(300).decode("utf-8", errors="replace")
            raise RuntimeError(f"Model endpoint returned HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach model endpoint: {exc.reason}") from exc
        if not isinstance(result, dict):
            raise ValueError("Model endpoint returned a non-object JSON value")
        return result


class QwenOllamaGenerator(CompatibleApiGenerator):
    """Run an open-weight Qwen model served by a local Ollama instance."""

    generation_mode = "qwen_ollama"

    @classmethod
    def from_environment(cls) -> "QwenOllamaGenerator":
        return cls(
            base_url=os.getenv("GROWTH_QWEN_BASE", "http://127.0.0.1:11434/v1"),
            model=os.getenv("GROWTH_QWEN_MODEL", "qwen3:4b-instruct"),
            api_key="",
            timeout=int(os.getenv("GROWTH_QWEN_TIMEOUT", "900")),
        )

    def probe(self) -> None:
        """Require the selected Qwen model to be loaded in the local model catalog."""
        request = urllib.request.Request(f"{self.base_url}/models", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                payload = json.load(reply)
        except (urllib.error.URLError, ValueError) as exc:
            raise RuntimeError(
                "Cannot reach local Qwen model service. Start Ollama and pull "
                f"{self.model} before using --mode qwen."
            ) from exc
        model_ids = {
            item.get("id") for item in payload.get("data", []) if isinstance(item, dict)
        }
        if self.model not in model_ids:
            raise RuntimeError(
                f"Qwen model {self.model!r} is not installed in Ollama. "
                f"Available models: {sorted(str(model) for model in model_ids if model)}. "
                f"Run: ollama pull {self.model}"
            )

    def generate(self, brief: Brief, facts: list[dict], rules: dict) -> GenerationResult:
        self.probe()
        return super().generate(brief, facts, rules)

    def _request(self, messages: list[dict]) -> dict:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 900,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                result = json.load(reply)
        except urllib.error.HTTPError as exc:
            details = exc.read(300).decode("utf-8", errors="replace")
            raise RuntimeError(f"Qwen service returned HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach Qwen service: {exc.reason}") from exc
        if not isinstance(result, dict):
            raise ValueError("Qwen service returned a non-object JSON value")
        return result
