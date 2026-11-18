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
    if (value === null || value === undefined) {
      label.textContent = '—';
      return;
    }
    bar.style.width = Math.max(0, Math.min(100, value)) + '%';
    bar.classList.toggle('hot', value >= 85);
    label.textContent = Math.round(value) + '%';
  }

  async function pollStats() {
    if (api() && api().get_system_stats) {
      try {
        const st = await api().get_system_stats();
        setBar('cpu', st.cpu);
        setBar('ram', st.ram);
        setBar('disk', st.disk);
      } catch (e) {
        /* телеметрия необязательна */
      }
    }
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
      strict: $('strict').checked,
      minus_words: $('minus-words').value.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean),
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
      link.addEventListener('click', (e) => {
        e.stopPropagation();
        if (api()) api().open_link(p.url);
      });
      const left = daysLeft(p.deadline);
      const deadline = el('td', { cls: left !== null && left <= 3 && left >= 0 ? 'soon' : '', text: p.deadline || '—' });
      if (left !== null && left >= 0) deadline.appendChild(el('div', { cls: 'small', text: 'осталось дней: ' + left }));
      const work = el('button', { cls: 'btn btn-small', text: 'В работу', title: 'Подготовить заявку по этой закупке' });
      work.type = 'button';
      work.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!api()) return;
        const res = await api().tender_set_purchase(p);
        if (res && res.ok === false) toast(res.text);
        else switchTab('tender');
      });
      const tr = el('tr', { cls: 'row-link', title: 'Открыть закупку на zakupki.gov.ru' }, [
        el('td', null, [link, el('div', null, [el('span', { cls: 'law', text: p.law || '—' })]),
          el('div', { cls: 'small', text: p.method || '' })]),
        el('td', null, [el('div', { cls: 'clamp', text: p.title || '—', title: p.title || '' }),
          el('div', { cls: 'small', text: (p.tags || []).join(', ') })]),
        el('td', null, [el('div', { cls: 'clamp clamp-3', text: p.customer || '—', title: p.customer || '' })]),
        el('td', { cls: 'num', text: money(p.price) }),
        deadline,
        el('td', { cls: 'row-actions' }, [work]),
      ]);
      tr.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        if (window.getSelection && String(window.getSelection())) return; // выделяют текст — не переходим
        if (api()) api().open_link(p.url);
      });
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
      top.appendChild(el('li', { title: c.customer }, [
        el('div', { cls: 'clamp clamp-2', text: c.customer }),
        el('span', { text: money(c.sum) }),
        document.createTextNode(' · закупок: ' + c.count),
      ]));
    });
    const tags = $('by-tag');
    tags.textContent = '';
    Object.entries(a ? a.by_tag : {}).forEach(([tag, v]) => {
      tags.appendChild(el('li', null, [document.createTextNode(tag + ': ' + v.count + ' шт., '), el('span', { text: money(v.sum) })]));
    });
  }


  // ---------- заявка ----------
  const tender = { state: null };

  function kv(label, value) {
    return el('div', { cls: 'fact' }, [el('div', { cls: 'fact-label', text: label }), el('div', { cls: 'fact-value', text: value || '—' })]);
  }

  function renderTender(st) {
    tender.state = st;
    const box = $('tender-purchase');
    box.textContent = '';
    const p = st && st.purchase;
    box.classList.toggle('empty-state', !p);
    if (!p) {
      box.textContent = 'Нажмите «В работу» у закупки на вкладке «Закупки» или введите номер.';
    } else {
      const link = el('a', { text: '№ ' + p.number, title: 'Открыть на zakupki.gov.ru' });
      link.addEventListener('click', () => api() && api().open_link(p.url));
      box.appendChild(el('div', { cls: 'purchase-head' }, [link, el('span', { cls: 'law', text: p.law || '—' })]));
      box.appendChild(el('div', { cls: 'purchase-title', text: p.title || '' }));
      box.appendChild(el('div', { cls: 'purchase-meta' }, [
        kv('Заказчик', p.customer), kv('НМЦК', money(p.price)), kv('Подача до', p.deadline),
      ]));
    }

    const files = $('tender-files');
    files.textContent = '';
    (st && st.files || []).forEach((f) => {
      const ok = /извлечён|архив/.test(f.status);
      files.appendChild(el('li', { cls: ok ? 'ok' : 'bad' }, [
        el('span', { cls: 'file-name', text: f.name }), el('span', { cls: 'file-status', text: f.status + (f.chars ? ' · ' + f.chars.toLocaleString('ru-RU') + ' симв.' : '') }),
      ]));
    });
    $('tender-text-info').textContent = st && st.text_chars ? 'Текст ТЗ: ' + st.text_chars.toLocaleString('ru-RU') + ' символов' : 'Документы не загружены.';

    const a = st && st.analysis;
    const facts = $('tender-facts');
    facts.textContent = '';
    const lists = $('tender-lists');
    lists.textContent = '';
    const posBody = $('tender-positions');
    posBody.textContent = '';
    $('tender-positions-table').style.display = a && a.positions && a.positions.length ? '' : 'none';
    if (a) {
      [['Кратко', a.summary], ['Срок поставки', a.delivery_terms], ['Место поставки', a.delivery_place],
        ['Монтаж и пусконаладка', a.installation], ['Оплата', a.payment_terms], ['Приёмка', a.acceptance_terms],
        ['Срок контракта', a.contract_term], ['Штрафы и пени', a.penalties], ['Гарантия', a.warranty],
        ['Обеспечение заявки', a.bid_security], ['Обеспечение контракта', a.contract_security]]
        .forEach(([k, v]) => facts.appendChild(kv(k, v)));
      [['Ограничения', a.restrictions], ['Требования к участнику', a.participant_requirements], ['Документы в заявке', a.documents_required]]
        .forEach(([title, items]) => {
          const ul = el('ul', { cls: 'list' });
          (items && items.length ? items : ['не найдено']).forEach((t) => ul.appendChild(el('li', { text: t })));
          lists.appendChild(el('div', { cls: 'tender-list' }, [el('h3', { text: title }), ul]));
        });
      (a.positions || []).forEach((pos, i) => {
        const reqs = el('ul', { cls: 'req-list' });
        (pos.characteristics || []).forEach((c) => reqs.appendChild(el('li', { text: c.name + ': ' + (c.requirement || '—') })));
        posBody.appendChild(el('tr', null, [
          el('td', { text: String(i + 1) }), el('td', { text: pos.name }),
          el('td', { text: ((pos.quantity || '') + ' ' + (pos.unit || '')).trim() || '—' }), el('td', null, [reqs]),
        ]));
      });
    }

    $('tender-catalog').textContent = st && st.catalog_count
      ? 'Каталог поставщика: ' + st.catalog_count + ' позиций — подбор только из него.'
      : 'Каталог не загружен — ИИ предложит модели сам (обязательно проверьте).';
    const mBody = $('tender-matches');
    mBody.textContent = '';
    const matches = st && st.matches || [];
    $('tender-matches-table').style.display = matches.length ? '' : 'none';
    matches.forEach((m) => {
      const cls = { 'соответствует': 'badge ok', 'частично': 'badge warn', 'не найдено': 'badge bad' }[m.compliance] || 'badge';
      mBody.appendChild(el('tr', null, [
        el('td', null, [document.createTextNode(m.position), el('div', { cls: 'small', text: ((m.quantity || '') + ' ' + (m.unit || '')).trim() })]),
        el('td', null, [el('div', { text: [m.brand, m.model].filter(Boolean).join(' ') || '—' }),
          el('div', { cls: 'small', text: (m.country || '') + (m.from_catalog ? ' · из каталога' : ' · предложение ИИ') })]),
        el('td', null, [el('span', { cls: cls, text: m.compliance })]),
        el('td', { cls: 'num', text: m.price || '—' }),
        el('td', { cls: 'small', text: m.notes || '' }),
      ]));
    });

    const est = $('tender-estimate');
    est.textContent = '';
    const e = st && st.estimate;
    const t = (st && st.totals) || {};
    est.classList.toggle('empty-state', !e);
    if (!e) {
      est.textContent = 'Появится после разбора ТЗ.';
    } else {
      est.appendChild(el('div', { cls: 'facts an-facts' }, [
        kv('НМЦК', money(p && p.price)),
        kv('Оборудование', money(t.equipment_min) + ' – ' + money(t.equipment_max)),
        kv('Монтаж', e.installation_required ? money(t.installation_min) + ' – ' + money(t.installation_max) : 'не требуется'),
        kv('Себестоимость', money(t.cost_min) + ' – ' + money(t.cost_max)),
        kv('Маржа', t.margin_min !== null && t.margin_min !== undefined
          ? money(t.margin_min) + ' – ' + money(t.margin_max) + ' (' + t.margin_pct_min + '…' + t.margin_pct_max + '%)' : '—'),
        kv('Рекомендация', e.recommendation || '—'),
      ]));
      if (e.equipment && e.equipment.length) {
        const body = el('tbody');
        e.equipment.forEach((it) => body.appendChild(el('tr', null, [
          el('td', null, [document.createTextNode(it.name || ''), el('div', { cls: 'small', text: it.key_specs || '' })]),
          el('td', { text: ((it.quantity || '') + ' ' + (it.unit || '')).trim() }),
          el('td', { cls: 'num', text: money(it.total_min) + ' – ' + money(it.total_max) }),
          el('td', { cls: 'small', text: it.price_basis || '' }),
        ])));
        est.appendChild(el('table', { cls: 'table tender-table an-table' }, [
          el('thead', null, [el('tr', null, [el('th', { text: 'Оборудование' }), el('th', { text: 'Кол-во' }), el('th', { text: 'Стоимость' }), el('th', { text: 'Основа цены' })])]),
          body,
        ]));
      }
      if (e.installation_required && e.installation_scope) est.appendChild(kv('Работы по монтажу', e.installation_scope));
      if (e.risks && e.risks.length) {
        const ul = el('ul', { cls: 'req-list' });
        e.risks.forEach((x) => ul.appendChild(el('li', { text: x })));
        est.appendChild(el('div', { cls: 'tender-list' }, [el('h3', { text: 'Риски' }), ul]));
      }
      if (e.reasoning) est.appendChild(el('div', { cls: 'an-reasoning', text: e.reasoning }));
      est.appendChild(el('div', { cls: 'small', text: 'Цены ориентировочные — проверьте у поставщиков.' }));
    }

    const steps = (st && st.steps) || {};
    [['files', 'step-files'], ['analysis', 'step-analysis'], ['match', 'step-match'], ['estimate', 'step-estimate'], ['docs', 'step-docs']]
      .forEach(([key, id]) => {
        const section = $(id);
        const info = steps[key] || {};
        ['is-running', 'is-done', 'is-error', 'is-skipped'].forEach((c) => section.classList.remove(c));
        if (info.status) section.classList.add('is-' + info.status);
        let note = section.querySelector('.step-note');
        if (!note) {
          note = el('span', { cls: 'step-note' });
          section.querySelector('.step-head').appendChild(note);
        }
        const label = { running: '◌ выполняется', done: '✓ готово', error: '✕ ошибка', skipped: '— пропущено' }[info.status] || '';
        note.textContent = label + (info.note ? ': ' + info.note : '');
        note.title = info.note || '';
      });

    const docs = $('tender-docs');
    docs.textContent = '';
    (st && st.documents || []).forEach((path) => {
      const name = path.split(/[\\/]/).pop();
      const btn = el('button', { cls: 'btn btn-small', text: 'Открыть' });
      btn.type = 'button';
      btn.addEventListener('click', () => api() && api().tender_open(path));
      docs.appendChild(el('li', { cls: 'ok' }, [el('span', { cls: 'file-name', text: '▤ ' + name }), btn]));
    });

    const hasP = !!p;
    ['tender-fetch', 'tender-pick', 'tender-clear', 'tender-analyze', 'tender-open-folder', 'tender-run-all'].forEach((id) => ($(id).disabled = !hasP));
    $('tender-match').disabled = !(a && a.positions && a.positions.length);
    $('tender-build').disabled = !a;
  }

  let timerStart = 0;
  let timerId = null;
  function tickTimer() {
    const sec = Math.floor((Date.now() - timerStart) / 1000);
    $('tender-timer').textContent = '⏱ ' + String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }
  function setTimer(busy) {
    if (busy && !timerId) {
      timerStart = Date.now();
      tickTimer();
      timerId = setInterval(tickTimer, 1000);
    } else if (!busy && timerId) {
      clearInterval(timerId);
      timerId = null;
    }
  }

  function setTenderStatus(text, busy, isError) {
    setTimer(busy);
    const s = $('tender-status');
    s.textContent = text || '';
    s.classList.toggle('error', !!isError);
    s.classList.toggle('busy', !!busy);
    document.querySelectorAll('#tab-tender .btn').forEach((b) => {
      if (busy) b.dataset.wasDisabled = b.disabled ? '1' : '';
      if (busy) b.disabled = true;
    });
    if (!busy && tender.state) renderTender(tender.state);
    else if (!busy) document.querySelectorAll('#tab-tender .btn').forEach((b) => (b.disabled = b.dataset.wasDisabled === '1'));
  }

  $('tender-find').addEventListener('click', () => {
    const v = $('tender-number').value.trim();
    if (v && api()) api().tender_find(v);
  });
  $('tender-number').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('tender-find').click();
    }
  });
  $('tender-fetch').addEventListener('click', () => api() && api().tender_fetch_docs());
  $('tender-run-all').addEventListener('click', () => api() && api().tender_run_all());
  $('tender-pick').addEventListener('click', () => api() && api().tender_pick_files());
  $('tender-clear').addEventListener('click', () => api() && api().tender_clear_files());
  $('tender-analyze').addEventListener('click', () => api() && api().tender_analyze());
  $('tender-match').addEventListener('click', () => api() && api().tender_match());
  $('tender-build').addEventListener('click', () => api() && api().tender_build_docs());
  $('tender-catalog-import').addEventListener('click', () => api() && api().tender_import_catalog());
  $('tender-open-folder').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().tender_open('');
    if (res && res.ok === false) toast(res.text);
  });


  // ---------- аналитика ----------
  function rub(v) {
    return v === null || v === undefined ? '—' : money(v);
  }

  function renderAnalyst(st) {
    if (!st) return;
    const set = st.settings || {};
    if (document.activeElement !== $('an-profile')) $('an-profile').value = set.profile || '';
    $('an-enabled').checked = !!set.enabled;
    $('an-min').value = set.min_score || 60;
    $('an-top').value = set.top_n || 3;

    const scores = $('an-scores');
    scores.textContent = '';
    (st.scores || []).forEach((sc) => {
      const cls = sc.score >= 80 ? 'badge ok' : sc.score >= 60 ? 'badge warn' : 'badge';
      const li = el('li', { title: sc.reason || '' }, [
        el('span', { cls: cls, text: String(sc.score) }),
        el('div', { cls: 'clamp clamp-2', text: sc.title || sc.number }),
        el('div', { cls: 'small', text: sc.reason || '' }),
      ]);
      li.addEventListener('click', () => api() && api().open_link(sc.url));
      li.classList.add('row-link');
      scores.appendChild(li);
    });

    const box = $('an-reports');
    box.textContent = '';
    const reports = st.reports || [];
    if (!reports.length) {
      box.appendChild(el('div', { cls: 'empty', text: 'Отчётов пока нет. Заполните профиль и нажмите «Проанализировать сейчас».' }));
      return;
    }
    reports.forEach((r) => {
      const p = r.purchase || {};
      const e = r.estimate || {};
      const t = r.totals || {};
      const rec = e.recommendation || '';
      const recCls = { 'участвовать': 'badge ok', 'подумать': 'badge warn', 'не участвовать': 'badge bad' }[rec] || 'badge';
      const link = el('a', { text: '№ ' + p.number, title: 'Открыть на zakupki.gov.ru' });
      link.addEventListener('click', () => api() && api().open_link(p.url));
      const toTender = el('button', { cls: 'btn btn-small', text: 'В работу' });
      toTender.type = 'button';
      toTender.addEventListener('click', async () => {
        const res = await api().analyst_to_tender(p.number);
        if (res && res.ok === false) toast(res.text);
        else switchTab('tender');
      });
      const card = el('article', { cls: 'tender-step an-card' }, [
        el('header', { cls: 'an-card-head' }, [
          el('span', { cls: 'an-score', text: String(r.score ?? '—') }),
          el('div', { cls: 'an-title' }, [el('div', { cls: 'clamp clamp-2', text: p.title || '', title: p.title || '' }),
            el('div', { cls: 'small' }, [link, document.createTextNode(' · ' + (p.law || '') + ' · ' + (p.customer || '').slice(0, 90))])]),
          rec ? el('span', { cls: recCls, text: rec }) : null,
          toTender,
        ]),
        el('div', { cls: 'facts an-facts' }, [
          kv('НМЦК', rub(p.price)), kv('Подача до', p.deadline),
          kv('Оборудование', t.equipment_min !== undefined ? rub(t.equipment_min) + ' – ' + rub(t.equipment_max) : '—'),
          kv('Монтаж', e.installation_required ? rub(t.installation_min) + ' – ' + rub(t.installation_max) : (e.equipment ? 'не требуется' : '—')),
          kv('Себестоимость', t.cost_min !== undefined ? rub(t.cost_min) + ' – ' + rub(t.cost_max) : '—'),
          kv('Маржа', t.margin_min !== null && t.margin_min !== undefined
            ? rub(t.margin_min) + ' – ' + rub(t.margin_max) + ' (' + t.margin_pct_min + '…' + t.margin_pct_max + '%)' : '—'),
        ]),
      ]);
      if (r.score_reason) card.appendChild(el('div', { cls: 'small', text: 'Почему подходит: ' + r.score_reason }));
      if (r.error) card.appendChild(el('div', { cls: 'proc-status error', text: 'Глубокий разбор не удался: ' + r.error }));
      if (e.equipment && e.equipment.length) {
        const body = el('tbody');
        e.equipment.forEach((it) => body.appendChild(el('tr', null, [
          el('td', null, [document.createTextNode(it.name || ''), el('div', { cls: 'small', text: it.key_specs || '' })]),
          el('td', { text: ((it.quantity || '') + ' ' + (it.unit || '')).trim() }),
          el('td', { cls: 'num', text: rub(it.total_min) + ' – ' + rub(it.total_max) }),
          el('td', { cls: 'small', text: it.price_basis || '' }),
        ])));
        card.appendChild(el('table', { cls: 'table tender-table an-table' }, [
          el('thead', null, [el('tr', null, [el('th', { text: 'Оборудование' }), el('th', { text: 'Кол-во' }), el('th', { text: 'Стоимость' }), el('th', { text: 'Основа цены' })])]),
          body,
        ]));
      }
      if (e.installation_required && e.installation_scope) card.appendChild(kv('Работы по монтажу', e.installation_scope));
      if (e.other_costs) card.appendChild(kv('Прочие расходы', e.other_costs));
      if (e.risks && e.risks.length) {
        const ul = el('ul', { cls: 'req-list' });
        e.risks.forEach((x) => ul.appendChild(el('li', { text: x })));
        card.appendChild(el('div', { cls: 'tender-list' }, [el('h3', { text: 'Риски' }), ul]));
      }
      if (e.reasoning) card.appendChild(el('div', { cls: 'an-reasoning', text: e.reasoning }));
      card.appendChild(el('div', { cls: 'small', text: 'Цены ориентировочные: ' + (e.equipment && e.equipment.some((x) => x.price_basis === 'каталог') ? 'частично из вашего каталога.' : 'оценка ИИ, проверьте у поставщиков.') }));
      box.appendChild(card);
    });
  }

  function analystValues() {
    return {
      enabled: $('an-enabled').checked,
      profile: $('an-profile').value.trim(),
      min_score: Number($('an-min').value) || 60,
      top_n: Number($('an-top').value) || 3,
    };
  }
  $('an-save').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().analyst_save(analystValues());
    toast(res.text);
    renderAnalyst(await api().analyst_state());
  });
  $('an-run').addEventListener('click', async () => {
    if (!api()) return;
    await api().analyst_save(analystValues());
    api().analyst_run_now();
  });

  // ---------- Telegram и почта ----------
  function renderIntegrations(st) {
    const tg = st.telegram || {};
    $('tg-status').textContent = !tg.enabled ? 'Не подключён.'
      : tg.paired ? 'Подключён' + (tg.bot ? ' (@' + tg.bot + ')' : '') + ', чат привязан. Бот отвечает только вам.'
        : 'Бот подключён' + (tg.bot ? ' (@' + tg.bot + ')' : '') + ', ждёт привязки чата.';
    if (tg.code && !tg.paired) $('tg-code').textContent = 'Отправьте боту: /start ' + tg.code;
    else $('tg-code').textContent = '';
    const em = st.email || {};
    $('em-status').textContent = em.configured ? 'Почта настроена.' : (em.has_password ? 'Пароль сохранён.' : 'Пароль не задан.');
  }

  $('tg-connect').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().telegram_connect($('tg-token').value.trim());
    toast(res.text);
    if (res.ok) {
      $('tg-token').value = '';
      $('tg-code').textContent = res.text;
    }
    renderIntegrations(await api().get_state());
  });
  $('tg-test').addEventListener('click', async () => toast((await api().telegram_test()).text));
  $('tg-disconnect').addEventListener('click', async () => {
    toast((await api().telegram_disconnect()).text);
    renderIntegrations(await api().get_state());
  });

  function updateEmailUi() {
    $('em-custom').style.display = $('em-preset').value === 'custom' ? 'flex' : 'none';
  }
  $('em-preset').addEventListener('change', updateEmailUi);
  $('em-save').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().email_save({
      enabled: $('em-enabled').checked, preset: $('em-preset').value, host: $('em-host').value.trim(),
      port: Number($('em-port').value) || 465, login: $('em-login').value.trim(), to: $('em-to').value.trim(),
      password: $('em-password').value,
    });
    toast(res.text);
    if (res.ok) $('em-password').value = '';
    renderIntegrations(await api().get_state());
  });
  $('em-test').addEventListener('click', () => api() && api().email_test());
  $('make-shortcut').addEventListener('click', async () => toast((await api().create_shortcut()).text));

  // ---------- настройки ----------
  function fillSettings(cfg) {
    $('assistant-name').value = cfg.assistant_name || '';
    $('user-name').value = cfg.user_name || '';
    $('wake-word').value = cfg.wake_word || '';
    $('city').value = cfg.city || '';
    $('voice-enabled').checked = !!cfg.voice_enabled;
    $('tts-voice').value = cfg.tts_voice || 'ru-RU-DmitryNeural';
    $('tts-pitch').value = cfg.tts_pitch || '-8Hz';
    $('tts-rate').value = cfg.tts_rate || '+0%';
    const eid = cfg.eleven_voice_id || 'onwK4e9ZLuTAKqWW03F9';
    const known = Array.from($('eleven-voice').options).some((o) => o.value === eid);
    $('eleven-voice').value = known ? eid : 'custom';
    $('eleven-custom').value = known ? '' : eid;
    updateVoiceUi();
    $('ai-provider').value = cfg.ai_provider || 'ollama';
    $('ollama-model').value = cfg.ollama_model || 'qwen2.5:7b';
    $('oa-preset').value = cfg.openai_preset || 'hermes_openrouter';
    $('oa-url').value = cfg.openai_base_url || '';
    $('oa-model').value = cfg.openai_model || '';
    $('gc-model').value = cfg.gigachat_model || 'GigaChat-2';
    $('gc-scope').value = cfg.gigachat_scope || 'GIGACHAT_API_PERS';
    updateAiUi();
    $('model').value = cfg.model || 'claude-opus-5-5';
    $('effort').value = cfg.effort || 'low';
    $('apps').value = Object.entries(cfg.apps || {}).map(([k, v]) => k + ' = ' + v).join('\n');
    const em = cfg.email || {};
    $('em-enabled').checked = !!em.enabled;
    $('em-preset').value = em.preset || 'yandex';
    $('em-host').value = em.host || '';
    $('em-port').value = em.port || 465;
    $('em-login').value = em.login || '';
    $('em-to').value = em.to || '';
    updateEmailUi();
    const co = cfg.company || {};
    [['co-name', 'name'], ['co-inn', 'inn'], ['co-kpp', 'kpp'], ['co-ogrn', 'ogrn'], ['co-address', 'address'],
      ['co-director', 'director'], ['co-director-position', 'director_position'], ['co-phone', 'phone'], ['co-email', 'email']]
      .forEach(([id, key]) => ($(id).value = co[key] || ''));
    const p = cfg.procurement || {};
    $('pages').value = p.pages_per_tag || 1;
    $('interval').value = p.monitor_interval_min || 30;
    $('ca-bundle').value = p.ca_bundle || '';
    state.tags = (p.tags || []).slice();
    $('fz44').checked = p.fz44 !== false;
    $('fz223').checked = p.fz223 !== false;
    $('only-active').checked = p.only_active !== false;
    $('strict').checked = p.strict !== false;
    if (Array.isArray(p.minus_words)) $('minus-words').value = p.minus_words.join(', ');
    $('price-from').value = p.price_from || '';
    $('price-to').value = p.price_to || '';
    renderTags();
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
  $('oa-preset').addEventListener('change', () => {
    const pr = OA_PRESETS[$('oa-preset').value];
    if (pr) {
      $('oa-url').value = pr[0];
      $('oa-model').value = pr[1];
    }
  });
  async function saveAiKey(provider, inputId) {
    if (!api()) return;
    const res = await api().save_ai_key(provider, $(inputId).value.trim());
    toast(res.text);
    if (res.ok) $(inputId).value = '';
    applyState(await api().get_state());
  }
  $('gc-save').addEventListener('click', () => saveAiKey('gigachat', 'gc-key'));
  $('oa-save').addEventListener('click', () => saveAiKey('openai', 'oa-key'));
  $('test-ai').addEventListener('click', async () => {
    if (!api()) return;
    await api().save_settings(aiSettings());
    applyState(await api().get_state());
    toast('Проверяю ИИ…');
    api().test_ai();
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
    };
  }

  function updateAiUi() {
    const p = $('ai-provider').value;
    $('gigachat-box').style.display = p === 'gigachat' ? 'flex' : 'none';
    $('openai-box').style.display = p === 'openai' ? 'flex' : 'none';
    $('ollama-box').style.display = p === 'ollama' ? 'flex' : 'none';
    $('claude-box').style.display = p === 'claude' ? 'flex' : 'none';
  }
  $('ai-provider').addEventListener('change', updateAiUi);

  $('check-ollama').addEventListener('click', async () => {
    if (!api()) return;
    await api().save_settings({ ai_provider: 'ollama', ollama_model: $('ollama-model').value.trim() || 'qwen2.5:7b' });
    const res = await api().check_ollama();
    $('ollama-status').textContent = res.text;
    applyState(await api().get_state());
  });

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

  $('save-eleven-key').addEventListener('click', async () => {
    if (!api()) return;
    const res = await api().save_elevenlabs_key($('eleven-key').value);
    toast(res.text);
    if (res.ok) {
      $('eleven-key').value = '';
      applyState(await api().get_state());
    }
  });

  function applyState(st) {
    state.config = st.config;
    fillSettings(st.config);
    const ai = $('ai-pill');
    ai.textContent = st.ai_label;
    ai.className = 'pill ' + (st.ai_ok ? 'ok' : 'off');
    const voice = $('voice-pill');
    voice.textContent = st.voice_available ? 'Голос: готов' : 'Голос: нет';
    voice.className = 'pill ' + (st.voice_available ? 'ok' : 'off');
    voice.title = st.voice_error || '';
    $('eleven-status').textContent = st.has_eleven_key ? 'Ключ ElevenLabs сохранён.' : 'Ключ ElevenLabs не задан.';
    renderIntegrations(st);
    $('gc-status').textContent = st.has_gigachat_key ? 'Ключ GigaChat сохранён.' : 'Ключ GigaChat не задан.';
    $('oa-status').textContent = st.has_openai_key ? 'Ключ сервиса сохранён.' : 'Ключ не задан (для LM Studio не нужен).';
    const pc = (st.config && st.config.procurement) || {};
    if (!Array.isArray(pc.minus_words)) $('minus-words').value = (st.default_minus_words || []).join(', ');
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

  $('test-voice').addEventListener('click', async () => {
    if (!api()) return;
    await api().save_settings(Object.assign({ voice_enabled: true }, voiceSettings()));
    $('voice-enabled').checked = true;
    api().test_voice();
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
      tts_voice: $('tts-voice').value,
      tts_pitch: $('tts-pitch').value,
      tts_rate: $('tts-rate').value,
      eleven_voice_id: elevenVoiceId(),
      ...aiSettings(),
      model: $('model').value,
      effort: $('effort').value,
      apps: apps,
      company: {
        name: $('co-name').value, inn: $('co-inn').value, kpp: $('co-kpp').value, ogrn: $('co-ogrn').value,
        address: $('co-address').value, director: $('co-director').value,
        director_position: $('co-director-position').value, phone: $('co-phone').value, email: $('co-email').value,
      },
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
          else setProcStatus('Обновлено: ' + new Date().toLocaleTimeString('ru-RU') + ', актуальных: ' + state.items.length
            + (p.notes && p.notes.length ? ' · ' + p.notes.join(' · ') : ''));
          break;
        case 'tender_state':
          renderTender(p.state);
          setTenderStatus(p.status, !!p.busy, false);
          break;
        case 'tender_status':
          setTenderStatus(p.text, !!p.busy, !!p.error);
          break;
        case 'analyst_state':
          renderAnalyst(p);
          break;
        case 'analyst_status': {
          const s2 = $('an-status');
          s2.textContent = p.text || '';
          s2.classList.toggle('error', !!p.error);
          s2.classList.toggle('busy', !!p.busy);
          $('an-run').disabled = !!p.busy;
          break;
        }
        case 'telegram_status':
          if (p.text) toast(p.text);
          if (p.paired && api()) api().get_state().then(renderIntegrations);
          break;
        case 'toast':
          toast(p.text);
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
    pollStats();
    setInterval(pollStats, 3000);
    renderTender(await api().tender_state());
    renderAnalyst(await api().analyst_state());
  });
})();
