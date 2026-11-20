/* Вкладка «Агенты»: карточки агентов Джарвиса и живой журнал. Подключается после app.js.
   Данные приходят событием «agents» ({agents, log, seq, now}) и при открытии через api().agents_state(). */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const ui = { filter: 'all', follow: true, lastSeq: -1, data: null, clockSkew: 0 };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  const STATUS = { idle: 'ждёт', working: 'работает', done: 'готово', error: 'ошибка' };

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function elapsed(a) {
    if (a.status !== 'working' || !a.started) return '';
    const sec = Math.max(0, Math.floor(Date.now() / 1000 - ui.clockSkew - a.started));
    return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }

  function renderCards(agents) {
    const box = $('agents-grid');
    if (!box) return;
    box.textContent = '';
    agents.forEach((a) => {
      const card = el('article', 'agent-card is-' + a.status);
      card.dataset.agent = a.id;
      if (ui.filter === a.id) card.classList.add('is-selected');
      const head = el('header', 'agent-head');
      head.appendChild(el('span', 'agent-icon', a.icon));
      const t = el('div', 'agent-title');
      t.appendChild(el('div', 'agent-name', a.name));
      t.appendChild(el('div', 'agent-role', a.role));
      head.appendChild(t);
      head.appendChild(el('span', 'agent-status', STATUS[a.status] || a.status));
      card.appendChild(head);
      card.appendChild(el('div', 'agent-task', a.task || a.last_result || '—'));
      const bar = el('div', 'agent-bar');
      const fill = el('i');
      if (a.status === 'working' && (a.progress === null || a.progress === undefined)) bar.classList.add('indeterminate');
      else fill.style.width = Math.round((a.progress || 0) * 100) + '%';
      bar.appendChild(fill);
      card.appendChild(bar);
      const meta = el('div', 'agent-meta');
      meta.appendChild(el('span', 'agent-timer', elapsed(a)));
      meta.appendChild(el('span', null, 'запусков: ' + (a.runs || 0) + (a.errors ? ' · ошибок: ' + a.errors : '')));
      card.appendChild(meta);
      card.addEventListener('click', () => {
        ui.filter = ui.filter === a.id ? 'all' : a.id;
        const sel = $('agents-filter');
        if (sel) sel.value = ui.filter;
        render(ui.data, true);
      });
      box.appendChild(card);
    });
  }

  function renderLog(log, agents, force) {
    const box = $('agents-log');
    if (!box) return;
    const names = {};
    agents.forEach((a) => (names[a.id] = a));
    const rows = log.filter((e) => ui.filter === 'all' || e.agent === ui.filter).slice(-400);
    box.textContent = '';
    rows.forEach((e) => {
      const line = el('div', 'log-line lvl-' + e.level);
      line.appendChild(el('span', 'log-ts', fmtTime(e.ts)));
      const who = names[e.agent] || { icon: '•', name: e.agent };
      line.appendChild(el('span', 'log-agent', who.icon + ' ' + who.name));
      line.appendChild(el('span', 'log-text', e.text));
      box.appendChild(line);
    });
    if (ui.follow || force) box.scrollTop = box.scrollHeight;
    const working = agents.filter((a) => a.status === 'working').length;
    const sum = $('agents-summary');
    if (sum) sum.textContent = working ? 'Работают агентов: ' + working : 'Все агенты ждут задач';
  }

  function render(data, force) {
    if (!data || !Array.isArray(data.agents)) return;
    ui.data = data;
    if (typeof data.now === 'number') ui.clockSkew = Date.now() / 1000 - data.now;
    renderCards(data.agents);
    if (force || data.seq !== ui.lastSeq) renderLog(data.log || [], data.agents, force);
    ui.lastSeq = data.seq;
  }

  function setup() {
    const sel = $('agents-filter');
    if (sel && !sel.dataset.ready) {
      sel.dataset.ready = '1';
      sel.addEventListener('change', () => {
        ui.filter = sel.value;
        render(ui.data, true);
      });
    }
    const log = $('agents-log');
    if (log && !log.dataset.ready) {
      log.dataset.ready = '1';
      log.addEventListener('scroll', () => {
        ui.follow = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
      });
    }
    const clear = $('agents-clear-filter');
    if (clear && !clear.dataset.ready) {
      clear.dataset.ready = '1';
      clear.addEventListener('click', () => {
        ui.filter = 'all';
        if (sel) sel.value = 'all';
        render(ui.data, true);
      });
    }
  }

  // Перехватываем события: «agents» — наше, остальное отдаём app.js.
  function hook() {
    if (!window.jarvis || window.jarvis.__agentsHooked) return;
    const orig = window.jarvis.onEvent.bind(window.jarvis);
    window.jarvis.onEvent = function (msg) {
      if (msg && msg.event === 'agents') {
        try { render(msg.payload); } catch (e) { /* журнал не должен ломать окно */ }
        return;
      }
      orig(msg);
    };
    window.jarvis.__agentsHooked = true;
  }

  hook();
  setup();
  // Таймеры «работает 00:12» обновляем раз в секунду без запросов к Python.
  setInterval(() => {
    if (ui.data && ui.data.agents.some((a) => a.status === 'working')) renderCards(ui.data.agents);
  }, 1000);

  window.addEventListener('pywebviewready', async () => {
    hook();
    setup();
    const api = window.pywebview && window.pywebview.api;
    if (api && api.agents_state) {
      try { render(await api.agents_state(), true); } catch (e) { /* нет агентов — не страшно */ }
    }
  });
})();
