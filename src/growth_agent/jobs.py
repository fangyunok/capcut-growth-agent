"""Bounded in-process jobs with inspectable local records, for one demo process.

Queued work is deliberately not replayed after restart: a model call may have
already completed. This is not a distributed or durable execution queue.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_JOB_ID = re.compile(r"job-[a-f0-9]{32}\Z")
_STATUS = {"queued", "running", "completed", "failed", "interrupted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueueFull(RuntimeError):
    """The local worker has reached its accepted pending-work limit."""


class JobManager:
    def __init__(self, output_root: Path, *, max_pending: int = 8, concurrency: int = 2, timeout_seconds: float = 240) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 32:
            raise ValueError("max_pending must be an integer from 1 to 32")
        if type(concurrency) is not int or not 1 <= concurrency <= max_pending:
            raise ValueError("concurrency must be from 1 to max_pending")
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be greater than zero and at most 600")
        self.output_root = Path(output_root).resolve()
        self.record_root = self.output_root / "_jobs"
        self.max_pending = max_pending
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._tasks: dict[str, asyncio.Task] = {}

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        target = self.record_root / f"{job_id}.json"
        if not target.resolve().is_relative_to(self.record_root.resolve()):
            raise ValueError("Job record resolves outside its directory")
        return target

    def _read(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if path.stat().st_size > 65536:
            raise ValueError("Job record is too large")
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict) or record.get("job_id") != job_id
            or not isinstance(record.get("status"), str) or record["status"] not in _STATUS
            or not isinstance(record.get("request"), dict)
            or record.get("mode") not in {"qwen", "api"}
            or record.get("strategy") not in {"agent", "rag"}
        ):
            raise ValueError("Invalid job record")
        return record

    def _write(self, record: dict[str, Any], *, initial: bool = False) -> None:
        target = self._path(record["job_id"])
        if initial and target.exists():
            raise FileExistsError("The job record already exists")
        temporary = self.record_root / f"{record['job_id']}-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            temporary.replace(target)
        finally:
            if temporary.is_file():
                temporary.unlink()

    def _update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        record = self._read(job_id)
        record.update(changes, updated_at_utc=_now())
        self._write(record)
        return record

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self.record_root.mkdir(parents=True, exist_ok=True)
            for path in self.record_root.glob("job-*.json"):
                try:
                    record = self._read(path.stem)
                    if record["status"] in {"queued", "running"}:
                        self._update(path.stem, status="interrupted", error={
                            "code": "process_restarted",
                            "message": "上次进程结束前未记录任务完成状态；请检查产物后重新提交。",
                        })
                except (OSError, ValueError, TypeError):
                    # Preserve damaged records for inspection instead of
                    # silently replacing or replaying them.
                    continue
            self._started = True

    async def submit(
        self, request: dict[str, Any], operation: Callable[[], Awaitable[dict[str, Any]]],
        *, mode: str = "qwen", strategy: str = "agent",
    ) -> dict[str, Any]:
        await self.start()
        async with self._lock:
            active = sum(not task.done() for task in self._tasks.values())
            if self._closing or active >= self.max_pending:
                raise JobQueueFull("本地任务队列已满，请等待当前任务完成后再提交。")
            job_id = "job-" + uuid.uuid4().hex
            record = {
                "job_id": job_id, "kind": "audit", "status": "queued",
                "request": request, "mode": mode, "strategy": strategy,
                "created_at_utc": _now(), "updated_at_utc": _now(),
                "run_id": None, "result_status": None, "error": None,
            }
            self._write(record, initial=True)
            task = asyncio.create_task(self._execute(job_id, operation), name=job_id)
            self._tasks[job_id] = task
            task.add_done_callback(lambda completed, identifier=job_id: self._done(identifier, completed))
            return record

    def _done(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(job_id, None)
        if not task.cancelled():
            task.exception()  # Consume errors if record persistence itself failed.

    async def _execute(self, job_id: str, operation: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        try:
            async with self._semaphore:
                self._update(job_id, status="running")
                result = await asyncio.wait_for(operation(), timeout=self.timeout_seconds)
                if not isinstance(result, dict) or not isinstance(result.get("run_id"), str):
                    raise ValueError("Invalid job result")
                if result.get("result_status") == "model_error":
                    self._update(job_id, **result, status="failed", error={
                        "code": "model_error", "message": "模型服务调用失败，审核产物已保留；请检查服务后重新提交。",
                    })
                else:
                    self._update(job_id, **result, status="completed")
        except asyncio.CancelledError:
            self._update(job_id, status="interrupted", error={
                "code": "server_shutdown", "message": "服务关闭时任务中断；不会自动重复调用模型。",
            })
            raise
        except TimeoutError:
            self._update(job_id, status="failed", error={"code": "job_timeout", "message": "任务超过本地执行时间上限，请检查模型服务。"})
        except Exception as exc:
            self._update(job_id, status="failed", error={
                "code": "run_error", "message": "审核过程未完成，请检查产品资料、模型配置和本地产物。",
                "type": type(exc).__name__,
            })

    async def get(self, job_id: str) -> dict[str, Any]:
        await self.start()
        return self._read(job_id)

    async def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        await self.start()
        records = []
        for path in self.record_root.glob("job-*.json"):
            try:
                records.append(self._read(path.stem))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(records, key=lambda record: str(record.get("created_at_utc", "")), reverse=True)[:limit]

    async def close(self) -> None:
        self._closing = True
        identifiers = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its coroutine first ran cannot catch the
        # cancellation itself, so preserve its interrupted state here too.
        for job_id in identifiers:
            try:
                if self._read(job_id)["status"] in {"queued", "running"}:
                    self._update(job_id, status="interrupted", error={
                        "code": "server_shutdown", "message": "服务关闭时任务中断；不会自动重复调用模型。",
                    })
            except (OSError, ValueError, TypeError):
                continue
