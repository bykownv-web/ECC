"""Background jobs: JobManager unit tests and the /api/jobs/* endpoints with the mock backend."""

import io
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.backends import BackendUnavailable
from app.config import Settings
from app.jobs import CANCELLED_CODE, DONE, ERROR, JobCancelled, JobError, JobManager
from app.main import create_app

TIMEOUT = 10.0


@pytest.fixture
def app(tmp_path):
    return create_app(replace(Settings(), backend="mock", output_dir=tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


def poll(client, job_id, until=("done", "error"), stage=None):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in until and (stage is None or body["stage"] == stage):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {until}: {body}")


class Gate:
    """A generator that blocks until released, reporting progress (and so honouring cancel)."""

    def __init__(self, original):
        self.original = original
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, params, progress=None):
        self.started.set()
        while not self.release.wait(0.01):
            if progress is not None:
                progress("Генерация: шаг 1/2", 0.5)
        return self.original(params, progress=progress)


# ---------------------------------------------------------------- JobManager


def test_manager_runs_jobs_in_order_and_reports_progress():
    manager = JobManager()
    seen = []

    def work(name):
        def fn(ctx):
            ctx.report(f"stage {name}", 0.5)
            seen.append(name)
            return {"name": name}

        return fn

    jobs = [manager.submit("t", work(n)) for n in "abc"]
    for job in jobs:
        assert job.wait(TIMEOUT)
    assert seen == ["a", "b", "c"]
    snap = manager.snapshot(jobs[0])
    assert snap["status"] == DONE and snap["progress"] == 1.0 and snap["result"] == {"name": "a"}
    assert snap["finished"] >= snap["started"] >= snap["created"]


def test_manager_maps_errors_and_keeps_last_jobs():
    manager = JobManager(error_mapper=lambda exc: JobError(418, str(exc)), max_jobs=3)

    def boom(_ctx):
        raise RuntimeError("nope")

    first = manager.submit("t", boom)
    assert first.wait(TIMEOUT)
    assert manager.snapshot(first)["error"] == {"code": 418, "detail": "nope"}
    later = [manager.submit("t", lambda _ctx: {}) for _ in range(4)]
    for job in later:
        job.wait(TIMEOUT)
    assert manager.get(first.id) is None
    assert all(manager.get(j.id) for j in later[-3:])


def test_manager_scaled_progress_is_monotonic():
    manager = JobManager()
    values = []

    def fn(ctx):
        sub = ctx.scaled(0.5, 1.0)
        ctx.report("a", 0.4)
        sub("b", 0.0)  # maps to 0.5
        values.append(manager.snapshot(job)["progress"])
        sub("c", 0.5)  # maps to 0.75
        values.append(manager.snapshot(job)["progress"])
        ctx.report("d", 0.1)  # never goes backwards
        values.append(manager.snapshot(job)["progress"])
        return {}

    job = manager.submit("t", fn)
    job.wait(TIMEOUT)
    assert values == [0.5, 0.75, 0.75]


def test_manager_cancel_running_job():
    manager = JobManager()
    started = threading.Event()

    def fn(ctx):
        started.set()
        while True:
            ctx.report("loop", 0.1)
            time.sleep(0.005)

    job = manager.submit("t", fn)
    assert started.wait(TIMEOUT)
    manager.cancel(job.id)
    assert job.wait(TIMEOUT)
    snap = manager.snapshot(job)
    assert snap["status"] == ERROR and snap["error"]["code"] == CANCELLED_CODE
    assert snap["cancel_requested"] is True


# ---------------------------------------------------------------- API lifecycle


def test_generate_job_lifecycle(client):
    res = client.post(
        "/api/jobs/generate", json={"prompt": "a red fox", "width": 256, "height": 256, "seed": 5}
    )
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    body = poll(client, job_id)
    assert body["status"] == "done", body
    assert body["kind"] == "generate"
    assert body["progress"] == 1.0
    assert body["error"] is None
    assert body["result"]["seed"] == 5
    img = Image.open(io.BytesIO(client.get(body["result"]["url"]).content))
    # safety intact on the job path: provenance metadata is written
    assert img.info["ai_generated"] == "true"
    assert img.info["prompt"] == "a red fox"


def test_generate_job_reports_steps(app, client):
    stages = []
    original = app.state.backends.generator.generate

    def spy(params, progress=None):
        def wrapped(stage, fraction):
            stages.append((stage, fraction))
            progress(stage, fraction)

        return original(params, progress=wrapped)

    app.state.backends.generator.generate = spy
    job_id = client.post(
        "/api/jobs/generate", json={"prompt": "x", "width": 256, "height": 256, "steps": 3}
    ).json()["job_id"]
    assert poll(client, job_id)["status"] == "done"
    assert [s for s, _ in stages] == ["Генерация: шаг 1/3", "Генерация: шаг 2/3", "Генерация: шаг 3/3"]
    assert stages[-1][1] == 1.0


@pytest.mark.parametrize("prompt", ["nude woman on beach", "голая девушка"])
def test_generate_job_blocked_prompt_rejected_synchronously(app, client, prompt):
    res = client.post("/api/jobs/generate", json={"prompt": prompt})
    assert res.status_code == 422
    assert not app.state.jobs._jobs  # nothing was queued


def test_generate_job_bad_model_and_validation(client):
    assert (
        client.post("/api/jobs/generate", json={"prompt": "x", "model": "../x.safetensors"}).status_code
        == 400
    )
    assert client.post("/api/jobs/generate", json={"prompt": ""}).status_code == 422


@pytest.mark.parametrize(
    "exc, code, detail",
    [
        (BackendUnavailable("нет модели"), 503, "нет модели"),
        (RuntimeError("secret internals"), 500, None),
    ],
)
def test_job_error_mapping(app, client, exc, code, detail):
    def broken(*_a, **_k):
        raise exc

    app.state.backends.generator.generate = broken
    job_id = client.post("/api/jobs/generate", json={"prompt": "x"}).json()["job_id"]
    body = poll(client, job_id)
    assert body["error"]["code"] == code
    if detail:
        assert body["error"]["detail"] == detail
    else:
        assert "secret" not in body["error"]["detail"]
    # the blocking endpoint maps the same way
    res = client.post("/api/generate", json={"prompt": "x"})
    assert res.status_code == code


def test_cancel_running_job(app, client):
    gate = Gate(app.state.backends.generator.generate)
    app.state.backends.generator.generate = gate
    job_id = client.post("/api/jobs/generate", json={"prompt": "x", "width": 256, "height": 256}).json()[
        "job_id"
    ]
    assert gate.started.wait(TIMEOUT)
    running = poll(client, job_id, until=("running",), stage="Генерация: шаг 1/2")
    assert 0 < running["progress"] < 1
    res = client.post(f"/api/jobs/{job_id}/cancel")
    assert res.status_code == 200
    assert res.json()["cancel_requested"] is True
    body = poll(client, job_id)
    assert body["status"] == "error"
    assert body["error"]["code"] == CANCELLED_CODE
    assert body["result"] is None


def test_queue_position_and_cancel_queued(app, client):
    gate = Gate(app.state.backends.generator.generate)
    app.state.backends.generator.generate = gate
    req = {"prompt": "x", "width": 256, "height": 256}
    first = client.post("/api/jobs/generate", json=req).json()["job_id"]
    assert gate.started.wait(TIMEOUT)
    second = client.post("/api/jobs/generate", json=req).json()["job_id"]
    third = client.post("/api/jobs/generate", json=req).json()["job_id"]

    running = client.get(f"/api/jobs/{first}").json()
    assert running["status"] == "running" and running["queue_position"] is None
    b, c = (client.get(f"/api/jobs/{j}").json() for j in (second, third))
    assert (b["status"], b["queue_position"], b["stage"]) == ("queued", 1, "В очереди")
    assert c["queue_position"] == 2

    cancelled = client.post(f"/api/jobs/{second}/cancel").json()
    assert cancelled["status"] == "error" and cancelled["error"]["code"] == CANCELLED_CODE
    assert client.get(f"/api/jobs/{third}").json()["queue_position"] == 1

    gate.release.set()
    assert poll(client, first)["status"] == "done"
    assert poll(client, third)["status"] == "done"


def test_unknown_job_404(client):
    assert client.get("/api/jobs/" + "a" * 32).status_code == 404
    assert client.get("/api/jobs/../etc").status_code == 404
    assert client.post("/api/jobs/" + "b" * 32 + "/cancel").status_code == 404


def test_cancel_finished_job_is_noop(client):
    job_id = client.post("/api/jobs/generate", json={"prompt": "x", "width": 256, "height": 256}).json()[
        "job_id"
    ]
    assert poll(client, job_id)["status"] == "done"
    body = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert body["status"] == "done" and body["result"] is not None


def test_mock_generator_cancel_via_progress():
    from app.backends import GenerateParams, MockGenerator

    def progress(_stage, _fraction):
        raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        MockGenerator().generate(GenerateParams(prompt="x", width=64, height=64), progress=progress)


def test_health_reports_lowvram_and_architecture(client):
    body = client.get("/api/health").json()
    assert body["lowvram"] == "auto"
    assert "lowvram_active" in body
    assert body["architecture"] is None
