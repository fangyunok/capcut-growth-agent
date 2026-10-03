"""Exercise the model/tool protocol without claiming that a model was run."""

from __future__ import annotations

import json
import io
import unittest
from unittest.mock import patch

from growth_agent.agent_loop import _align_claim_uses, _chat_complete, _claim_errors, run_llm_agent
from growth_agent.generation import CompatibleApiGenerator, OfflineTemplateGenerator
from growth_agent.knowledge import KnowledgeBase
from growth_agent.pipeline import DEFAULT_KNOWLEDGE, DEFAULT_RULES
from growth_agent.schemas import Brief, Draft


def _brief(feature: str = "auto-captions") -> Brief:
    return Brief(
        id="agent-loop-test", locale="en-US", feature=feature,
        audience="small business video creators",
        intent="Write a practical caption tutorial",
        seed_keyword="add captions to a video",
        cta="Explore current CapCut features",
    )


def _response(content: str | None = None, calls: list[dict] | None = None) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content, "tool_calls": calls}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _valid_draft(brief: Brief) -> dict:
    fact = KnowledgeBase.from_file(DEFAULT_KNOWLEDGE).get("capcut-auto-captions")
    rules = json.loads(DEFAULT_RULES.read_text(encoding="utf-8"))["locales"][brief.locale]
    return OfflineTemplateGenerator().generate(brief, [fact], rules).draft.model_dump()


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_alignment_only_repairs_visible_passages_for_retrieved_facts(self) -> None:
        raw = _valid_draft(_brief())
        raw["claim_uses"] = [
            {"fact_id": "capcut-auto-captions", "sentence": "A source paraphrase", "location": "intro"},
            {"fact_id": "unretrieved", "sentence": "A source paraphrase", "location": "body[0]"},
            {"fact_id": "capcut-auto-captions", "sentence": "A source paraphrase", "location": "body"},
        ]
        aligned, changes = _align_claim_uses(Draft.model_validate(raw), {"capcut-auto-captions"})
        self.assertEqual(aligned.claim_uses[0].sentence, aligned.intro)
        self.assertEqual(aligned.claim_uses[1].sentence, "A source paraphrase")
        self.assertEqual(aligned.claim_uses[2].sentence, "A source paraphrase")
        self.assertEqual(len(changes), 1)
        self.assertIn("Unretrieved fact ID: unretrieved", _claim_errors(aligned, {"capcut-auto-captions"}))
        self.assertIn("Claim text is absent from body", _claim_errors(aligned, {"capcut-auto-captions"}))

    async def test_known_feature_with_unsupported_promise_can_abstain(self) -> None:
        brief = _brief()
        brief.intent = "Claim automatic captions are 100% accurate without review."
        responses = iter([
            _response(content=json.dumps({
                "abstain": True,
                "reason": "Premature refusal without checking sources.",
            })),
            _response(calls=[
                _call("search-1", "search_knowledge", {"query": "auto-captions"}),
                _call("rules-1", "get_editorial_rules", {"locale": "en-US"}),
            ]),
            _response(content=json.dumps({
                "abstain": True,
                "reason": "The sources do not support 100% accuracy or skipping review.",
            })),
        ])
        with patch("growth_agent.agent_loop._chat_complete", side_effect=lambda *_: next(responses)):
            result = await run_llm_agent(
                brief, CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen"),
                DEFAULT_KNOWLEDGE, DEFAULT_RULES,
            )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNone(result["draft"])
        self.assertGreater(len(result["facts"]), 0)
        self.assertEqual(result["checks"]["reason"], "model_abstained")
        self.assertEqual(result["checks"]["model_reason"], "The sources do not support 100% accuracy or skipping review.")
        self.assertEqual(
            len([item for item in result["trace"] if item["step"] == "revision_requested"]), 1
        )

    async def test_unlisted_tool_and_invalid_arguments_never_reach_mcp(self) -> None:
        brief = _brief()
        responses = iter([
            _response(calls=[
                _call(str(index), "delete_knowledge", {}) if index % 2 == 0
                else _call(str(index), "search_knowledge", {"query": "captions", "top_k": 100})
            ])
            for index in range(6)
        ])
        with (
            patch("growth_agent.agent_loop._chat_complete", side_effect=lambda *_: next(responses)),
            patch("growth_agent.agent_loop._tool") as mcp_call,
        ):
            result = await run_llm_agent(
                brief, CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen"),
                DEFAULT_KNOWLEDGE, DEFAULT_RULES,
            )
        mcp_call.assert_not_called()
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(
            len([item for item in result["trace"] if item["step"] == "tool_rejected"]), 6
        )

    async def test_model_chooses_real_mcp_tools_then_submits_valid_draft(self) -> None:
        brief = _brief()
        responses = iter([
            _response(calls=[
                _call("search-1", "search_knowledge", {"query": "auto-captions add captions", "top_k": 5}),
                _call("rules-1", "get_editorial_rules", {"locale": "en-US"}),
            ]),
            _response(content=json.dumps(_valid_draft(brief))),
        ])
        seen_messages: list[list[dict]] = []

        def scripted(_generator, messages, _final_json=False):
            seen_messages.append(list(messages))
            return next(responses)

        with patch("growth_agent.agent_loop._chat_complete", side_effect=scripted):
            result = await run_llm_agent(
                brief, CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen"),
                DEFAULT_KNOWLEDGE, DEFAULT_RULES,
            )
        self.assertEqual(result["status"], "pending_review")
        self.assertTrue(result["checks"]["passed"])
        self.assertEqual(result["generation"].mode, "qwen_tool_agent")
        self.assertEqual(result["generation"].input_tokens, 20)
        self.assertEqual(len([item for item in result["trace"] if item["step"] == "tool_call"]), 2)
        self.assertEqual(len([message for message in seen_messages[1] if message["role"] == "tool"]), 2)

    async def test_missing_feature_evidence_stops_without_draft(self) -> None:
        brief = _brief("ai-dubbing")
        responses = iter([
            _response(calls=[
                _call("search-1", "search_knowledge", {"query": "ai-dubbing"}),
                _call("rules-1", "get_editorial_rules", {"locale": "en-US"}),
            ]),
            _response(calls=[_call("search-2", "search_knowledge", {"query": "ai dubbing CapCut"})]),
        ])
        with patch("growth_agent.agent_loop._chat_complete", side_effect=lambda *_: next(responses)):
            result = await run_llm_agent(
                brief, CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen"),
                DEFAULT_KNOWLEDGE, DEFAULT_RULES,
            )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNone(result["draft"])
        self.assertEqual(result["facts"], [])

    async def test_failed_rule_check_drives_one_revision(self) -> None:
        brief = _brief()
        valid = _valid_draft(brief)
        invalid = json.loads(json.dumps(valid))
        invalid["body"].append("100% accurate in every situation.")
        responses = iter([
            _response(calls=[
                _call("search-1", "search_knowledge", {"query": "auto-captions"}),
                _call("rules-1", "get_editorial_rules", {"locale": "en-US"}),
            ]),
            _response(content=json.dumps(invalid)),
            _response(content=json.dumps(valid)),
        ])
        seen_messages: list[list[dict]] = []

        def scripted(_generator, messages, _final_json=False):
            seen_messages.append(list(messages))
            return next(responses)

        with patch("growth_agent.agent_loop._chat_complete", side_effect=scripted):
            result = await run_llm_agent(
                brief, CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen"),
                DEFAULT_KNOWLEDGE, DEFAULT_RULES,
            )
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(
            len([item for item in result["trace"] if item["step"] == "revision_requested"]), 1
        )
        self.assertIn("100% accurate", seen_messages[2][-1]["content"])

    def test_draft_request_switches_from_tools_to_json_mode(self) -> None:
        generator = CompatibleApiGenerator("http://localhost:9999/v1", "mock-qwen")
        messages = [{"role": "user", "content": "test"}]
        with patch(
            "growth_agent.agent_loop.urllib.request.urlopen",
            return_value=io.BytesIO(b'{"choices": [{"message": {"content": "{}"}}]}'),
        ) as request_call:
            _chat_complete(generator, messages, False)
            tool_payload = json.loads(request_call.call_args.args[0].data)
        self.assertIn("tools", tool_payload)
        self.assertNotIn("response_format", tool_payload)

        with patch(
            "growth_agent.agent_loop.urllib.request.urlopen",
            return_value=io.BytesIO(b'{"choices": [{"message": {"content": "{}"}}]}'),
        ) as request_call:
            _chat_complete(generator, messages, True)
            draft_payload = json.loads(request_call.call_args.args[0].data)
        self.assertNotIn("tools", draft_payload)
        self.assertEqual(draft_payload["response_format"], {"type": "json_object"})
        self.assertGreater(draft_payload["max_tokens"], tool_payload["max_tokens"])


if __name__ == "__main__":
    unittest.main()
