(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const els = {
    status: $("#status"),
    empty: $("#result-empty"),
    progress: $("#progress"),
    track: $("#progress-track"),
    bar: $("#progress-bar"),
    stage: $("#progress-stage"),
    percent: $("#progress-percent"),
    cancel: $("#progress-cancel"),
    error: $("#error"),
    result: $("#result"),
    img: $("#result-img"),
    meta: $("#result-meta"),
    download: $("#result-download"),
    history: $("#history"),
  };

  // Health (also re-read after each job: architecture of the loaded model, low-VRAM mode)
  function refreshHealth() {
    return fetch("/api/health", { cache: "no-store" })
      .then((r) => r.json())
      .then((h) => {
        const parts = [`${h.backend} · ${h.device}`];
        if (h.lowvram_active) parts.push("режим экономии видеопамяти");
        if (h.architecture === "sd15") parts.push("модель SD 1.5: лучше 512×512");
        else if (h.architecture === "sdxl") parts.push("модель SDXL: 1024×1024");
        els.status.textContent = parts.join(" · ");
        els.status.className = "status ok";
      })
      .catch(() => {
        els.status.textContent = "Сервер недоступен";
        els.status.className = "status bad";
      });
  }
  refreshHealth();

  // Models / LoRA
  let loraNames = [];
  const MAX_LORAS = 5;

  function addLoraRow(block) {
    const list = $(".loras", block);
    if (!loraNames.length || list.children.length >= MAX_LORAS) return;
    const row = $("#lora-row").content.firstElementChild.cloneNode(true);
    const sel = $("select", row);
    loraNames.forEach((n) => sel.add(new Option(n, n)));
    $(".remove-lora", row).addEventListener("click", () => list.removeChild(row));
    list.append(row);
  }

  fetch("/api/models")
    .then((r) => r.json())
    .then((m) => {
      loraNames = m.loras.map((l) => l.name);
      $$(".model-block").forEach((block) => {
        const sel = $('select[name="model"]', block);
        sel.options[0].textContent = `По умолчанию (${m.default})`;
        m.checkpoints.forEach((c) => sel.add(new Option(`${c.name} · ${c.size_mb} МБ`, c.name)));
        const add = $(".add-lora", block);
        add.disabled = !loraNames.length;
        add.title = loraNames.length ? "" : "Положите .safetensors в models/loras/";
        add.addEventListener("click", () => addLoraRow(block));
      });
    })
    .catch(() => {});

  function collectLoras(form) {
    return $$(".lora-row", form).map((row) => ({
      name: $("select", row).value,
      weight: parseFloat($("input", row).value || "0.8"),
    }));
  }

  function showError(message) {
    els.error.textContent = message;
    els.error.hidden = false;
  }

  function showResult(data) {
    els.img.src = data.url;
    els.download.href = data.url;
    const parts = [];
    if (data.seed != null) parts.push(`seed ${data.seed}`);
    els.meta.textContent = parts.join(" · ");
    els.result.hidden = false;

    const thumb = document.createElement("img");
    thumb.src = data.url;
    thumb.alt = "";
    thumb.addEventListener("click", () => showResult(data));
    els.history.prepend(thumb);
  }

  async function errorText(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join("; ");
    } catch (_) { /* not JSON */ }
    return `Ошибка ${res.status}`;
  }

  // Background jobs: submit -> poll GET /api/jobs/{id} -> show the result
  const POLL_MS = 700;
  let activeJob = null; // { id, cancelled, timer }

  function setBusy(form, busy) {
    $$('form button[type="submit"]').forEach((btn) => { btn.disabled = busy; });
    els.progress.hidden = !busy;
    if (busy) {
      els.empty.hidden = true;
      els.error.hidden = true;
      els.result.hidden = true;
      els.cancel.disabled = true;
      renderProgress({ stage: "Отправка…", progress: 0 });
    }
  }

  function renderProgress(job) {
    const pct = Math.round((job.progress || 0) * 100);
    let stage = job.stage || "";
    if (job.status === "queued" && job.queue_position) stage = `В очереди: позиция ${job.queue_position}`;
    if (job.cancel_requested && job.status === "running") stage = "Отмена…";
    els.stage.textContent = stage;
    els.percent.textContent = `${pct}%`;
    els.bar.style.width = `${pct}%`;
    els.track.setAttribute("aria-valuenow", String(pct));
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitForJob(jobId) {
    for (;;) {
      const res = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await errorText(res));
      const job = await res.json();
      renderProgress(job);
      if (job.status === "done") return job.result;
      if (job.status === "error") {
        const err = new Error(job.error ? job.error.detail : "Ошибка");
        err.cancelled = job.error && job.error.code === 499;
        throw err;
      }
      await sleep(POLL_MS);
    }
  }

  els.cancel.addEventListener("click", async () => {
    if (!activeJob) return;
    els.cancel.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${activeJob}/cancel`, { method: "POST" });
      if (res.ok) renderProgress(await res.json());
    } catch (_) { /* polling reports the final state */ }
  });

  function buildRequest(form) {
    const body = {};
    for (const [k, v] of new FormData(form).entries()) {
      if (v === "" || k.startsWith("lora_")) continue;
      body[k] = ["width", "height", "steps", "seed"].includes(k) ? parseInt(v, 10)
        : k === "guidance" ? parseFloat(v) : v;
    }
    body.loras = collectLoras(form);
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  async function submit(form, url) {
    if (activeJob) return;
    setBusy(form, true);
    try {
      const res = await fetch(url, buildRequest(form));
      if (!res.ok) throw new Error(await errorText(res));
      const { job_id: jobId } = await res.json();
      activeJob = jobId;
      els.cancel.disabled = false;
      showResult(await waitForJob(jobId));
      refreshHealth();
    } catch (err) {
      showError(err.cancelled ? "Задача отменена." : (err.message || String(err)));
    } finally {
      activeJob = null;
      setBusy(form, false);
    }
  }

  $("#form-generate").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/api/jobs/generate"); });
})();
