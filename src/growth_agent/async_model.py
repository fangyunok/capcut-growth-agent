"""Cancellable model HTTP calls for the local asynchronous web worker."""

from __future__ import annotations

from typing import Any

import httpx

from .agent_loop import _TOOLS
from .generation import CompatibleApiGenerator


async def async_chat_complete(
    generator: CompatibleApiGenerator,
    messages: list[dict[str, Any]],
    final_json: bool = False,
) -> dict[str, Any]:
    """Use the shared chat protocol and close the connection on cancellation.

    Cancelling the client request does not guarantee that a remote model server
    cancels inference. Provider URLs, response bodies, and credentials are kept
    out of the errors returned to the application.
    """
    payload: dict[str, Any] = {
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
    headers = {"Content-Type": "application/json"}
    if generator.api_key:
        headers["Authorization"] = f"Bearer {generator.api_key}"
    try:
        async with httpx.AsyncClient(timeout=generator.timeout, trust_env=False) as client:
            response = await client.post(
                f"{generator.base_url}/chat/completions", json=payload, headers=headers,
            )
            response.raise_for_status()
            try:
                result = response.json()
            except ValueError:
                raise RuntimeError("Model service returned invalid JSON") from None
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Model service returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError:
        raise RuntimeError("Cannot reach model service") from None
    if not isinstance(result, dict):
        raise ValueError("Model service returned a non-object response")
    return result
