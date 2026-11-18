(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const els = {
    status: $("#status"),
    empty: $("#result-empty"),
    spinner: $("#spinner"),
    error: $("#error"),
    result: $("#result"),
    img: $("#result-img"),
    meta: $("#result-meta"),
    download: $("#result-download"),
    history: $("#history"),
  };

  // Tabs
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === btn.dataset.tab));
    });
  });

  // File previews
  $$('input[type="file"]').forEach((input) => {
    input.addEventListener("change", () => {
      const preview = input.parentElement.querySelector(".preview");
      const file = input.files && input.files[0];
      if (!preview) return;
      if (preview.src) URL.revokeObjectURL(preview.src);
      if (file) preview.src = URL.createObjectURL(file);
      else preview.removeAttribute("src");
    });
  });

  // Health
  fetch("/api/health")
    .then((r) => r.json())
    .then((h) => {
      const swap = h.swap_model_present ? "" : " · модель подмены лица не найдена";
      els.status.textContent = `${h.backend} · ${h.device}${swap}`;
      els.status.className = "status " + (h.swap_model_present ? "ok" : "bad");
    })
    .catch(() => {
      els.status.textContent = "Сервер недоступен";
      els.status.className = "status bad";
    });

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

  function setBusy(form, busy) {
    const btn = $('button[type="submit"]', form);
    btn.disabled = busy;
    els.spinner.hidden = !busy;
    if (busy) {
      els.empty.hidden = true;
      els.error.hidden = true;
      els.result.hidden = true;
    }
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
    if (data.faces_swapped != null) parts.push(`лиц заменено: ${data.faces_swapped}`);
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

  async function submit(form, url, asJson) {
    setBusy(form, true);
    try {
      let init;
      if (asJson) {
        const fd = new FormData(form);
        const body = {};
        for (const [k, v] of fd.entries()) {
          if (v === "" || k.startsWith("lora_")) continue;
          body[k] = ["width", "height", "steps", "seed"].includes(k) ? parseInt(v, 10)
            : k === "guidance" ? parseFloat(v) : v;
        }
        body.loras = collectLoras(form);
        init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
      } else {
        const fd = new FormData(form);
        for (const [k, v] of Array.from(fd.entries())) if (v === "" || k.startsWith("lora_")) fd.delete(k);
        if ($(".model-block", form)) fd.set("loras", JSON.stringify(collectLoras(form)));
        $$('input[type="checkbox"]', form).forEach((cb) => fd.set(cb.name, cb.checked ? "true" : "false"));
        init = { method: "POST", body: fd };
      }
      const res = await fetch(url, init);
      if (!res.ok) throw new Error(await errorText(res));
      showResult(await res.json());
    } catch (err) {
      showError(err.message || String(err));
    } finally {
      setBusy(form, false);
    }
  }

  $("#form-generate").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/api/generate", true); });
  $("#form-swap").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/api/swap", false); });
  $("#form-combo").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/api/generate-swap", false); });
})();
