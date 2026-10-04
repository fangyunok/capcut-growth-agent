"""Retrieval strategies must be interchangeable without changing behaviour.

These tests cover the sparse strategies and the fusion logic. Dense strategies
are exercised through a deterministic stand-in backend so the plumbing can be
verified without downloading a model.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from growth_agent.knowledge import KnowledgeBase
from growth_agent.resources import DEFAULT_AUDIT_KNOWLEDGE
from growth_agent.retrieval import (
    BM25Retriever,
    STRATEGIES,
    build_retriever,
    default_strategy,
)
from growth_agent.retrieval.hybrid import HybridRetriever
from growth_agent.retrieval.lexical import LexicalRetriever
from growth_agent.retrieval.text import index_terms

try:
    import numpy  # noqa: F401
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


def _catalog_facts() -> list[dict]:
    return KnowledgeBase.from_file(DEFAULT_AUDIT_KNOWLEDGE).all()


class _KeywordBackend:
    """Deterministic stand-in so vector plumbing is testable without a model."""

    name = "test:keyword"
    VOCABULARY = ("virtual", "camera", "scene", "recording", "remux", "wizard")

    def encode(self, texts: list[str]):
        import numpy as np

        rows = [
            [1.0 if word in text.casefold() else 0.0 for word in self.VOCABULARY]
            for text in texts
        ]
        matrix = np.asarray(rows, dtype="float32")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return matrix / norms


class _FixedRetriever:
    """Returns a predetermined ranking, for testing fusion arithmetic."""

    def __init__(self, name: str, ids: list[str], facts: dict[str, dict]):
        self.name = name
        self._ids = ids
        self._facts = facts

    def search(self, query, top_k=5, product_id=""):
        output = []
        for fact_id in self._ids:
            if fact_id not in self._facts:
                continue
            output.append(dict(self._facts[fact_id]))
        return output[:top_k]


class TextNormalisationTests(unittest.TestCase):
    def test_cjk_queries_become_character_bigrams(self) -> None:
        tokens = index_terms("虚拟摄像头")
        self.assertIn("虚拟", tokens)
        self.assertIn("拟摄", tokens)
        self.assertNotIn("虚拟摄像头", tokens)

    def test_latin_queries_keep_word_tokens_and_drop_stopwords(self) -> None:
        tokens = index_terms("the virtual camera is on")
        self.assertIn("virtual", tokens)
        self.assertIn("camera", tokens)
        self.assertNotIn("the", tokens)

    def test_empty_input_yields_no_tokens(self) -> None:
        self.assertEqual(index_terms("   "), [])


class StrategyFactoryTests(unittest.TestCase):
    def tearDown(self) -> None:
        import os

        os.environ.pop("GROWTH_RETRIEVAL", None)

    def test_default_strategy_is_the_lexical_baseline(self) -> None:
        import os

        os.environ.pop("GROWTH_RETRIEVAL", None)
        self.assertEqual(default_strategy(), "lexical")

    def test_environment_selects_the_strategy(self) -> None:
        import os

        os.environ["GROWTH_RETRIEVAL"] = "BM25"
        self.assertEqual(default_strategy(), "bm25")

    def test_unknown_strategy_is_rejected_with_the_available_list(self) -> None:
        with self.assertRaises(ValueError) as caught:
            build_retriever(_catalog_facts(), strategy="does-not-exist")
        self.assertIn("lexical", str(caught.exception))

    def test_declared_sparse_strategies_build(self) -> None:
        facts = _catalog_facts()
        for name in ("lexical", "bm25"):
            with self.subTest(strategy=name):
                self.assertIn(name, STRATEGIES)
                self.assertEqual(build_retriever(facts, strategy=name).name, name)


class BaselineParityTests(unittest.TestCase):
    """The default path must behave exactly as it did before the refactor."""

    def setUp(self) -> None:
        self.catalog = KnowledgeBase.from_file(DEFAULT_AUDIT_KNOWLEDGE)

    def test_default_retriever_is_lexical(self) -> None:
        self.assertEqual(self.catalog.retriever_name, "lexical")

    def test_exact_feature_query_returns_cards(self) -> None:
        results = self.catalog.search("virtual-camera", product_id="obs-studio")
        self.assertTrue(results)
        self.assertTrue(all(f["feature"] == "virtual-camera" for f in results))

    def test_product_isolation_still_holds(self) -> None:
        self.assertEqual(
            self.catalog.search("virtual-camera", product_id="another-product"), []
        )

    def test_blank_query_returns_nothing(self) -> None:
        self.assertEqual(self.catalog.search("   ", product_id="obs-studio"), [])

    def test_scored_cards_keep_the_original_explainability_keys(self) -> None:
        results = self.catalog.search("virtual-camera", product_id="obs-studio")
        self.assertIn("retrieval_score", results[0])
        self.assertIn("matched_terms", results[0])


class BM25Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = _catalog_facts()
        self.retriever = BM25Retriever(self.facts)

    def test_matching_feature_ranks_first(self) -> None:
        results = self.retriever.search("virtual camera", product_id="obs-studio")
        self.assertTrue(results)
        self.assertEqual(results[0]["feature"], "virtual-camera")

    def test_product_isolation_holds(self) -> None:
        self.assertEqual(
            self.retriever.search("virtual camera", product_id="unknown-product"), []
        )

    def test_unrelated_query_returns_nothing(self) -> None:
        self.assertEqual(
            self.retriever.search("quantum chromodynamics", product_id="obs-studio"), []
        )

    def test_chinese_query_matches_through_bigrams(self) -> None:
        results = self.retriever.search("虚拟摄像头", product_id="obs-studio")
        self.assertTrue(results)
        self.assertEqual(results[0]["feature"], "virtual-camera")

    def test_scores_are_positive_and_matched_terms_are_reported(self) -> None:
        results = self.retriever.search("virtual camera", product_id="obs-studio")
        self.assertGreater(results[0]["retrieval_score"], 0)
        self.assertTrue(results[0]["matched_terms"])

    def test_empty_catalog_does_not_raise(self) -> None:
        empty = BM25Retriever([])
        self.assertEqual(empty.search("virtual camera"), [])

    def test_blank_query_returns_nothing(self) -> None:
        self.assertEqual(self.retriever.search(""), [])


class HybridFusionTests(unittest.TestCase):
    """RRF arithmetic, verified with fixed component rankings."""

    def setUp(self) -> None:
        self.facts = {fact["id"]: fact for fact in _catalog_facts()}
        self.ids = list(self.facts)

    def test_a_card_ranked_by_both_components_beats_a_single_component_card(self) -> None:
        shared, only_first, only_second = self.ids[0], self.ids[1], self.ids[2]
        first = _FixedRetriever("first", [only_first, shared], self.facts)
        second = _FixedRetriever("second", [only_second, shared], self.facts)
        hybrid = HybridRetriever(self.facts.values(), retrievers=[first, second])

        results = hybrid.search("anything", top_k=3)
        self.assertEqual(results[0]["id"], shared)

    def test_a_card_found_by_only_one_component_is_retained(self) -> None:
        single = self.ids[0]
        first = _FixedRetriever("first", [single], self.facts)
        second = _FixedRetriever("second", [], self.facts)
        hybrid = HybridRetriever(self.facts.values(), retrievers=[first, second])

        self.assertEqual([card["id"] for card in hybrid.search("q")], [single])

    def test_no_component_hits_returns_nothing(self) -> None:
        first = _FixedRetriever("first", [], self.facts)
        second = _FixedRetriever("second", [], self.facts)
        hybrid = HybridRetriever(self.facts.values(), retrievers=[first, second])
        self.assertEqual(hybrid.search("q"), [])

    def test_product_isolation_is_forwarded_to_every_component(self) -> None:
        seen: list[str] = []

        class _Recording(_FixedRetriever):
            def search(self, query, top_k=5, product_id=""):
                seen.append(product_id)
                return super().search(query, top_k=top_k, product_id=product_id)

        hybrid = HybridRetriever(
            self.facts.values(),
            retrievers=[
                _Recording("a", [], self.facts),
                _Recording("b", [], self.facts),
            ],
        )
        hybrid.search("q", product_id="obs-studio")
        self.assertEqual(seen, ["obs-studio", "obs-studio"])

    def test_component_names_are_reported_for_the_run_trace(self) -> None:
        first = _FixedRetriever("first", [], self.facts)
        second = _FixedRetriever("second", [], self.facts)
        hybrid = HybridRetriever(self.facts.values(), retrievers=[first, second])
        self.assertEqual(hybrid.components, ["first", "second"])
        self.assertIn("hybrid", hybrid.name)


class VectorPlumbingTests(unittest.TestCase):
    """Dense retrieval internals, exercised through the stand-in backend."""

    def setUp(self) -> None:
        if not HAS_NUMPY:
            self.skipTest("numpy is not installed")
        from growth_agent.retrieval.vector import VectorRetriever

        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.retriever = VectorRetriever(
            _catalog_facts(), backend=_KeywordBackend(),
            cache_dir=self._temporary.name,
        )

    def test_matching_query_ranks_the_expected_card_first(self) -> None:
        results = self.retriever.search("virtual camera", product_id="obs-studio")
        self.assertTrue(results)
        self.assertEqual(results[0]["feature"], "virtual-camera")

    def test_product_isolation_holds_for_dense_retrieval(self) -> None:
        results = self.retriever.search("virtual camera", product_id="no-such-product")
        self.assertEqual(results, [])

    def test_cached_vectors_are_reused_on_a_second_build(self) -> None:
        from growth_agent.retrieval.vector import VectorRetriever

        rebuilt = VectorRetriever(
            _catalog_facts(), backend=_KeywordBackend(),
            cache_dir=self._temporary.name,
        )
        self.assertEqual(rebuilt._matrix.shape[1], self.retriever._matrix.shape[1])
        cached = list(Path(self._temporary.name).glob("vectors-*.npz"))
        self.assertEqual(len(cached), 1)

    def test_blank_query_returns_nothing(self) -> None:
        self.assertEqual(self.retriever.search("  "), [])


if __name__ == "__main__":
    unittest.main()
