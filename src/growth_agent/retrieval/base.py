"""Retriever interface shared by every ranking strategy.

Each implementation ranks the same fact-card structure and must honour the
product isolation rule: a query scoped to one product never returns another
product's card, however close the wording looks.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Retriever(Protocol):
    """Rank source-backed fact cards for a query."""

    name: str

    def search(
        self, query: str, top_k: int = 5, product_id: str = ""
    ) -> list[dict[str, Any]]:
        """Return ranked cards.

        Each card keeps its original fields plus the strategy's own
        explainability keys, so a comparison can attribute a difference to the
        ranking model rather than to a changed output shape.
        """
        ...
