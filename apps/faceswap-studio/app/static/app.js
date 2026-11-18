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
          if (v === "") continue;
          body[k] = ["width", "height", "steps", "seed"].includes(k) ? parseInt(v, 10)
            : k === "guidance" ? parseFloat(v) : v;
        }
        init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
      } else {
        const fd = new FormData(form);
        for (const [k, v] of Array.from(fd.entries())) if (v === "") fd.delete(k);
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
