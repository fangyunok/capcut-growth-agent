"""Verify cancellation and protocol behavior against a real loopback server."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from growth_agent.async_model import async_chat_complete
from growth_agent.generation import CompatibleApiGenerator


class AsyncModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.received: list[dict] = []
        self.request_received = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.handlers: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.hold_response = False
        self.status = 200
        self.response_body = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.generator = CompatibleApiGenerator(
            f"http://127.0.0.1:{port}/v1", "test-model", api_key="fake-api-token", timeout=2,
        )

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("ascii").split("\r\n")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {name.lower(): value.strip() for name, value in headers.items()}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.received.append({"request_line": lines[0], "headers": headers, "payload": json.loads(body)})
            self.request_received.set()
            if self.hold_response:
                remaining = await reader.read()
                if remaining == b"":
                    self.disconnected.set()
                return
            response = (
                f"HTTP/1.1 {self.status} Test Response\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n"
                f"Content-Length: {len(self.response_body)}\r\n\r\n"
            ).encode("ascii") + self.response_body
            writer.write(response)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            self.disconnected.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.writers.discard(writer)
            self.handlers.discard(task)

    async def test_tool_and_final_json_requests_match_the_model_protocol(self) -> None:
        messages = [{"role": "user", "content": "检查中英文营销文案"}]
        for final_json in (False, True):
            result = await async_chat_complete(self.generator, messages, final_json)
            self.assertEqual(result["choices"][0]["message"]["content"], "{}")
        first, final = self.received
        self.assertEqual(first["request_line"], "POST /v1/chat/completions HTTP/1.1")
        self.assertEqual(first["headers"]["authorization"], "Bearer fake-api-token")
        self.assertEqual(first["payload"]["messages"], messages)
        self.assertEqual(first["payload"]["model"], "test-model")
        self.assertEqual(first["payload"]["temperature"], 0)
        self.assertFalse(first["payload"]["stream"])
        self.assertEqual(first["payload"]["max_tokens"], 450)
        self.assertEqual(first["payload"]["tool_choice"], "auto")
        self.assertEqual({tool["function"]["name"] for tool in first["payload"]["tools"]}, {
            "search_knowledge", "get_editorial_rules",
        })
        self.assertNotIn("response_format", first["payload"])
        self.assertEqual(final["payload"]["max_tokens"], 1800)
        self.assertEqual(final["payload"]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", final["payload"])
        self.assertNotIn("tool_choice", final["payload"])

    async def test_cancelling_a_pending_reply_disconnects_the_client(self) -> None:
        self.hold_response = True
        call = asyncio.create_task(async_chat_complete(self.generator, []))
        self.addAsyncCleanup(self._cancel, call)
        await asyncio.wait_for(self.request_received.wait(), timeout=2)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(self.disconnected.wait(), timeout=2)

    @staticmethod
    async def _cancel(task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_http_error_reports_status_without_body_url_or_credentials(self) -> None:
        self.status = 401
        self.response_body = b'{"error":"fake-sensitive-provider-detail"}'
        with self.assertRaises(RuntimeError) as caught:
            await async_chat_complete(self.generator, [])
        self.assertEqual(str(caught.exception), "Model service returned HTTP 401")
        self.assertTrue(caught.exception.__suppress_context__)
        for private in ("fake-sensitive-provider-detail", "fake-api-token", self.generator.base_url):
            self.assertNotIn(private, str(caught.exception))

    async def test_read_timeout_disconnects_and_reports_a_safe_error(self) -> None:
        self.hold_response = True
        self.generator.timeout = 0.03
        with self.assertRaisesRegex(RuntimeError, "Cannot reach model service"):
            await async_chat_complete(self.generator, [])
        await asyncio.wait_for(self.disconnected.wait(), timeout=2)

    async def test_invalid_json_and_non_object_responses_are_rejected(self) -> None:
        self.response_body = b"fake-sensitive-invalid-json"
        with self.assertRaisesRegex(RuntimeError, "Model service returned invalid JSON"):
            await async_chat_complete(self.generator, [])
        self.response_body = b"[]"
        with self.assertRaisesRegex(ValueError, "non-object response"):
            await async_chat_complete(self.generator, [])

    async def test_environment_proxy_configuration_does_not_redirect_model_calls(self) -> None:
        with patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
            "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
            "all_proxy": "http://127.0.0.1:1", "no_proxy": "",
        }):
            await async_chat_complete(self.generator, [])
        self.assertEqual(len(self.received), 1)


if __name__ == "__main__":
    unittest.main()
