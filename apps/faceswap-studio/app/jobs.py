"""In-memory background jobs with progress reporting.

A single worker thread runs jobs one at a time: GPU work (image generation) must be
serialized anyway, and a queue lets the UI show "position in queue" instead of a frozen spinner.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("image_studio.jobs")

QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"
CANCELLED_CODE = 499
CANCELLED_DETAIL = "Задача отменена."
STAGE_QUEUED = "В очереди"
STAGE_STARTING = "Запуск…"
STAGE_DONE = "Готово"

# progress(stage, fraction 0..1); may raise JobCancelled when the job was cancelled
ProgressFn = Callable[[str, float], None]


class JobCancelled(Exception):
    """Raised inside a job when its cancel flag is set."""


@dataclass
class JobError:
    code: int
    detail: str


@dataclass
class Job:
    kind: str
    fn: Callable[[JobContext], dict]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = QUEUED
    stage: str = STAGE_QUEUED
    progress: float = 0.0
    result: dict | None = None
    error: JobError | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)

    def wait(self, timeout: float | None = None) -> bool:
        return self.done_event.wait(timeout)


class JobContext:
    """Handed to a job function: progress reporting + cooperative cancellation."""

    def __init__(self, job: Job) -> None:
        self._job = job

    @property
    def cancelled(self) -> bool:
        return self._job.cancel_event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled(CANCELLED_DETAIL)

    def report(self, stage: str, fraction: float | None = None) -> None:
        self.check()
        self._job.stage = stage
        if fraction is not None:
            # never go backwards: sub-stages may restart their own 0..1 range
            self._job.progress = max(self._job.progress, min(max(float(fraction), 0.0), 1.0))

    def scaled(self, start: float, end: float) -> ProgressFn:
        """A progress callback mapping a sub-task's 0..1 onto start..end of the whole job."""

        def progress(stage: str, fraction: float) -> None:
            self.report(stage, start + (end - start) * min(max(fraction, 0.0), 1.0))

        return progress


ErrorMapper = Callable[[Exception], JobError]


def default_error_mapper(exc: Exception) -> JobError:
    log.error("Job failed", exc_info=exc)
    return JobError(500, "Внутренняя ошибка сервера. Подробности в журнале.")


class JobManager:
    def __init__(
        self,
        error_mapper: ErrorMapper = default_error_mapper,
        max_jobs: int = 100,
        idle_timeout: float = 30.0,
    ) -> None:
        self._map_error = error_mapper
        self._max_jobs = max_jobs
        self._idle_timeout = idle_timeout
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._pending: deque[Job] = deque()
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ public

    def submit(self, kind: str, fn: Callable[[JobContext], dict]) -> Job:
        job = Job(kind=kind, fn=fn)
        with self._cond:
            self._jobs[job.id] = job
            self._pending.append(job)
            self._prune()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="fss-job-worker", daemon=True)
                self._worker.start()
            self._cond.notify()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._cond:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status in {DONE, ERROR}:
                return job
            job.cancel_event.set()
            if job.status == QUEUED:
                self._pending.remove(job)
                self._finish(job, error=JobError(CANCELLED_CODE, CANCELLED_DETAIL))
        return job

    def snapshot(self, job: Job) -> dict[str, Any]:
        with self._cond:
            position = None
            if job.status == QUEUED:
                try:
                    position = self._pending.index(job) + 1
                except ValueError:
                    position = None
            return {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "stage": job.stage,
                "progress": round(job.progress, 3),
                "queue_position": position,
                "result": job.result,
                "error": {"code": job.error.code, "detail": job.error.detail} if job.error else None,
                "cancel_requested": job.cancel_event.is_set(),
                "created": job.created,
                "started": job.started,
                "finished": job.finished,
            }

    # ------------------------------------------------------------------ worker

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait(timeout=self._idle_timeout)
                    if not self._pending:
                        # idle: let the thread exit; submit() starts a new one
                        self._worker = None
                        return
                job = self._pending.popleft()
                job.status, job.stage = RUNNING, STAGE_STARTING
                job.started = time.time()
            self._execute(job)

    def _execute(self, job: Job) -> None:
        ctx = JobContext(job)
        try:
            ctx.check()
            result = job.fn(ctx)
        except JobCancelled:
            log.info("Job %s cancelled", job.id)
            error = JobError(CANCELLED_CODE, CANCELLED_DETAIL)
        except Exception as exc:  # noqa: BLE001 - mapped into a job error
            error = self._map_error(exc)
        else:
            with self._cond:
                self._finish(job, result=result)
            return
        with self._cond:
            self._finish(job, error=error)

    def _finish(self, job: Job, result: dict | None = None, error: JobError | None = None) -> None:
        job.finished = time.time()
        if error is None:
            job.status, job.stage, job.progress, job.result = DONE, STAGE_DONE, 1.0, result
        else:
            job.status, job.error = ERROR, error
            job.stage = "Отменено" if error.code == CANCELLED_CODE else "Ошибка"
        job.fn = _noop  # drop references to input images
        job.done_event.set()

    def _prune(self) -> None:
        excess = len(self._jobs) - self._max_jobs
        if excess <= 0:
            return
        for job_id in [j.id for j in self._jobs.values() if j.status in {DONE, ERROR}][:excess]:
            del self._jobs[job_id]


def _noop(_ctx: JobContext) -> dict:
    return {}
