"""FastAPI application: REST API + static web UI."""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from . import __version__
from .backends import (
    Backends,
    BackendUnavailable,
    GenerateParams,
    build_backends,
)
from .config import Settings
from .jobs import ERROR, Job, JobContext, JobError, JobManager, ProgressFn
from .registry import list_models, resolve_model
from .safety import (
    ContentBlocked,
    NsfwClassifier,
    apply_watermark,
    check_prompt,
    provenance_metadata,
)

log = logging.getLogger("image_studio")
STATIC_DIR = Path(__file__).resolve().parent / "static"
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
JOB_ID_RE = IMAGE_ID_RE
STAGE_SAVE = "Проверка и сохранение…"
INTERNAL_ERROR = "Внутренняя ошибка сервера. Подробности в журнале сервера."


class LoraSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    weight: float = Field(default=0.8, ge=-2.0, le=2.0)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    negative_prompt: str = Field(default="", max_length=1000)
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)
    steps: int | None = Field(default=None, ge=1)
    guidance: float = Field(default=7.0, ge=0.0, le=20.0)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    # File name from /api/models -> checkpoints; empty = default model
    model: str | None = Field(default=None, max_length=255)
    loras: list[LoraSpec] = Field(default_factory=list)


class ImageResult(BaseModel):
    id: str
    url: str
    seed: int | None = None


class JobAccepted(BaseModel):
    job_id: str


@dataclass(frozen=True)
class JobSpec:
    """Validated input, ready to run: `fn(ctx)` returns an ImageResult dict."""

    kind: str
    fn: Callable[[JobContext], dict]


def map_job_error(exc: Exception) -> JobError:
    """Same status codes as the exception handlers of the blocking endpoints."""
    if isinstance(exc, ContentBlocked):
        return JobError(422, str(exc))
    if isinstance(exc, BackendUnavailable):
        return JobError(503, str(exc))
    if isinstance(exc, HTTPException):
        return JobError(exc.status_code, str(exc.detail))
    log.error("Job failed", exc_info=exc)
    return JobError(500, INTERNAL_ERROR)


