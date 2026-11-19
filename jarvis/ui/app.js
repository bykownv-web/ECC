/* Интерфейс J.A.R.V.I.S. Все данные выводятся через textContent — без innerHTML. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const TABS = Array.from(document.querySelectorAll('.tab[data-tab]')).map((b) => b.dataset.tab);
  const state = {
    tags: [],
    items: [],
    analytics: null,
    config: null,
  };
  // Объявлены заранее: используются обработчиками событий и pollStats до разделов, где они описаны.
  const tender = { state: null, busy: false };
  const neural = { analyst: { value: 0, state: 'idle', label: '' } };
  const dirty = { analyst: false, email: false };

  // ---------- утилиты ----------
  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.cls) node.className = opts.cls;
      if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
      if (opts.title) node.title = String(opts.title);
    }
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  const str = (v) => (v === null || v === undefined ? '' : String(v));
  const arr = (v) => (Array.isArray(v) ? v : []);
  // Список строк: массив, одиночная строка или ничего.
  const list = (v) => (Array.isArray(v) ? v.filter((x) => x !== null && x !== undefined && x !== '') : (v ? [v] : []));
  const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});

  // Число из числа или строки вида «1 200 000,50»; null — если числа нет.
  function toNum(v) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    if (typeof v === 'number') return isFinite(v) ? v : null;
    const s = String(v).replace(/[\s ₽]/g, '').replace(',', '.');
    if (!/^-?\d+(\.\d+)?$/.test(s)) return null;
    return Number(s);
  }

  function money(v) {
    const n = toNum(v);
    if (n === null) return '—';
    return n.toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' ₽';
  }

  function intText(v) {
    const n = toNum(v);
    return n === null ? '' : n.toLocaleString('ru-RU', { maximumFractionDigits: 0 });
  }

  function pct(v) {
    const n = toNum(v);
    return n === null ? '—' : n.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
  }

  function range(a, b) {
    if (toNum(a) === null && toNum(b) === null) return '—';
    return money(a) + ' – ' + money(b);
  }

  function marginText(t) {
    if (toNum(t.margin_min) === null && toNum(t.margin_max) === null) return '—';
    const p = toNum(t.margin_pct_min) !== null || toNum(t.margin_pct_max) !== null ? ' (' + pct(t.margin_pct_min) + '…' + pct(t.margin_pct_max) + '%)' : '';
    return range(t.margin_min, t.margin_max) + p;
  }

  function parseRuDate(s) {
    const m = /(\d{2})\.(\d{2})\.(\d{4})/.exec(str(s));
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
    if (!text) return;
    const t = $('toast');
    t.textContent = String(text);
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 4500);
  }

  function api() {
    return window.pywebview && window.pywebview.api;
  }

  // Вызов Python: всегда возвращает Promise; исключение Python превращается в null и сообщение.
  async function invoke(quiet, name, args) {
    const a = api();
    if (!a || typeof a[name] !== 'function') return null;
    try {
      const res = await a[name](...args);
      return res === undefined ? null : res;
    } catch (e) {
      if (!quiet) toast('Ошибка: ' + ((e && e.message) || e || name));
      return null;
    }
  }
  const call = (name, ...args) => invoke(false, name, args);
  const callQuiet = (name, ...args) => invoke(true, name, args); // телеметрия: без сообщений каждые 3 с

  // Кнопка блокируется на время запроса, чтобы не отправить его дважды.
  async function withButton(btn, fn) {
    if (btn.disabled) return undefined;
    btn.disabled = true;
    try {
      return await fn();
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- часы ----------
  const startedAt = Date.now();
  function tickClock() {
    const d = new Date();
    $('clock').textContent = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    $('date-label').textContent = d.toLocaleDateString('ru-RU', { weekday: 'short', day: '2-digit', month: 'long', year: 'numeric' }).toUpperCase();
    const mins = Math.floor((Date.now() - startedAt) / 60000);
    $('uptime-label').textContent = 'UPTIME ' + String(Math.floor(mins / 60)).padStart(2, '0') + ':' + String(mins % 60).padStart(2, '0');
  }

  function setBar(name, value) {
    const bar = $('sys-' + name + '-bar');
    const label = $('sys-' + name);
    const n = toNum(value);
    if (n === null) {
      label.textContent = '—';
      bar.style.width = '0%';
      return;
    }
    bar.style.width = Math.max(0, Math.min(100, n)) + '%';
    bar.classList.toggle('hot', n >= 85);
    label.textContent = Math.round(n) + '%';
  }

  async function pollStats() {
    const st = await callQuiet('get_system_stats');
    if (st) {
      setBar('cpu', st.cpu);
      setBar('ram', st.ram);
      setBar('disk', st.disk);
    }
    // Статус Telegram обновляется только текстами статуса — поля ввода не трогаются.
    if (document.body.dataset.tab === 'settings') {
      const tg = await callQuiet('telegram_status');
      if (tg) renderIntegrations({ telegram: tg });
    }
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ---------- вкладки ----------
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  function switchTab(name) {
    if (!TABS.includes(name)) return;
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-body').forEach((b) => b.classList.toggle('active', b.id === 'tab-' + name));
    document.body.dataset.tab = name;
    updateNeural();
  }

  // ---------- нейросеть вместо реактора на вкладках «Заявка» и «Аналитика» ----------
  // Контракт с ui/neural.js: window.JarvisNeural.setVisible(bool) и
  // window.JarvisNeural.setProgress(value 0..1 или null — неизвестно, { state: 'idle'|'working'|'done'|'error', label }).
  function tenderProgress() {
    const st = obj(tender.state);
    const steps = obj(st.steps);
    const keys = ['files', 'analysis', 'match', 'estimate', 'docs'];
    let sum = 0;
    let running = '';
    let error = '';
    keys.forEach((k) => {
      const s = obj(steps[k]).status;
      if (s === 'done' || s === 'skipped') sum += 1;
      else if (s === 'running') {
        sum += 0.5;
        running = k;
      } else if (s === 'error') error = k;
    });
    const titles = { files: 'Документы', analysis: 'Разбор ТЗ', match: 'Подбор', estimate: 'Стоимость', docs: 'Документы заявки' };
    const value = sum / keys.length;
    if (running) return { value, state: 'working', label: titles[running] };
    if (error) return { value, state: 'error', label: titles[error] };
    if (value >= 1) return { value: 1, state: 'done', label: 'Готово' };
    if (tender.busy) return { value: value || null, state: 'working', label: 'Работаю' };
    return { value, state: 'idle', label: st.purchase ? 'Ожидание' : 'Нет закупки' };
  }

  function updateNeural() {
    const n = window.JarvisNeural;
    const tab = document.body.dataset.tab || 'chat';
    const on = tab === 'tender' || tab === 'analyst';
    if (!n) return;
    try {
      n.setVisible(on);
      if (!on) return;
      const p = tab === 'tender' ? tenderProgress() : neural.analyst;
      n.setProgress(p.value, { state: p.state, label: p.label });
    } catch (e) {
      /* декоративный виджет не должен ломать интерфейс */
    }
  }

  function analystProgressFrom(text, busy, isError) {
    const t = str(text);
    const m = /(\d+)\s+из\s+(\d+)/.exec(t);
    const a = neural.analyst;
    const done = /^Готово/.test(t);
    if (isError) Object.assign(a, { state: 'error', label: 'Ошибка' });
    else if (!busy) Object.assign(a, { value: done ? 1 : a.value, state: done ? 'done' : 'idle', label: done ? 'Готово' : '' });
    else if (m) Object.assign(a, { value: Math.min(0.98, Number(m[1]) / Math.max(1, Number(m[2]))), state: 'working', label: t.split('…')[0] });
    else Object.assign(a, { value: a.state === 'working' ? a.value : 0.05, state: 'working', label: t.split('…')[0] });
  }

  // ---------- диалог ----------
  function addMessage(kind, text) {
    if (text === null || text === undefined || text === '') return;
    const box = $('messages');
    const who = { user: 'Вы', assistant: (state.config && state.config.assistant_name) || 'Джарвис' }[kind];
    const msg = el('div', { cls: 'msg ' + kind });
    if (who) msg.appendChild(el('span', { cls: 'msg-who', text: who }));
    msg.appendChild(document.createTextNode(String(text)));
    box.appendChild(msg);
    box.scrollTop = box.scrollHeight;
  }

  const MODES = { idle: 'Ожидание', listening: 'Слушаю', thinking: 'Обработка' };
  function setMode(mode) {
    if (!MODES[mode]) return;
    $('reactor').dataset.mode = mode;
    $('state-label').textContent = MODES[mode];
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
    call('send_message', text);
  });

  $('reset-btn').addEventListener('click', () => {
    if (!api()) return;
    call('reset_chat');
    $('messages').textContent = '';
    addMessage('system', 'Начат новый диалог.');
  });

  $('mic-btn').addEventListener('click', () => call('listen_once'));
  $('stop-btn').addEventListener('click', () => call('stop_speaking'));

  $('wake-toggle').addEventListener('change', async (e) => {
    const box = e.target;
    if (!api()) {
      box.checked = false;
      return;
    }
    const res = await call('set_wake_listening', box.checked);
    if (res) toast(res.text);
    if (!res || res.ok === false) box.checked = false;
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
      if (!state.tags.some((x) => x.toLowerCase() === t.toLowerCase())) state.tags.push(t);
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
      return v === '' || isNaN(Number(v)) ? null : Number(v);
    };
    return {
      tags: state.tags.slice(),
      fz44: $('fz44').checked,
      fz223: $('fz223').checked,
      only_active: $('only-active').checked,
      price_from: num('price-from'),
      price_to: num('price-to'),
      strict: $('strict').checked,
      minus_words: $('minus-words').value.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean),
    };
  }

  $('proc-search').addEventListener('click', () => {
    if (!api()) return;
    if ($('tag-input').value.trim()) addTag(); // введённый, но не добавленный тег
    if (!state.tags.length) {
      toast('Добавьте хотя бы один тег.');
      return;
    }
    if (!$('fz44').checked && !$('fz223').checked) {
      toast('Выберите 44-ФЗ или 223-ФЗ.');
      return;
    }
    setProcStatus('Запрос к ЕИС…');
    call('search_procurements', procParams());
  });

  $('proc-export').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    const res = await call('export_procurements');
    if (res) toast(res.text);
  }));

  $('monitor-toggle').addEventListener('change', async (e) => {
    const box = e.target;
    if (!api()) {
      box.checked = false;
      return;
    }
    if (box.checked) {
      if (!state.tags.length) {
        toast('Добавьте хотя бы один тег.');
        box.checked = false;
        return;
      }
      await call('save_settings', { procurement: procParams() });
    }
    const res = await call('set_monitoring', box.checked);
    if (res) toast(res.text);
    if (!res || res.ok === false) box.checked = false;
  });

  $('proc-filter').addEventListener('input', renderTable);
  $('proc-sort').addEventListener('change', renderTable);

  function setProcStatus(text, isError) {
    const s = $('proc-status');
    s.textContent = str(text);
    s.classList.toggle('error', !!isError);
  }

  function sortedItems() {
    const q = $('proc-filter').value.trim().toLowerCase();
    let items = state.items.filter((p) =>
      !q || [p.title, p.customer, p.number, list(p.tags).join(' ')].map(str).join(' ').toLowerCase().includes(q));
    const mode = $('proc-sort').value;
    items = items.slice().sort((a, b) => {
      if (mode === 'deadline') {
        const da = parseRuDate(a.deadline), db = parseRuDate(b.deadline);
        return (da ? da.getTime() : Infinity) - (db ? db.getTime() : Infinity) || 0;
      }
      if (mode === 'updated') {
        const da = parseRuDate(a.updated || a.published), db = parseRuDate(b.updated || b.published);
        return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
      }
      return (toNum(b.price) || 0) - (toNum(a.price) || 0);
    });
    return items;
  }

  function renderTable() {
    const body = $('proc-body');
    body.textContent = '';
    const items = sortedItems();
    $('proc-empty').style.display = items.length ? 'none' : 'block';
    items.forEach((p) => {
      const link = el('a', { text: '№ ' + (str(p.number) || '—'), title: 'Открыть в ЕИС' });
      link.addEventListener('click', (e) => {
        e.stopPropagation();
        call('open_link', str(p.url));
      });
      const left = daysLeft(p.deadline);
      const deadline = el('td', { cls: left !== null && left <= 3 && left >= 0 ? 'soon' : '', text: str(p.deadline) || '—' });
      if (left !== null && left >= 0) deadline.appendChild(el('div', { cls: 'small', text: 'осталось дней: ' + left }));
      const work = el('button', { cls: 'btn btn-small', text: 'В работу', title: 'Подготовить заявку по этой закупке' });
      work.type = 'button';
      work.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!api()) return;
        withButton(work, async () => {
          const res = await call('tender_set_purchase', p, true);
          if (!res || res.ok === false) toast((res && res.text) || 'Не удалось взять закупку в работу.');
          else switchTab('tender');
        });
      });
      const tags = list(p.tags).join(', ');
      const tr = el('tr', { cls: 'row-link', title: 'Открыть закупку на zakupki.gov.ru' }, [
        el('td', null, [link, el('div', null, [el('span', { cls: 'law', text: str(p.law) || '—' })]),
          el('div', { cls: 'small', text: str(p.method) })]),
        el('td', null, [el('div', { cls: 'clamp', text: str(p.title) || '—', title: str(p.title) }),
          el('div', { cls: 'small', text: tags })]),
        el('td', null, [el('div', { cls: 'clamp clamp-3', text: str(p.customer) || '—', title: str(p.customer) })]),
        el('td', { cls: 'num', text: money(p.price) }),
        deadline,
        el('td', { cls: 'row-actions' }, [work]),
      ]);
      tr.addEventListener('click', (e) => {
        if (e.target.closest('button, a')) return;
        if (window.getSelection && String(window.getSelection())) return; // выделяют текст — не переходим
        call('open_link', str(p.url));
      });
      body.appendChild(tr);
    });
  }

  function renderAnalytics(raw) {
    const a = raw && typeof raw === 'object' ? raw : null;
    const byLaw = obj(a && a.by_law);
    $('a-count').textContent = a && a.count !== undefined && a.count !== null ? String(a.count) : '—';
    $('a-sum').textContent = a ? money(a.total_sum) : '—';
    $('a-avg').textContent = a ? money(a.average_price) : '—';
    $('a-law').textContent = a ? (byLaw['44-ФЗ'] || 0) + ' / ' + (byLaw['223-ФЗ'] || 0) : '—';
    $('a-soon').textContent = a ? String(arr(a.deadline_soon).length) : '—';
    const top = $('top-customers');
    top.textContent = '';
    arr(a && a.top_customers).forEach((c) => {
      const cc = obj(c);
      top.appendChild(el('li', { title: str(cc.customer) }, [
        el('div', { cls: 'clamp clamp-2', text: str(cc.customer) || '—' }),
        el('span', { text: money(cc.sum) }),
        document.createTextNode(' · закупок: ' + (toNum(cc.count) || 0)),
      ]));
    });
    const tags = $('by-tag');
    tags.textContent = '';
    Object.entries(obj(a && a.by_tag)).forEach(([tag, v]) => {
      const vv = obj(v);
      tags.appendChild(el('li', null, [document.createTextNode(tag + ': ' + (toNum(vv.count) || 0) + ' шт., '), el('span', { text: money(vv.sum) })]));
    });
  }


  // ---------- заявка ----------
  function kv(label, value) {
    return el('div', { cls: 'fact' }, [el('div', { cls: 'fact-label', text: label }), el('div', { cls: 'fact-value', text: str(value) || '—' })]);
  }

  function estimateTable(equipment) {
    const body = el('tbody');
    equipment.forEach((raw) => {
      const it = obj(raw);
      body.appendChild(el('tr', null, [
        el('td', null, [document.createTextNode(str(it.name)), el('div', { cls: 'small', text: str(it.key_specs) })]),
        el('td', { text: (str(it.quantity) + ' ' + str(it.unit)).trim() }),
        el('td', { cls: 'num', text: range(it.total_min, it.total_max) }),
        el('td', { cls: 'small', text: str(it.price_basis) }),
      ]));
    });
    return el('table', { cls: 'table tender-table an-table' }, [
      el('thead', null, [el('tr', null, [el('th', { text: 'Оборудование' }), el('th', { text: 'Кол-во' }), el('th', { text: 'Стоимость' }), el('th', { text: 'Основа цены' })])]),
      body,
    ]);
  }

  function risksBlock(risks) {
    const ul = el('ul', { cls: 'req-list' });
    risks.forEach((x) => ul.appendChild(el('li', { text: str(x) })));
    return el('div', { cls: 'tender-list' }, [el('h3', { text: 'Риски' }), ul]);
  }

  function renderTender(raw) {
    const st = obj(raw);
    tender.state = st;
    const box = $('tender-purchase');
    box.textContent = '';
    const p = st.purchase && typeof st.purchase === 'object' ? st.purchase : null;
    box.classList.toggle('empty-state', !p);
    if (!p) {
      box.textContent = 'Нажмите «В работу» у закупки на вкладке «Закупки» или введите номер.';
    } else {
      const link = el('a', { text: '№ ' + (str(p.number) || '—'), title: 'Открыть на zakupki.gov.ru' });
      link.addEventListener('click', () => call('open_link', str(p.url)));
      box.appendChild(el('div', { cls: 'purchase-head' }, [link, el('span', { cls: 'law', text: str(p.law) || '—' })]));
      box.appendChild(el('div', { cls: 'purchase-title', text: str(p.title) }));
      box.appendChild(el('div', { cls: 'purchase-meta' }, [
        kv('Заказчик', p.customer), kv('НМЦК', money(p.price)), kv('Подача до', p.deadline),
      ]));
    }

    const files = $('tender-files');
    files.textContent = '';
    arr(st.files).forEach((raw2) => {
      const f = obj(raw2);
      const status = str(f.status);
      const kind = str(f.kind);
      const ok = kind ? (kind === 'ok' || kind === 'skipped') : /извлечён|архив/.test(status);
      const chars = toNum(f.chars) ? ' · ' + intText(f.chars) + ' симв.' : '';
      files.appendChild(el('li', { cls: kind === 'warning' ? 'warn' : ok ? 'ok' : 'bad' }, [
        el('span', { cls: 'file-name', text: str(f.name) || '—' }), el('span', { cls: 'file-status', text: (status || '—') + chars }),
      ]));
    });
    $('tender-text-info').textContent = toNum(st.text_chars) ? 'Текст ТЗ: ' + intText(st.text_chars) + ' символов' : 'Документы не загружены.';

    const a = st.analysis && typeof st.analysis === 'object' ? st.analysis : null;
    const positions = arr(a && a.positions);
    const facts = $('tender-facts');
    facts.textContent = '';
    const lists = $('tender-lists');
    lists.textContent = '';
    const posBody = $('tender-positions');
    posBody.textContent = '';
    $('tender-positions-table').style.display = positions.length ? '' : 'none';
    if (a) {
      [['Кратко', a.summary], ['Срок поставки', a.delivery_terms], ['Место поставки', a.delivery_place],
        ['Монтаж и пусконаладка', a.installation], ['Оплата', a.payment_terms], ['Приёмка', a.acceptance_terms],
        ['Срок контракта', a.contract_term], ['Штрафы и пени', a.penalties], ['Гарантия', a.warranty],
        ['Обеспечение заявки', a.bid_security], ['Обеспечение контракта', a.contract_security]]
        .forEach(([k, v]) => facts.appendChild(kv(k, v)));
      [['Ограничения', a.restrictions], ['Требования к участнику', a.participant_requirements], ['Документы в заявке', a.documents_required]]
        .forEach(([title, items]) => {
          const ul = el('ul', { cls: 'list' });
          const xs = list(items);
          (xs.length ? xs : ['не найдено']).forEach((t) => ul.appendChild(el('li', { text: str(t) })));
          lists.appendChild(el('div', { cls: 'tender-list' }, [el('h3', { text: title }), ul]));
        });
      positions.forEach((raw2, i) => {
        const pos = obj(raw2);
        const reqs = el('ul', { cls: 'req-list' });
        arr(pos.characteristics).forEach((c) => {
          const cc = obj(c);
          reqs.appendChild(el('li', { text: (str(cc.name) || '—') + ': ' + (str(cc.requirement) || '—') }));
        });
        posBody.appendChild(el('tr', null, [
          el('td', { text: String(i + 1) }), el('td', { text: str(pos.name) || '—' }),
          el('td', { text: (str(pos.quantity) + ' ' + str(pos.unit)).trim() || '—' }), el('td', null, [reqs]),
        ]));
      });
    }

    $('tender-catalog').textContent = toNum(st.catalog_count)
      ? 'Каталог поставщика: ' + st.catalog_count + ' позиций — подбор только из него.'
      : 'Каталог не загружен — ИИ предложит модели сам (обязательно проверьте).';
    const mBody = $('tender-matches');
    mBody.textContent = '';
    const matches = arr(st.matches);
    $('tender-matches-table').style.display = matches.length ? '' : 'none';
    matches.forEach((raw2) => {
      const m = obj(raw2);
      const compliance = str(m.compliance);
      const cls = { 'соответствует': 'badge ok', 'частично': 'badge warn', 'не найдено': 'badge bad' }[compliance] || 'badge';
      const price = toNum(m.price) !== null ? money(m.price) : (str(m.price) || '—');
      mBody.appendChild(el('tr', null, [
        el('td', null, [document.createTextNode(str(m.position) || '—'), el('div', { cls: 'small', text: (str(m.quantity) + ' ' + str(m.unit)).trim() })]),
        el('td', null, [el('div', { text: [m.brand, m.model].map(str).filter(Boolean).join(' ') || '—' }),
          el('div', { cls: 'small', text: str(m.country) + (m.from_catalog ? ' · из каталога' : ' · предложение ИИ') })]),
        el('td', null, [el('span', { cls: cls, text: compliance || '—' })]),
        el('td', { cls: 'num', text: price }),
        el('td', { cls: 'small', text: str(m.notes) }),
      ]));
    });

    const est = $('tender-estimate');
    est.textContent = '';
    const e = st.estimate && typeof st.estimate === 'object' ? st.estimate : null;
    const t = obj(st.totals);
    est.classList.toggle('empty-state', !e);
    if (!e) {
      est.textContent = 'Появится после разбора ТЗ.';
    } else {
      est.appendChild(el('div', { cls: 'facts an-facts' }, [
        kv('НМЦК', money(p && p.price)),
        kv('Оборудование', range(t.equipment_min, t.equipment_max)),
        kv('Монтаж', e.installation_required ? range(t.installation_min, t.installation_max) : 'не требуется'),
        kv('Себестоимость', range(t.cost_min, t.cost_max)),
        kv('Маржа', marginText(t)),
        kv('Рекомендация', e.recommendation),
      ]));
      const equipment = arr(e.equipment);
      if (equipment.length) est.appendChild(estimateTable(equipment));
      if (e.installation_required && e.installation_scope) est.appendChild(kv('Работы по монтажу', e.installation_scope));
      const risks = list(e.risks);
      if (risks.length) est.appendChild(risksBlock(risks));
      if (e.reasoning) est.appendChild(el('div', { cls: 'an-reasoning', text: str(e.reasoning) }));
      est.appendChild(el('div', { cls: 'small', text: 'Цены ориентировочные — проверьте у поставщиков.' }));
    }

    const steps = obj(st.steps);
    [['files', 'step-files'], ['analysis', 'step-analysis'], ['match', 'step-match'], ['estimate', 'step-estimate'], ['docs', 'step-docs']]
      .forEach(([key, id]) => {
        const section = $(id);
        const info = obj(steps[key]);
        const status = ['running', 'done', 'error', 'skipped'].includes(info.status) ? info.status : '';
        ['is-running', 'is-done', 'is-error', 'is-skipped'].forEach((c) => section.classList.remove(c));
        if (status) section.classList.add('is-' + status);
        let note = section.querySelector('.step-note');
        if (!note) {
          note = el('span', { cls: 'step-note' });
          section.querySelector('.step-head').appendChild(note);
        }
        const label = { running: '◌ выполняется', done: '✓ готово', error: '✕ ошибка', skipped: '— пропущено' }[status] || '';
        note.textContent = label + (info.note ? ': ' + str(info.note) : '');
        note.title = str(info.note);
      });

    const docs = $('tender-docs');
    docs.textContent = '';
    arr(st.documents).filter((x) => typeof x === 'string' && x).forEach((path) => {
      const name = path.split(/[\\/]/).pop();
      const btn = el('button', { cls: 'btn btn-small', text: 'Открыть' });
      btn.type = 'button';
      btn.addEventListener('click', async () => {
        const res = await call('tender_open', path);
        if (res && res.ok === false) toast(res.text);
      });
      docs.appendChild(el('li', { cls: 'ok' }, [el('span', { cls: 'file-name', text: '▤ ' + name }), btn]));
    });

    updateTenderButtons();
  }

  // Доступность кнопок вычисляется из состояния каждый раз заново — ничего не «залипает» после занятости.
  function updateTenderButtons() {
    const st = obj(tender.state);
    const hasP = !!(st.purchase && typeof st.purchase === 'object');
    const a = st.analysis && typeof st.analysis === 'object' ? st.analysis : null;
    const busy = tender.busy;
    const rules = {
      'tender-find': true,
      'tender-catalog-import': true,
      'tender-run-all': hasP,
      'tender-fetch': hasP,
      'tender-pick': hasP,
      'tender-clear': hasP,
      'tender-analyze': hasP,
      'tender-open-folder': hasP,
      'tender-match': !!(a && arr(a.positions).length),
      'tender-build': !!a,
    };
    Object.entries(rules).forEach(([id, ok]) => ($(id).disabled = busy || !ok));
    document.querySelectorAll('#tender-docs button').forEach((b) => (b.disabled = busy));
  }

  // Секундомер: между шагами конвейера Python на миг шлёт busy=false — останавливаемся с задержкой,
  // чтобы время не сбрасывалось на каждом шаге.
  let timerStart = 0;
  let timerEnd = 0;
  let timerId = null;
  let timerStopId = null;
  function tickTimer() {
    const sec = Math.max(0, Math.floor(((timerEnd || Date.now()) - timerStart) / 1000));
    $('tender-timer').textContent = '⏱ ' + String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }
  function setTimer(busy) {
    if (busy) {
      if (timerStopId) {
        clearTimeout(timerStopId);
        timerStopId = null;
        timerEnd = 0;
      }
      if (!timerId) {
        timerStart = Date.now();
        timerEnd = 0;
        tickTimer();
        timerId = setInterval(tickTimer, 1000);
      }
    } else if (timerId && !timerStopId) {
      const end = Date.now();
      timerStopId = setTimeout(() => {
        clearInterval(timerId);
        timerId = null;
        timerStopId = null;
        timerEnd = end;
        tickTimer();
      }, 1500);
    }
  }

  function setTenderStatus(text, busy, isError) {
    tender.busy = !!busy;
    setTimer(!!busy);
    const s = $('tender-status');
    s.textContent = str(text);
    s.classList.toggle('error', !!isError);
    s.classList.toggle('busy', !!busy);
    updateTenderButtons();
  }

  $('tender-find').addEventListener('click', () => {
    const v = $('tender-number').value.trim();
    if (!v) {
      toast('Введите номер закупки или ссылку.');
      return;
    }
    call('tender_find', v);
  });
  $('tender-number').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('tender-find').click();
    }
  });
  $('tender-fetch').addEventListener('click', () => call('tender_fetch_docs'));
  $('tender-run-all').addEventListener('click', () => call('tender_run_all'));
  $('tender-pick').addEventListener('click', () => call('tender_pick_files'));
  $('tender-clear').addEventListener('click', () => call('tender_clear_files'));
  $('tender-analyze').addEventListener('click', () => call('tender_analyze'));
  $('tender-match').addEventListener('click', () => call('tender_match'));
  $('tender-build').addEventListener('click', () => call('tender_build_docs'));
  $('tender-catalog-import').addEventListener('click', () => call('tender_import_catalog'));
  $('tender-open-folder').addEventListener('click', async () => {
    const res = await call('tender_open', '');
    if (res && res.ok === false) toast(res.text);
  });


  // ---------- аналитика ----------
  function rub(v) {
    return money(v);
  }

  function numOr(v, def) {
    const n = toNum(v);
    return n === null ? def : n;
  }

  ['an-profile', 'an-enabled', 'an-min', 'an-top'].forEach((id) => {
    $(id).addEventListener('input', () => (dirty.analyst = true));
    $(id).addEventListener('change', () => (dirty.analyst = true));
  });

  function renderAnalyst(raw) {
    if (!raw || typeof raw !== 'object') return;
    const st = raw;
    // Настройки заполняются, только если пользователь их не правит (иначе события прогресса затирают ввод).
    if (st.settings && typeof st.settings === 'object' && !dirty.analyst) {
      const set = st.settings;
      $('an-profile').value = str(set.profile);
      $('an-enabled').checked = !!set.enabled;
      $('an-min').value = numOr(set.min_score, 60);
      $('an-top').value = numOr(set.top_n, 3);
    }

    const scores = $('an-scores');
    scores.textContent = '';
    arr(st.scores).forEach((raw2) => {
      const sc = obj(raw2);
      const score = toNum(sc.score);
      const cls = score >= 80 ? 'badge ok' : score >= 60 ? 'badge warn' : 'badge';
      const li = el('li', { title: str(sc.reason) }, [
        el('span', { cls: cls, text: score === null ? '—' : String(score) }),
        el('div', { cls: 'clamp clamp-2', text: str(sc.title) || str(sc.number) || '—' }),
        el('div', { cls: 'small', text: str(sc.reason) }),
      ]);
      li.addEventListener('click', () => call('open_link', str(sc.url)));
      li.classList.add('row-link');
      scores.appendChild(li);
    });

    const box = $('an-reports');
    box.textContent = '';
    const reports = arr(st.reports).filter((r) => r && typeof r === 'object');
    if (!reports.length) {
      box.appendChild(el('div', { cls: 'empty', text: 'Отчётов пока нет. Заполните профиль и нажмите «Проанализировать сейчас».' }));
      return;
    }
    reports.forEach((r) => {
      const p = obj(r.purchase);
      const e = obj(r.estimate);
      const t = obj(r.totals);
      const rec = str(e.recommendation);
      const recCls = { 'участвовать': 'badge ok', 'подумать': 'badge warn', 'не участвовать': 'badge bad' }[rec] || 'badge';
      const link = el('a', { text: '№ ' + (str(p.number) || '—'), title: 'Открыть на zakupki.gov.ru' });
      link.addEventListener('click', () => call('open_link', str(p.url)));
      const toTender = el('button', { cls: 'btn btn-small', text: 'В работу' });
      toTender.type = 'button';
      toTender.disabled = !p.number;
      toTender.addEventListener('click', () => withButton(toTender, async () => {
        const res = await call('analyst_to_tender', str(p.number));
        if (!res || res.ok === false) toast((res && res.text) || 'Не удалось взять закупку в работу.');
        else switchTab('tender');
      }));
      const equipment = arr(e.equipment);
      const hasEstimate = !!(r.estimate && typeof r.estimate === 'object');
      const meta = [str(p.law), str(p.customer).slice(0, 90)].filter(Boolean).join(' · ');
      const card = el('article', { cls: 'tender-step an-card' }, [
        el('header', { cls: 'an-card-head' }, [
          el('span', { cls: 'an-score', text: toNum(r.score) === null ? '—' : String(toNum(r.score)) }),
          el('div', { cls: 'an-title' }, [el('div', { cls: 'clamp clamp-2', text: str(p.title) || '—', title: str(p.title) }),
            el('div', { cls: 'small' }, [link, document.createTextNode(meta ? ' · ' + meta : '')])]),
          rec ? el('span', { cls: recCls, text: rec }) : null,
          toTender,
        ]),
        el('div', { cls: 'facts an-facts' }, [
          kv('НМЦК', rub(p.price)), kv('Подача до', p.deadline),
          kv('Оборудование', range(t.equipment_min, t.equipment_max)),
          kv('Монтаж', e.installation_required ? range(t.installation_min, t.installation_max) : (hasEstimate ? 'не требуется' : '—')),
          kv('Себестоимость', range(t.cost_min, t.cost_max)),
          kv('Маржа', marginText(t)),
        ]),
      ]);
      if (r.score_reason) card.appendChild(el('div', { cls: 'small', text: 'Почему подходит: ' + str(r.score_reason) }));
      if (r.error) card.appendChild(el('div', { cls: 'proc-status error', text: 'Глубокий разбор не удался: ' + str(r.error) }));
      if (equipment.length) card.appendChild(estimateTable(equipment));
      if (e.installation_required && e.installation_scope) card.appendChild(kv('Работы по монтажу', e.installation_scope));
      if (e.other_costs) card.appendChild(kv('Прочие расходы', e.other_costs));
      const risks = list(e.risks);
      if (risks.length) card.appendChild(risksBlock(risks));
      if (e.reasoning) card.appendChild(el('div', { cls: 'an-reasoning', text: str(e.reasoning) }));
      if (hasEstimate) {
        card.appendChild(el('div', { cls: 'small', text: 'Цены ориентировочные: ' + (equipment.some((x) => obj(x).price_basis === 'каталог') ? 'частично из вашего каталога.' : 'оценка ИИ, проверьте у поставщиков.') }));
      }
      box.appendChild(card);
    });
  }

  function analystValues() {
    return {
      enabled: $('an-enabled').checked,
      profile: $('an-profile').value.trim(),
      min_score: Math.max(0, Math.min(100, numOr($('an-min').value, 60))),
      top_n: Math.max(1, Math.min(10, numOr($('an-top').value, 3))),
    };
  }
  async function saveAnalyst() {
    const res = await call('analyst_save', analystValues());
    if (res && res.ok !== false) dirty.analyst = false;
    return res;
  }
  $('an-save').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    const res = await saveAnalyst();
    if (res) toast(res.text);
    renderAnalyst(await call('analyst_state'));
  }));
  $('an-run').addEventListener('click', async () => {
    if (!api()) return;
    const btn = $('an-run');
    btn.disabled = true; // снимет событие analyst_status с busy=false
    const res = await saveAnalyst();
    if (!res || res.ok === false) {
      btn.disabled = false;
      if (res) toast(res.text);
      return;
    }
    const s = $('an-status');
    s.textContent = 'Запускаю аналитику…';
    s.classList.remove('error');
    s.classList.add('busy');
    try {
      await api().analyst_run_now();
    } catch (err) {
      btn.disabled = false;
      s.classList.remove('busy');
      s.textContent = '';
      toast('Ошибка: ' + ((err && err.message) || err));
    }
  });

  // ---------- Telegram и почта ----------
  function renderIntegrations(raw) {
    const st = obj(raw);
    if (st.telegram && typeof st.telegram === 'object') {
      const tg = st.telegram;
      const bot = tg.bot ? ' (@' + str(tg.bot) + ')' : '';
      $('tg-status').textContent = !tg.enabled ? 'Не подключён.'
        : tg.paired ? 'Подключён' + bot + ', чат привязан. Бот отвечает только вам.'
          : 'Бот подключён' + bot + ', ждёт привязки чата.';
      if (tg.enabled && tg.code && !tg.paired) $('tg-code').textContent = 'Код привязки: ' + str(tg.code) + '  (или отправьте боту /start ' + str(tg.code) + ')';
      else $('tg-code').textContent = '';
      $('tg-open').style.display = tg.enabled && !tg.paired ? '' : 'none';
      $('tg-health').textContent = !tg.enabled ? ''
        : tg.last_error ? 'Связь с Telegram: ошибка — ' + str(tg.last_error)
          : tg.last_ok ? 'Связь с Telegram: работает (последняя проверка ' + str(tg.last_ok) + ')'
            : tg.running ? 'Связь с Telegram: подключаюсь…' : 'Связь с Telegram: не запущена';
    }
    if (st.email && typeof st.email === 'object' && Object.keys(st.email).length) {
      const em = st.email;
      $('em-status').textContent = em.configured ? 'Почта настроена.' : (em.has_password ? 'Пароль сохранён.' : 'Пароль не задан.');
    }
  }

  async function refreshStatus() {
    const st = await call('get_state');
    if (st) applyStatus(st);
  }

  $('tg-connect').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    const token = $('tg-token').value.trim();
    if (!token) {
      toast('Вставьте токен бота от @BotFather.');
      return;
    }
    const res = await call('telegram_connect', token);
    if (!res) return;
    toast(res.text);
    await refreshStatus();
    if (res.ok) {
      $('tg-token').value = '';
      if (!$('tg-code').textContent) $('tg-code').textContent = str(res.text);
    }
  }));
  $('tg-open').addEventListener('click', async () => {
    const res = await call('telegram_open_bot');
    if (res) toast(res.text);
  });
  $('tg-test').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    const res = await call('telegram_test');
    if (res) toast(res.text);
  }));
  $('tg-disconnect').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    const res = await call('telegram_disconnect');
    if (res) toast(res.text);
    await refreshStatus();
  }));

  function updateEmailUi() {
    $('em-custom').style.display = $('em-preset').value === 'custom' ? 'flex' : 'none';
  }
  $('em-preset').addEventListener('change', updateEmailUi);
  ['em-enabled', 'em-preset', 'em-host', 'em-port', 'em-login', 'em-to', 'em-password'].forEach((id) => {
    $(id).addEventListener('input', () => (dirty.email = true));
    $(id).addEventListener('change', () => (dirty.email = true));
  });
  function emailValues() {
    return {
      enabled: $('em-enabled').checked, preset: $('em-preset').value, host: $('em-host').value.trim(),
      port: numOr($('em-port').value, 465), login: $('em-login').value.trim(), to: $('em-to').value.trim(),
      password: $('em-password').value,
    };
  }
  async function saveEmail() {
    const res = await call('email_save', emailValues());
    if (res && res.ok) {
      $('em-password').value = '';
      dirty.email = false;
    }
    return res;
  }
  $('em-save').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    const res = await saveEmail();
    if (res) toast(res.text);
    await refreshStatus();
  }));
  $('em-test').addEventListener('click', async () => {
    if (!api()) return;
    if (dirty.email) await saveEmail();
    toast('Отправляю тестовое письмо…');
    call('email_test');
  });
  $('make-shortcut').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    const res = await call('create_shortcut');
    if (res) toast(res.text);
  }));

  // ---------- настройки ----------
  // Значение select: если такого варианта нет — добавляем его (addUnknown) или берём запасное.
  function setSelect(id, value, fallback, addUnknown) {
    const sel = $(id);
    const v = str(value) || fallback;
    const known = Array.from(sel.options).some((o) => o.value === v);
    if (!known && addUnknown && v) sel.appendChild(el('option', { text: v })).value = v;
    sel.value = known || addUnknown ? v : fallback;
  }

  // Поля вкладки «Закупки» заполняются только при запуске: дальше ими управляет пользователь,
  // и сохранение настроек не должно сбрасывать несохранённые теги и минус-слова.
  function fillProcTab(cfg, defaults) {
    const p = obj(cfg.procurement);
    state.tags = list(p.tags).map(String);
    $('fz44').checked = p.fz44 !== false;
    $('fz223').checked = p.fz223 !== false;
    $('only-active').checked = p.only_active !== false;
    $('strict').checked = p.strict !== false;
    $('minus-words').value = (Array.isArray(p.minus_words) ? p.minus_words : list(defaults)).join(', ');
    $('price-from').value = toNum(p.price_from) === null ? '' : toNum(p.price_from);
    $('price-to').value = toNum(p.price_to) === null ? '' : toNum(p.price_to);
    renderTags();
  }

  function fillSettings(cfg) {
    $('assistant-name').value = str(cfg.assistant_name);
    $('user-name').value = str(cfg.user_name);
    $('wake-word').value = str(cfg.wake_word);
    $('city').value = str(cfg.city);
    $('voice-enabled').checked = !!cfg.voice_enabled;
    setSelect('tts-voice', cfg.tts_voice, 'ru-RU-DmitryNeural');
    setSelect('tts-pitch', cfg.tts_pitch, '-8Hz');
    setSelect('tts-rate', cfg.tts_rate, '+0%');
    const eid = str(cfg.eleven_voice_id) || 'onwK4e9ZLuTAKqWW03F9';
    const known = Array.from($('eleven-voice').options).some((o) => o.value === eid && o.value !== 'custom');
    $('eleven-voice').value = known ? eid : 'custom';
    $('eleven-custom').value = known ? '' : eid;
    updateVoiceUi();
    setSelect('ai-provider', cfg.ai_provider, 'ollama');
    $('ollama-model').value = str(cfg.ollama_model) || 'qwen2.5:7b';
    setSelect('oa-preset', cfg.openai_preset, 'hermes_openrouter');
    $('oa-url').value = str(cfg.openai_base_url);
    $('oa-model').value = str(cfg.openai_model);
    fillPresetIfEmpty();
    setSelect('gc-model', cfg.gigachat_model, 'GigaChat-2', true);
    setSelect('gc-scope', cfg.gigachat_scope, 'GIGACHAT_API_PERS');
    updateAiUi();
    setSelect('model', cfg.model, 'claude-opus-5-5', true);
    setSelect('effort', cfg.effort, 'low');
    $('apps').value = Object.entries(obj(cfg.apps)).map(([k, v]) => k + ' = ' + v).join('\n');
    const em = obj(cfg.email);
    $('em-enabled').checked = !!em.enabled;
    setSelect('em-preset', em.preset, 'yandex');
    $('em-host').value = str(em.host);
    $('em-port').value = em.port || 465;
    $('em-login').value = str(em.login);
    $('em-to').value = str(em.to);
    $('em-password').value = '';
    dirty.email = false;
    updateEmailUi();
    const co = obj(cfg.company);
    [['co-name', 'name'], ['co-inn', 'inn'], ['co-kpp', 'kpp'], ['co-ogrn', 'ogrn'], ['co-address', 'address'],
      ['co-director', 'director'], ['co-director-position', 'director_position'], ['co-phone', 'phone'], ['co-email', 'email']]
      .forEach(([id, key]) => ($(id).value = str(co[key])));
    const p = obj(cfg.procurement);
    $('pages').value = p.pages_per_tag || 1;
    $('interval').value = p.monitor_interval_min || 30;
    $('ca-bundle').value = str(p.ca_bundle);
  }

  const OA_PRESETS = {
    hermes_openrouter: ['https://openrouter.ai/api/v1', 'nousresearch/hermes-4-70b'],
    hermes_nous: ['https://inference-api.nousresearch.com/v1', 'Hermes-4-70B'],
    yandex: ['https://llm.api.cloud.yandex.net/v1', 'gpt://<ID_каталога>/yandexgpt/latest'],
    deepseek: ['https://api.deepseek.com/v1', 'deepseek-chat'],
    openrouter: ['https://openrouter.ai/api/v1', 'deepseek/deepseek-chat-v3-0324:free'],
    groq: ['https://api.groq.com/openai/v1', 'llama-3.3-70b-versatile'],
    lmstudio: ['http://127.0.0.1:1234/v1', 'local-model'],
  };
  function fillPresetIfEmpty() {
    const pr = OA_PRESETS[$('oa-preset').value];
    if (!pr) return;
    if (!$('oa-url').value.trim()) $('oa-url').value = pr[0];
    if (!$('oa-model').value.trim()) $('oa-model').value = pr[1];
  }
  $('oa-preset').addEventListener('change', () => {
    const pr = OA_PRESETS[$('oa-preset').value];
    if (pr) {
      $('oa-url').value = pr[0];
      $('oa-model').value = pr[1];
    }
  });

  function aiSettings() {
    return {
      ai_provider: $('ai-provider').value,
      ollama_model: $('ollama-model').value.trim() || 'qwen2.5:7b',
      openai_preset: $('oa-preset').value,
      openai_base_url: $('oa-url').value.trim(),
      openai_model: $('oa-model').value.trim(),
      gigachat_model: $('gc-model').value,
      gigachat_scope: $('gc-scope').value,
      model: $('model').value,
      effort: $('effort').value,
    };
  }

  // Сохранение ключа сначала сохраняет выбранного провайдера и его параметры:
  // иначе обновление после сохранения возвращало старого провайдера и прятало блок с ключом.
  async function saveAiKey(provider, inputId, btn) {
    if (!api()) return;
    await withButton(btn, async () => {
      const key = $(inputId).value.trim();
      await call('save_settings', aiSettings());
      const res = await call('save_ai_key', provider, key);
      if (res) toast(res.text);
      if (res && res.ok) $(inputId).value = '';
      await refreshStatus();
    });
  }
  $('gc-save').addEventListener('click', (e) => saveAiKey('gigachat', 'gc-key', e.currentTarget));
  $('oa-save').addEventListener('click', (e) => saveAiKey('openai', 'oa-key', e.currentTarget));
  $('test-ai').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    await call('save_settings', aiSettings());
    await refreshStatus();
    toast('Проверяю ИИ…');
    call('test_ai');
  }));

  function updateAiUi() {
    const p = $('ai-provider').value;
    $('gigachat-box').style.display = p === 'gigachat' ? 'flex' : 'none';
    $('openai-box').style.display = p === 'openai' ? 'flex' : 'none';
    $('ollama-box').style.display = p === 'ollama' ? 'flex' : 'none';
    $('claude-box').style.display = p === 'claude' ? 'flex' : 'none';
    if (p === 'openai') fillPresetIfEmpty();
  }
  $('ai-provider').addEventListener('change', updateAiUi);

  $('check-ollama').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    $('ollama-status').textContent = 'Проверяю Ollama…';
    await call('save_settings', { ai_provider: 'ollama', ollama_model: $('ollama-model').value.trim() || 'qwen2.5:7b' });
    const res = await call('check_ollama');
    $('ollama-status').textContent = res ? str(res.text) : 'Не удалось проверить Ollama.';
    await refreshStatus();
  }));

  function updateVoiceUi() {
    $('eleven-box').style.display = $('tts-voice').value === 'elevenlabs' ? 'flex' : 'none';
    $('eleven-custom').style.display = $('eleven-voice').value === 'custom' ? 'block' : 'none';
  }
  $('tts-voice').addEventListener('change', updateVoiceUi);
  $('eleven-voice').addEventListener('change', updateVoiceUi);

  function elevenVoiceId() {
    return $('eleven-voice').value === 'custom' ? $('eleven-custom').value.trim() : $('eleven-voice').value;
  }

  function voiceSettings() {
    return {
      tts_voice: $('tts-voice').value,
      tts_pitch: $('tts-pitch').value,
      tts_rate: $('tts-rate').value,
      eleven_voice_id: elevenVoiceId(),
    };
  }

  $('save-eleven-key').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    await call('save_settings', voiceSettings());
    const res = await call('save_elevenlabs_key', $('eleven-key').value);
    if (res) toast(res.text);
    if (res && res.ok) $('eleven-key').value = '';
    await refreshStatus();
  }));

  // Обновляет индикаторы и статусы, не трогая поля, которые пользователь мог изменить.
  function applyStatus(raw) {
    const st = obj(raw);
    if (st.config && typeof st.config === 'object') state.config = st.config;
    const ai = $('ai-pill');
    ai.textContent = str(st.ai_label) || 'ИИ: —';
    ai.className = 'pill ' + (st.ai_ok ? 'ok' : 'off');
    const voice = $('voice-pill');
    voice.textContent = st.voice_available ? 'Голос: готов' : 'Голос: нет';
    voice.className = 'pill ' + (st.voice_available ? 'ok' : 'off');
    voice.title = str(st.voice_error);
    $('eleven-status').textContent = st.has_eleven_key ? 'Ключ ElevenLabs сохранён.' : 'Ключ ElevenLabs не задан.';
    renderIntegrations(st);
    $('gc-status').textContent = st.has_gigachat_key ? 'Ключ GigaChat сохранён.' : 'Ключ GigaChat не задан.';
    $('oa-status').textContent = st.has_openai_key ? 'Ключ сервиса сохранён.' : 'Ключ не задан (для LM Studio не нужен).';
    $('key-status').textContent = st.has_key ? 'Ключ сохранён.' : 'Ключ не задан — работают только простые команды.';
  }

  // Полное заполнение формы настроек из конфигурации (запуск и «Сохранить настройки»).
  function applyState(raw, initial) {
    const st = obj(raw);
    const cfg = obj(st.config);
    fillSettings(cfg);
    if (initial) fillProcTab(cfg, st.default_minus_words);
    applyStatus(st);
  }

  $('save-key').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    await call('save_settings', aiSettings());
    const res = await call('save_api_key', $('api-key').value);
    if (res) toast(res.text);
    if (res && res.ok) $('api-key').value = '';
    await refreshStatus();
  }));

  $('test-voice').addEventListener('click', (e) => withButton(e.currentTarget, async () => {
    if (!api()) return;
    await call('save_settings', Object.assign({ voice_enabled: true }, voiceSettings()));
    $('voice-enabled').checked = true;
    call('test_voice');
  }));

  $('settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!api()) return;
    const submit = e.target.querySelector('button[type=submit]');
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      const apps = {};
      $('apps').value.split('\n').forEach((line) => {
        const i = line.indexOf('=');
        if (i > 0) {
          const k = line.slice(0, i).trim();
          const v = line.slice(i + 1).trim();
          if (k && v) apps[k] = v;
        }
      });
      // Ключи, вписанные в поля, но не сохранённые своей кнопкой, тоже сохраняем.
      const keySaves = [['gc-key', (k) => call('save_ai_key', 'gigachat', k)], ['oa-key', (k) => call('save_ai_key', 'openai', k)],
        ['api-key', (k) => call('save_api_key', k)], ['eleven-key', (k) => call('save_elevenlabs_key', k)]];
      const st = await call('save_settings', {
        assistant_name: $('assistant-name').value.trim() || 'Джарвис',
        user_name: $('user-name').value.trim() || 'сэр',
        wake_word: $('wake-word').value.trim().toLowerCase() || 'джарвис',
        city: $('city').value.trim() || 'Москва',
        voice_enabled: $('voice-enabled').checked,
        ...voiceSettings(),
        ...aiSettings(),
        apps: apps,
        company: {
          name: $('co-name').value, inn: $('co-inn').value, kpp: $('co-kpp').value, ogrn: $('co-ogrn').value,
          address: $('co-address').value, director: $('co-director').value,
          director_position: $('co-director-position').value, phone: $('co-phone').value, email: $('co-email').value,
        },
        procurement: {
          pages_per_tag: Math.max(1, Math.min(10, numOr($('pages').value, 1))),
          monitor_interval_min: Math.max(10, Math.min(1440, numOr($('interval').value, 30))),
          ca_bundle: $('ca-bundle').value.trim(),
        },
      });
      if (!st) {
        $('settings-status').textContent = 'Не сохранено.';
        return;
      }
      const notes = [];
      for (const [id, save] of keySaves) {
        const v = $(id).value.trim();
        if (!v) continue;
        const res = await save(v);
        if (res && res.ok) $(id).value = '';
        else if (res) notes.push(res.text);
      }
      if (dirty.email) {
        const res = await saveEmail();
        if (res && !res.ok) notes.push(res.text);
      }
      applyState((await call('get_state')) || st, false);
      $('settings-status').textContent = 'Сохранено.';
      if (notes.length) toast(notes.join(' '));
      setTimeout(() => ($('settings-status').textContent = ''), 3000);
    } finally {
      submit.disabled = false;
    }
  });

  // ---------- события из Python ----------
  function handleEvent(msg) {
    if (!msg || typeof msg !== 'object') return;
    const p = obj(msg.payload);
    switch (msg.event) {
      case 'state':
        setMode(p.mode);
        break;
      case 'tool':
        if (p.text) $('tool-label').textContent = '› ' + str(p.text);
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
        if (!p.text) break;
        addMessage('assistant', '⏰ Напоминание: ' + str(p.text));
        toast('⏰ ' + str(p.text));
        break;
      case 'procurement_status':
        if ('text' in p) setProcStatus(p.text);
        $('proc-search').disabled = !!p.busy;
        break;
      case 'procurement_results': {
        state.items = arr(p.items).filter((x) => x && typeof x === 'object');
        state.analytics = p.analytics || null;
        renderAnalytics(state.analytics);
        renderTable();
        $('proc-search').disabled = false;
        const errors = list(p.errors).map(str);
        const notes = list(p.notes).map(str);
        if (errors.length) setProcStatus(errors.join(' • '), true);
        else setProcStatus('Обновлено: ' + new Date().toLocaleTimeString('ru-RU') + ', актуальных: ' + state.items.length
          + (notes.length ? ' · ' + notes.join(' · ') : ''));
        break;
      }
      case 'tender_state':
        if (p.state && typeof p.state === 'object') renderTender(p.state);
        if ('busy' in p || 'status' in p) setTenderStatus(p.status, !!p.busy, false);
        updateNeural();
        break;
      case 'tender_status':
        setTenderStatus(p.text, !!p.busy, !!p.error);
        updateNeural();
        break;
      case 'analyst_state':
        renderAnalyst(p);
        break;
      case 'analyst_status': {
        const s2 = $('an-status');
        s2.textContent = str(p.text);
        s2.classList.toggle('error', !!p.error);
        s2.classList.toggle('busy', !!p.busy);
        $('an-run').disabled = !!p.busy;
        analystProgressFrom(p.text, !!p.busy, !!p.error);
        updateNeural();
        break;
      }
      case 'telegram_status':
        if (p.text) toast(p.text);
        call('telegram_status').then((tg) => tg && renderIntegrations({ telegram: tg }));
        break;
      case 'toast':
        toast(p.text);
        break;
      case 'switch_tab':
        switchTab(p.tab);
        break;
      case 'monitor_new':
        if (!p.text) break;
        toast(p.text);
        addMessage('assistant', '🔔 ' + str(p.text));
        break;
      default:
        break;
    }
  }
  window.jarvis = {
    onEvent(msg) {
      try {
        handleEvent(msg);
      } catch (e) {
        // Ошибка в одном событии не должна ломать остальные; Python о ней не узнает.
        toast('Ошибка интерфейса: ' + ((e && e.message) || e));
      }
    },
  };

  let started = false;
  async function init() {
    if (started) return;
    started = true;
    const st = await call('get_state');
    if (st) {
      applyState(st, true);
      addMessage('assistant', st.greeting);
    }
    setMode('idle');
    updateNeural();
    pollStats();
    setInterval(pollStats, 3000);
    const ts = await call('tender_state');
    if (ts) renderTender(ts);
    renderAnalyst(await call('analyst_state'));
  }
  window.addEventListener('pywebviewready', init);
  // Если pywebview подготовил API раньше, чем загрузился этот файл, событие уже не придёт.
  if (api() && typeof api().get_state === 'function') setTimeout(init, 0);
  updateTenderButtons();
})();
