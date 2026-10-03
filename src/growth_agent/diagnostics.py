"""Bounded, secret-free checks for model connectivity and media prerequisites."""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .generation import CompatibleApiGenerator, QwenOllamaGenerator
from .resources import DEFAULT_AUDIT_KNOWLEDGE, DEFAULT_AUDIT_RULES, DEFAULT_OUTPUT_ROOT
from .media import find_ffmpeg


def diagnose(*, mode: str = "qwen", timeout: int = 5) -> dict:
    if mode not in {"qwen", "api"}:
        raise ValueError("mode must be qwen or api")
    report = {
        "mode": mode, "sample_data_ready": DEFAULT_AUDIT_KNOWLEDGE.is_file() and DEFAULT_AUDIT_RULES.is_file(),
        "output_root": str(DEFAULT_OUTPUT_ROOT),
        "ffmpeg_ready": bool(shutil.which(os.getenv("GROWTH_FFMPEG", "ffmpeg"))),
        "model_ready": False,
    }
    try:
        find_ffmpeg()
        report["ffmpeg_ready"] = True
    except (ValueError, RuntimeError, OSError):
        report["ffmpeg_ready"] = False
    try:
        generator = QwenOllamaGenerator.from_environment() if mode == "qwen" else CompatibleApiGenerator.from_environment()
        parsed = urlsplit(generator.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid model endpoint")
        report["model"] = generator.model
        # Do not print URLs, query strings, credentials, headers or raw provider errors.
        report["endpoint_scope"] = "loopback" if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else "remote"
        request = urllib.request.Request(
            generator.base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + generator.api_key} if generator.api_key else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Invalid models catalog")
        available = {item.get("id") for item in payload["data"] if isinstance(item, dict)}
        report["model_ready"] = generator.model in available
        report["model_status"] = "available" if report["model_ready"] else "not_in_catalog"
        if not report["model_ready"]:
            report["next_step"] = "Load the configured model on your server, then rerun doctor."
    except Exception as exc:
        report["model_status"] = "unavailable"
        report["error_type"] = type(exc).__name__
        report["next_step"] = "Check the server, SSH tunnel, model name and environment configuration."
    report["catalog_check_only"] = True
    return report