def _accepts_progress(fn: Callable) -> bool:
    """Generators written against the old protocol take only `params`."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "progress" or p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def create_app(settings: Settings | None = None, backends: Backends | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    backends = backends or build_backends(settings)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    nsfw = NsfwClassifier(
        settings.nsfw_model_id if settings.backend != "mock" else "",
        settings.nsfw_threshold,
        backends.device,
    )

    app = FastAPI(title="Image Studio", version=__version__)
    app.state.settings = settings
    app.state.backends = backends
    jobs = JobManager(error_mapper=map_job_error)
    app.state.jobs = jobs

    # ------------------------------------------------------------------ helpers

    def snap(value: int) -> int:
        """Clamp to max_side and round down to a multiple of 8 (diffusers requirement)."""
        return max(256, min(value, settings.max_side)) // 8 * 8

    def save(img: Image.Image, kind: str, prompt: str | None = None) -> str:
        nsfw.check(img)
        img = apply_watermark(img, visible=settings.watermark)
        image_id = uuid.uuid4().hex
        img.save(
            settings.output_dir / f"{image_id}.png",
            format="PNG",
            pnginfo=provenance_metadata(kind, prompt),
        )
        return image_id

    def build_params(req: GenerateRequest) -> GenerateParams:
        """All synchronous validation of a generation request (prompt policy, model, LoRA)."""
        check_prompt(req.prompt, settings.extra_blocked_terms)
        checkpoint = None
        if req.model:
            checkpoint = resolve_model(settings.checkpoints_dir, req.model)
            if checkpoint is None:
                raise HTTPException(400, f"Модель не найдена: {req.model}")
        if len(req.loras) > settings.max_loras:
            raise HTTPException(400, f"Не больше {settings.max_loras} LoRA за раз.")
        loras = []
        for spec in req.loras:
            path = resolve_model(settings.loras_dir, spec.name)
            if path is None:
                raise HTTPException(400, f"LoRA не найдена: {spec.name}")
            loras.append((path, spec.weight))
        steps = min(req.steps or settings.default_steps, settings.max_steps)
        return GenerateParams(
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            width=snap(req.width),
            height=snap(req.height),
            steps=steps,
            guidance=req.guidance,
            seed=req.seed,
            checkpoint=checkpoint,
            loras=tuple(loras),
        )

    def run_generator(params: GenerateParams, progress: ProgressFn) -> tuple[Image.Image, int]:
        generate = backends.generator.generate
        if _accepts_progress(generate):
            return generate(params, progress=progress)
        return generate(params)

    def result(image_id: str, **extra) -> dict:
        return ImageResult(id=image_id, url=f"/api/images/{image_id}", **extra).model_dump()

    def generate_spec(req: GenerateRequest) -> JobSpec:
        params = build_params(req)

        def work(ctx: JobContext) -> dict:
            img, seed = run_generator(params, ctx.scaled(0.0, 0.95))
            ctx.report(STAGE_SAVE, 0.96)
            image_id = save(img, "generate", params.prompt)
            return result(image_id, seed=seed)

        return JobSpec("generate", work)

    def run_blocking(spec: JobSpec) -> ImageResult:
        """Blocking endpoints share the job queue, so GPU work stays serialized for both APIs."""
        job = jobs.submit(spec.kind, spec.fn)
        job.wait()
        if job.status == ERROR and job.error is not None:
            raise HTTPException(job.error.code, job.error.detail)
        return ImageResult.model_validate(job.result)

    def accepted(spec: JobSpec) -> JobAccepted:
        return JobAccepted(job_id=jobs.submit(spec.kind, spec.fn).id)

    def find_job(job_id: str) -> Job:
        job = jobs.get(job_id) if JOB_ID_RE.fullmatch(job_id) else None
        if job is None:
            raise HTTPException(404, "Задача не найдена.")
        return job

    # ------------------------------------------------------------------ errors

    @app.exception_handler(ContentBlocked)
    async def _blocked(_, exc: ContentBlocked):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(BackendUnavailable)
    async def _unavailable(_, exc: BackendUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # ------------------------------------------------------------------ routes

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "backend": backends.name,
            "device": backends.device,
            "model": settings.model_id if backends.name != "mock" else "mock",
            "watermark": settings.watermark,
            "lowvram": settings.lowvram,
            "lowvram_active": getattr(backends.generator, "lowvram_active", None),
            # Architecture of the loaded pipeline ("sdxl" / "sd15"), None until a model is loaded
            "architecture": getattr(backends.generator, "architecture", None),
        }

    @app.get("/api/models")
    def models() -> dict:
        return {
            "default": settings.model_id if backends.name != "mock" else "mock",
            "checkpoints": list_models(settings.checkpoints_dir),
            "loras": list_models(settings.loras_dir),
        }

    # blocking API: waits for the result

    @app.post("/api/generate", response_model=ImageResult)
    def generate(req: GenerateRequest) -> ImageResult:
        return run_blocking(generate_spec(req))

    # background jobs: same inputs and validation, then poll GET /api/jobs/{id}

    @app.post("/api/jobs/generate", response_model=JobAccepted, status_code=202)
    def job_generate(req: GenerateRequest) -> JobAccepted:
        return accepted(generate_spec(req))

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        return jobs.snapshot(find_job(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> dict:
        return jobs.snapshot(jobs.cancel(find_job(job_id).id))

    @app.get("/api/images/{image_id}")
    def get_image(image_id: str) -> FileResponse:
        if not IMAGE_ID_RE.fullmatch(image_id):
            raise HTTPException(404, "Изображение не найдено.")
        path = settings.output_dir / f"{image_id}.png"
        if not path.is_file():
            raise HTTPException(404, "Изображение не найдено.")
        return FileResponse(path, media_type="image/png", filename=f"{image_id}.png")

    @app.delete("/api/images/{image_id}", status_code=204)
    def delete_image(image_id: str) -> None:
        if not IMAGE_ID_RE.fullmatch(image_id):
            raise HTTPException(404, "Изображение не найдено.")
        (settings.output_dir / f"{image_id}.png").unlink(missing_ok=True)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def run() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Image Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    run()
