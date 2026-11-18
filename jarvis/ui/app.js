/* Интерфейс J.A.R.V.I.S. Все данные выводятся через textContent — без innerHTML. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    tags: [],
    items: [],
    analytics: null,
    config: null,
  };

  // ---------- утилиты ----------
  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.cls) node.className = opts.cls;
      if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
      if (opts.title) node.title = opts.title;
    }
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  function money(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' ₽';
  }

  function parseRuDate(s) {
    const m = /(\d{2})\.(\d{2})\.(\d{4})/.exec(s || '');
    return m ? new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1])) : null;
  }

  function daysLeft(s) {
    const d = parseRuDate(s);
    if (!d) return null;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return Math.round((d - today) / 86400000);
  }

  let toastTimer = null;
  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 4500);
  }

  function api() {
    return window.pywebview && window.pywebview.api;
  }

  // ---------- часы ----------
  function tickClock() {
    const d = new Date();
    $('clock').textContent = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ---------- вкладки ----------
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  function switchTab(name) {
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-body').forEach((b) => b.classList.toggle('active', b.id === 'tab-' + name));
  }

  // ---------- диалог ----------
  function addMessage(kind, text) {
    const box = $('messages');
    const who = { user: 'Вы', assistant: (state.config && state.config.assistant_name) || 'Джарвис' }[kind];
    const msg = el('div', { cls: 'msg ' + kind });
    if (who) msg.appendChild(el('span', { cls: 'msg-who', text: who }));
    msg.appendChild(document.createTextNode(text));
    box.appendChild(msg);
    box.scrollTop = box.scrollHeight;
  }

  function setMode(mode) {
    $('reactor').dataset.mode = mode;
    $('state-label').textContent = { idle: 'Ожидание', listening: 'Слушаю', thinking: 'Обработка' }[mode] || mode;
    if (mode === 'idle') $('tool-label').textContent = '';
    $('mic-btn').disabled = mode !== 'idle';
  }

  $('chat-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('chat-input');
    const text = input.value.trim();
    if (!text || !api()) return;
    addMessage('user', text);
    input.value = '';
    api().send_message(text);
  });

  $('reset-btn').addEventListener('click', () => {
    if (!api()) return;
    api().reset_chat();
    $('messages').textContent = '';
    addMessage('system', 'Начат новый диалог.');
  });

  $('mic-btn').addEventListener('click', () => api() && api().listen_once());
  $('stop-btn').addEventListener('click', () => api() && api().stop_speaking());

  $('wake-toggle').addEventListener('change', async (e) => {
    if (!api()) return;
    const res = await api().set_wake_listening(e.target.checked);
    toast(res.text);
    if (!res.ok) e.target.checked = false;
  });

  // ---------- теги ----------
  function renderTags() {
    const box = $('tags');
    box.textContent = '';
    state.tags.forEach((tag, i) => {
      const remove = el('button', { text: '×', title: 'Удалить' });
      remove.type = 'button';
      remove.addEventListener('click', () => {
        state.tags.splice(i, 1);
        renderTags();
      });
      box.appendChild(el('span', { cls: 'tag' }, [document.createTextNode(tag), remove]));
    });
  }

  function addTag() {
    const input = $('tag-input');
    input.value.split(',').map((s) => s.trim()).filter(Boolean).forEach((t) => {
      if (!state.tags.includes(t)) state.tags.push(t);
    });
    input.value = '';
    renderTags();
  }
  $('tag-add').addEventListener('click', addTag);
  $('tag-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTag();
    }
  });

  // ---------- закупки ----------
  function procParams() {
    const num = (id) => {
      const v = $(id).value.trim();
      return v === '' ? null : Number(v);
    };
    return {
      tags: state.tags.slice(),
      fz44: $('fz44').checked,
      fz223: $('fz223').checked,
      only_active: $('only-active').checked,
      price_from: num('price-from'),
      price_to: num('price-to'),
    };
  }

  $('proc-search').addEventListener('click', () => {
    if (!api()) return;
    if (!state.tags.length) {
      toast('Добавьте хотя бы один тег.');
      return;
    }
    if (!$('fz44').checked && !$('fz223').checked) {
      toast('Выберите 44-ФЗ или 223-ФЗ.');
      return;
    }
    setProcStatus('Запрос к ЕИС…');
    api().search_procurements(procParams());
  });

  $('proc-export').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().export_procurements();
    toast(res.text);
  });

  $('monitor-toggle').addEventListener('change', async (e) => {
    if (!api()) return;
    if (e.target.checked) await api().save_settings({ procurement: procParams() });
    const res = await api().set_monitoring(e.target.checked);
    toast(res.text);
  });

  $('proc-filter').addEventListener('input', renderTable);
  $('proc-sort').addEventListener('change', renderTable);

  function setProcStatus(text, isError) {
    const s = $('proc-status');
    s.textContent = text || '';
    s.classList.toggle('error', !!isError);
  }

  function sortedItems() {
    const q = $('proc-filter').value.trim().toLowerCase();
    let items = state.items.filter((p) =>
      !q || [p.title, p.customer, p.number, (p.tags || []).join(' ')].join(' ').toLowerCase().includes(q));
    const mode = $('proc-sort').value;
    items = items.slice().sort((a, b) => {
      if (mode === 'deadline') {
        const da = parseRuDate(a.deadline), db = parseRuDate(b.deadline);
        return (da ? da.getTime() : Infinity) - (db ? db.getTime() : Infinity);
      }
      if (mode === 'updated') {
        const da = parseRuDate(a.updated || a.published), db = parseRuDate(b.updated || b.published);
        return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
      }
      return (b.price || 0) - (a.price || 0);
    });
    return items;
  }

  function renderTable() {
    const body = $('proc-body');
    body.textContent = '';
    const items = sortedItems();
    $('proc-empty').style.display = items.length ? 'none' : 'block';
    items.forEach((p) => {
      const link = el('a', { text: '№ ' + p.number, title: 'Открыть в ЕИС' });
      link.addEventListener('click', () => api() && api().open_link(p.url));
      const left = daysLeft(p.deadline);
      const deadline = el('td', { cls: left !== null && left <= 3 && left >= 0 ? 'soon' : '', text: p.deadline || '—' });
      if (left !== null && left >= 0) deadline.appendChild(el('div', { cls: 'small', text: 'осталось дней: ' + left }));
      const tr = el('tr', null, [
        el('td', null, [link, el('div', null, [el('span', { cls: 'law', text: p.law || '—' })]),
          el('div', { cls: 'small', text: p.method || '' })]),
        el('td', null, [document.createTextNode(p.title || '—'),
          el('div', { cls: 'small', text: (p.tags || []).join(', ') })]),
        el('td', { text: p.customer || '—' }),
        el('td', { cls: 'num', text: money(p.price) }),
        deadline,
      ]);
      body.appendChild(tr);
    });
  }

  function renderAnalytics(a) {
    $('a-count').textContent = a ? a.count : '—';
    $('a-sum').textContent = a ? money(a.total_sum) : '—';
    $('a-avg').textContent = a ? money(a.average_price) : '—';
    $('a-law').textContent = a ? (a.by_law['44-ФЗ'] || 0) + ' / ' + (a.by_law['223-ФЗ'] || 0) : '—';
    $('a-soon').textContent = a ? a.deadline_soon.length : '—';
    const top = $('top-customers');
    top.textContent = '';
    (a ? a.top_customers : []).forEach((c) => {
      top.appendChild(el('li', null, [document.createTextNode(c.customer + ' — '), el('span', { text: money(c.sum) }),
        document.createTextNode(' (' + c.count + ')')]));
    });
    const tags = $('by-tag');
    tags.textContent = '';
    Object.entries(a ? a.by_tag : {}).forEach(([tag, v]) => {
      tags.appendChild(el('li', null, [document.createTextNode(tag + ': ' + v.count + ' шт., '), el('span', { text: money(v.sum) })]));
    });
  }

  // ---------- настройки ----------
  function fillSettings(cfg) {
    $('assistant-name').value = cfg.assistant_name || '';
    $('user-name').value = cfg.user_name || '';
    $('wake-word').value = cfg.wake_word || '';
    $('city').value = cfg.city || '';
    $('voice-enabled').checked = !!cfg.voice_enabled;
    $('model').value = cfg.model || 'claude-opus-5-5';
    $('effort').value = cfg.effort || 'low';
    $('apps').value = Object.entries(cfg.apps || {}).map(([k, v]) => k + ' = ' + v).join('\n');
    const p = cfg.procurement || {};
    $('pages').value = p.pages_per_tag || 1;
    $('interval').value = p.monitor_interval_min || 30;
    $('ca-bundle').value = p.ca_bundle || '';
    state.tags = (p.tags || []).slice();
    $('fz44').checked = p.fz44 !== false;
    $('fz223').checked = p.fz223 !== false;
    $('only-active').checked = p.only_active !== false;
    $('price-from').value = p.price_from || '';
    $('price-to').value = p.price_to || '';
    renderTags();
  }

  function applyState(st) {
    state.config = st.config;
    fillSettings(st.config);
    const ai = $('ai-pill');
    ai.textContent = st.has_key ? 'ИИ: Claude' : 'ИИ: простой режим';
    ai.className = 'pill ' + (st.has_key ? 'ok' : 'off');
    const voice = $('voice-pill');
    voice.textContent = st.voice_available ? 'Голос: готов' : 'Голос: нет';
    voice.className = 'pill ' + (st.voice_available ? 'ok' : 'off');
    voice.title = st.voice_error || '';
    $('key-status').textContent = st.has_key ? 'Ключ сохранён.' : 'Ключ не задан — работают только простые команды.';
  }

  $('save-key').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().save_api_key($('api-key').value);
    toast(res.text);
    if (res.ok) {
      $('api-key').value = '';
      applyState(await api().get_state());
    }
  });

  $('settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!api()) return;
    const apps = {};
    $('apps').value.split('\n').forEach((line) => {
      const i = line.indexOf('=');
      if (i > 0) {
        const k = line.slice(0, i).trim();
        const v = line.slice(i + 1).trim();
        if (k && v) apps[k] = v;
      }
    });
    const st = await api().save_settings({
      assistant_name: $('assistant-name').value.trim() || 'Джарвис',
      user_name: $('user-name').value.trim() || 'сэр',
      wake_word: $('wake-word').value.trim().toLowerCase() || 'джарвис',
      city: $('city').value.trim() || 'Москва',
      voice_enabled: $('voice-enabled').checked,
      model: $('model').value,
      effort: $('effort').value,
      apps: apps,
      procurement: {
        pages_per_tag: Math.max(1, Math.min(10, Number($('pages').value) || 1)),
        monitor_interval_min: Math.max(10, Number($('interval').value) || 30),
        ca_bundle: $('ca-bundle').value.trim(),
      },
    });
    applyState(st);
    $('settings-status').textContent = 'Сохранено.';
    setTimeout(() => ($('settings-status').textContent = ''), 3000);
  });

  // ---------- события из Python ----------
  window.jarvis = {
    onEvent(msg) {
      const p = msg.payload || {};
      switch (msg.event) {
        case 'state':
          setMode(p.mode);
          break;
        case 'tool':
          $('tool-label').textContent = '› ' + p.text;
          break;
        case 'user':
          addMessage('user', p.text);
          break;
        case 'assistant':
          addMessage('assistant', p.text);
          break;
        case 'error':
          addMessage('error', p.text);
          break;
        case 'reminder':
          addMessage('assistant', '⏰ Напоминание: ' + p.text);
          toast('⏰ ' + p.text);
          break;
        case 'procurement_status':
          setProcStatus(p.text);
          $('proc-search').disabled = !!p.busy;
          break;
        case 'procurement_results':
          state.items = p.items || [];
          state.analytics = p.analytics;
          renderAnalytics(p.analytics);
          renderTable();
          if (p.errors && p.errors.length) setProcStatus(p.errors.join(' • '), true);
          else setProcStatus('Обновлено: ' + new Date().toLocaleTimeString('ru-RU') + ', найдено ' + state.items.length);
          break;
        case 'monitor_new':
          toast(p.text);
          addMessage('assistant', '🔔 ' + p.text);
          break;
        default:
          break;
      }
    },
  };

  window.addEventListener('pywebviewready', async () => {
    const st = await api().get_state();
    applyState(st);
    addMessage('assistant', st.greeting);
    setMode('idle');
  });
})();
