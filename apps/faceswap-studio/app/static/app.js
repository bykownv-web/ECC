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
        else if (h.architecture === "zimage") parts.push("модель Z-Image Turbo: 8 шагов, CFG 0");
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

  // Architecture badge + per-architecture defaults (steps / CFG / size) of the selected model
  const ARCH_LABELS = { sdxl: "SDXL", sd15: "SD 1.5", zimage: "Z-Image Turbo", flux: "FLUX (не поддерживается)" };
  const modelInfo = new Map(); // select value -> { architecture, defaults }

  function setField(form, name, value) {
    const input = form.elements.namedItem(name);
    if (input && value != null) input.value = String(value);
  }

  function updateNegativePrompt(form) {
    const info = modelInfo.get(form.elements.namedItem("model").value) || {};
    const neg = form.elements.namedItem("negative_prompt");
    const hint = $("#negative-hint", form);
    const zimage = info.architecture === "zimage";
    const cfg = parseFloat(form.elements.namedItem("guidance").value || "0");
    // Z-Image Turbo uses the negative prompt only with classifier-free guidance (CFG > 0)
    neg.disabled = zimage && !(cfg > 0);
    if (hint) hint.hidden = !zimage;
  }

  function applyModel(block, fillDefaults) {
    const form = block.closest("form");
    const sel = $('select[name="model"]', block);
    const badge = $(".arch-badge", block);
    const info = modelInfo.get(sel.value) || {};
    const label = ARCH_LABELS[info.architecture];
    // unknown architecture: worth a warning for a local file, noise for the default model
    badge.hidden = !label && (sel.value === "" || !info.architecture);
    badge.textContent = label || "архитектура не определена";
    badge.classList.toggle("unknown", !label);
    badge.title = label ? `Архитектура модели: ${label}` : "";
    if (fillDefaults && info.defaults) {
      setField(form, "steps", info.defaults.steps);
      setField(form, "guidance", info.defaults.guidance);
      setField(form, "width", info.defaults.width);
      setField(form, "height", info.defaults.height);
    }
    updateNegativePrompt(form);
  }

  fetch("/api/models")
    .then((r) => r.json())
    .then((m) => {
      loraNames = m.loras.map((l) => l.name);
      if (m.default_model) modelInfo.set("", m.default_model);
      m.checkpoints.forEach((c) => modelInfo.set(c.name, c));
      $$(".model-block").forEach((block) => {
        const sel = $('select[name="model"]', block);
        sel.options[0].textContent = `По умолчанию (${m.default})`;
        m.checkpoints.forEach((c) => {
          const label = ARCH_LABELS[c.architecture];
          sel.add(new Option(`${c.name} · ${label ? `${label} · ` : ""}${c.size_mb} МБ`, c.name));
        });
        const form = block.closest("form");
        sel.addEventListener("change", () => applyModel(block, true));
        form.elements.namedItem("guidance").addEventListener("input", () => updateNegativePrompt(form));
        applyModel(block, true);
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
