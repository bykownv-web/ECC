/* J.A.R.V.I.S. — «нейросеть, которая собирается» вместо реактора на вкладках «Заявка» и «Аналитика».
   Контракт: window.JarvisNeural.setVisible(bool), window.JarvisNeural.setProgress(0..1 | null, { state, label }).
   Canvas 2D, без библиотек. Цикл requestAnimationFrame работает только пока виджет видим. */
(function () {
  'use strict';

  // ---------- палитра (как в style.css) ----------
  const C = {
    cyan: [86, 216, 255],
    teal: [45, 240, 207],
    amber: [255, 181, 74],
    violet: [155, 123, 255],
    white: [235, 250, 255],
    magenta: [255, 70, 170],
    blue: [60, 120, 255],
  };
  const NODE_TARGET = 150;

  // ---------- детерминированный ГСЧ ----------
  function rng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const R = rng(20261119);
  const rand = (a, b) => a + (b - a) * R();
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  const smooth = (v) => v * v * (3 - 2 * v);

  // ---------- целевая структура: сома + дендриты + аксон + внешняя сеть ----------
  // node: { tx, ty, parent, rank, kind: 'nucleus'|'soma'|'branch'|'tip'|'web', depth }
  function buildStructure() {
    const nodes = [];
    const add = (n) => (nodes.push(n), nodes.length - 1);
    const nucleus = add({ tx: 0, ty: 0, parent: -1, dist: 0, kind: 'nucleus', depth: 0 });
    const somaR = 0.12;
    const somaIdx = [];
    const SOMA = 10;
    for (let i = 0; i < SOMA; i++) {
      const a = (i / SOMA) * Math.PI * 2 + rand(-0.12, 0.12);
      const r = somaR * rand(0.85, 1.12);
      somaIdx.push(add({ tx: Math.cos(a) * r, ty: Math.sin(a) * r * 0.92, parent: nucleus, dist: r, kind: 'soma', depth: 0, ang: a }));
    }
    const step = 0.082;
    function grow(x, y, ang, len, depth, parent, dist) {
      const n = Math.max(2, Math.round(len / step));
      let px = x, py = y, last = parent, d = dist;
      for (let i = 0; i < n; i++) {
        ang += rand(-0.28, 0.28);
        const nx = px + Math.cos(ang) * step * rand(0.85, 1.15);
        const ny = py + Math.sin(ang) * step * rand(0.85, 1.15);
        if (Math.hypot(nx, ny) > 0.93) break;
        d += step;
        last = add({ tx: nx, ty: ny, parent: last, dist: d, kind: 'branch', depth });
        px = nx; py = ny;
      }
      if (depth < 2 && last !== parent) {
        const spread = rand(0.38, 0.62);
        grow(px, py, ang - spread, len * 0.62, depth + 1, last, d);
        grow(px, py, ang + spread * rand(0.8, 1.2), len * 0.6, depth + 1, last, d);
      } else if (last !== parent) {
        nodes[last].kind = 'tip';
      }
    }
    const DEND = 6;
    const base = rand(0, Math.PI * 2);
    for (let i = 0; i < DEND; i++) {
      const a = base + (i / DEND) * Math.PI * 2 + rand(-0.25, 0.25);
      // ближайшая точка сомы — корень ветви
      let root = somaIdx[0], best = 9;
      somaIdx.forEach((s) => {
        const da = Math.abs(Math.atan2(Math.sin(nodes[s].ang - a), Math.cos(nodes[s].ang - a)));
        if (da < best) { best = da; root = s; }
      });
      const isAxon = i === 0;
      grow(nodes[root].tx, nodes[root].ty, a, isAxon ? 0.62 : rand(0.36, 0.46), isAxon ? 1 : 0, root, nodes[root].dist);
    }
    // внешняя «паутина» — синапсы на кончиках ветвей
    const tips = nodes.map((n, i) => i).filter((i) => nodes[i].kind === 'tip' || (nodes[i].kind === 'branch' && Math.hypot(nodes[i].tx, nodes[i].ty) > 0.45));
    let guard = 0;
    while (nodes.length < NODE_TARGET && guard++ < 2000) {
      const a = rand(0, Math.PI * 2), r = rand(0.55, 0.94);
      const x = Math.cos(a) * r, y = Math.sin(a) * r;
      let near = -1, nd = 9;
      for (const t of tips) {
        const d = Math.hypot(nodes[t].tx - x, nodes[t].ty - y);
        if (d < nd) { nd = d; near = t; }
      }
      if (near < 0 || nd > 0.24 || nd < 0.05) continue;
      add({ tx: x, ty: y, parent: near, dist: nodes[near].dist + nd, kind: 'web', depth: 3 });
    }
    // масштаб, чтобы влезло в круг
    let maxR = 0;
    nodes.forEach((n) => (maxR = Math.max(maxR, Math.hypot(n.tx, n.ty))));
    const k = maxR > 0.92 ? 0.92 / maxR : 1;
    let maxD = 0;
    nodes.forEach((n) => { n.tx *= k; n.ty *= k; maxD = Math.max(maxD, n.dist); });
    // порядок «защёлкивания»: от сомы к периферии, паутина последней
    nodes.forEach((n) => {
      if (n.kind === 'nucleus') n.rank = 0;
      else if (n.kind === 'soma') n.rank = rand(0.02, 0.1);
      else if (n.kind === 'web') n.rank = rand(0.84, 0.99);
      else n.rank = clamp01(0.1 + (n.dist / maxD) * 0.72 + rand(-0.04, 0.04));
    });
    return nodes;
  }

  // ---------- спрайты свечения (рисуем заранее, на кадре — только drawImage) ----------
  function sprite(rgb, size, kind) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const g = c.getContext('2d');
    const h = size / 2;
    const grad = g.createRadialGradient(h, h, 0, h, h, h);
    const [r, gg, b] = rgb;
    if (kind === 'bokeh') {
      grad.addColorStop(0, `rgba(${r},${gg},${b},0.55)`);
      grad.addColorStop(0.72, `rgba(${r},${gg},${b},0.42)`);
      grad.addColorStop(0.86, `rgba(${r},${gg},${b},0.5)`);
      grad.addColorStop(1, `rgba(${r},${gg},${b},0)`);
    } else {
      grad.addColorStop(0, 'rgba(255,255,255,1)');
      grad.addColorStop(0.12, `rgba(${r},${gg},${b},0.95)`);
      grad.addColorStop(0.35, `rgba(${r},${gg},${b},0.32)`);
      grad.addColorStop(1, `rgba(${r},${gg},${b},0)`);
    }
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    return c;
  }

  // ---------- состояние ----------
  let root = null, canvas = null, ctx = null, readout = null, rPct = null, rLabel = null;
  let W = 0, H = 0, dpr = 1, bg = null, mask = null;
  let nodes = null, S = null, bokeh = null;
  let visible = false, raf = 0, lastT = 0, time = 0;
  let target = 0, shown = 0, indeterminate = false, state = 'idle', label = '';
  let loosen = 0, errFlash = 0, bloomT = -1, bloomed = false, somaFlash = 0, pulseAcc = 0;
  const pulses = [];
  const reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  let lastPctText = '', lastLabelText = '';

  function init() {
    if (root) return true;
    root = document.getElementById('neural');
    if (!root) return false;
    canvas = document.createElement('canvas');
    ctx = canvas.getContext('2d');
    if (!ctx) { root = null; return false; }
    root.appendChild(canvas);
    readout = document.createElement('div');
    readout.className = 'neural-readout';
    rPct = document.createElement('b');
    rLabel = document.createElement('span');
    readout.appendChild(rPct);
    readout.appendChild(rLabel);
    root.appendChild(readout);

    S = {
      cyan: sprite(C.cyan, 64), teal: sprite(C.teal, 64), amber: sprite(C.amber, 64),
      violet: sprite(C.violet, 64), white: sprite(C.white, 64), magenta: sprite(C.magenta, 64),
      bViolet: sprite(C.violet, 64, 'bokeh'), bBlue: sprite(C.blue, 64, 'bokeh'), bAmber: sprite(C.amber, 64, 'bokeh'), bCyan: sprite(C.cyan, 64, 'bokeh'),
    };
    nodes = buildStructure().map((n, i) => {
      const a = rand(0, Math.PI * 2), r = Math.sqrt(R()) * 0.9;
      const kind = n.kind;
      return Object.assign(n, {
        x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0, w: 0,
        fx: Math.cos(a) * r, fy: Math.sin(a) * r, // «свободная» позиция для статичного кадра
        ph: rand(0, 6.28), ph2: rand(0, 6.28), sp: rand(0.6, 1.4),
        free: R() < 0.32 ? 'violet' : 'cyan',
        size: kind === 'nucleus' ? 1 : kind === 'soma' ? 0.9 : kind === 'web' ? rand(0.45, 0.7) : kind === 'tip' ? 0.8 : rand(0.55, 0.8),
        id: i,
      });
    });
    bokeh = [];
    for (let i = 0; i < 22; i++) {
      const pick = R();
      bokeh.push({
        x: rand(-1, 1), y: rand(-1, 1), r: rand(0.04, 0.16), a: rand(0.05, 0.16), z: rand(0.2, 1),
        ph: rand(0, 6.28), s: pick < 0.4 ? 'bViolet' : pick < 0.7 ? 'bBlue' : pick < 0.85 ? 'bCyan' : 'bAmber',
      });
    }
    if (window.ResizeObserver) new ResizeObserver(resize).observe(root);
    window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
    if (reduce.addEventListener) reduce.addEventListener('change', () => { stop(); start(); });
    resize();
    return true;
  }

  function resize() {
    if (!root) return;
    const w = root.clientWidth, h = root.clientHeight;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (w === W && h === H && ratio === dpr) return;
    W = w; H = h; dpr = ratio;
    if (!W || !H) return;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    // фон и круговая маска с мягким краем
    bg = document.createElement('canvas');
    bg.width = canvas.width; bg.height = canvas.height;
    let g = bg.getContext('2d');
    let grad = g.createRadialGradient(bg.width / 2, bg.height / 2, 0, bg.width / 2, bg.height / 2, bg.width / 2);
    grad.addColorStop(0, 'rgba(26,52,110,0.55)');
    grad.addColorStop(0.55, 'rgba(14,30,72,0.35)');
    grad.addColorStop(1, 'rgba(6,12,30,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, bg.width, bg.height);
    mask = document.createElement('canvas');
    mask.width = canvas.width; mask.height = canvas.height;
    g = mask.getContext('2d');
    grad = g.createRadialGradient(mask.width / 2, mask.height / 2, 0, mask.width / 2, mask.height / 2, mask.width / 2);
    grad.addColorStop(0, 'rgba(0,0,0,1)');
    grad.addColorStop(0.8, 'rgba(0,0,0,1)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, mask.width, mask.height);
    if (reduce.matches || !raf) renderStatic();
  }

  // ---------- физика ----------
  const GRID = 0.26;
  const grid = new Map();
  const links = []; // [i, j, alpha]

  function nodeWeight(n, p) {
    return smooth(clamp01((p - n.rank * 0.92) / 0.08));
  }

  function step(dt) {
    time += dt;
    // плавное приближение к цели (без скачков, с ограничением скорости)
    let tgt = target;
    if (indeterminate) tgt = 0.3 + 0.28 * (0.5 + 0.5 * Math.sin(time * 0.9));
    const diff = tgt - shown;
    const maxStep = 0.45 * dt;
    shown += Math.max(-maxStep, Math.min(maxStep, diff * (1 - Math.exp(-dt * 2.2))));
    loosen += ((state === 'error' ? 0.3 : 0) - loosen) * (1 - Math.exp(-dt * 1.5));
    errFlash = Math.max(0, errFlash - dt * 1.4);
    somaFlash = Math.max(0, somaFlash - dt * 1.8);

    const done = state === 'done' && target >= 0.999;
    if (done && shown > 0.97 && !bloomed) { bloomed = true; bloomT = 0; somaFlash = 1; }
    if (!done) bloomed = false;
    if (bloomT >= 0) { bloomT += dt; if (bloomT > 1.6) bloomT = -1; }

    const p = Math.max(0, shown - loosen);
    const assembled = bloomed;
    const breath = (0.006 + 0.012 * p) * Math.sin(time * 1.5);
    const kSpring = assembled ? 16 : 7;

    // сетка соседей
    grid.clear();
    for (const n of nodes) {
      const key = Math.floor((n.x + 1.3) / GRID) * 64 + Math.floor((n.y + 1.3) / GRID);
      let cell = grid.get(key);
      if (!cell) grid.set(key, (cell = []));
      cell.push(n);
    }
    links.length = 0;
    const linkCount = new Uint8Array(nodes.length);

    for (const n of nodes) {
      n.w = assembled ? 1 : nodeWeight(n, p);
      const free = 1 - n.w;
      let ax = 0, ay = 0;
      if (free > 0.001) {
        // поле течения + индивидуальное блуждание (не орбиты)
        const fa = Math.sin(n.x * 2.1 + time * 0.23) * Math.cos(n.y * 1.7 - time * 0.19) * 3.14 + Math.sin(time * 0.07 + n.ph);
        ax += Math.cos(fa) * 0.16 + Math.sin(time * 0.8 * n.sp + n.ph) * 0.09;
        ay += Math.sin(fa) * 0.16 + Math.cos(time * 0.7 * n.sp + n.ph2) * 0.09;
        // мягкая граница круга
        const r = Math.hypot(n.x, n.y);
        if (r > 0.86) { ax -= (n.x / r) * (r - 0.86) * 4; ay -= (n.y / r) * (r - 0.86) * 4; }
        ax *= free; ay *= free;
      }
      if (n.w > 0.001) {
        const jx = Math.sin(time * 1.3 * n.sp + n.ph) * 0.006, jy = Math.cos(time * 1.1 * n.sp + n.ph2) * 0.006;
        const tx = n.tx * (1 + breath) + jx, ty = n.ty * (1 + breath) + jy;
        ax += (tx - n.x) * kSpring * n.w;
        ay += (ty - n.y) * kSpring * n.w;
      }
      // соседи: отталкивание и нити
      const gx = Math.floor((n.x + 1.3) / GRID), gy = Math.floor((n.y + 1.3) / GRID);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const cell = grid.get((gx + ox) * 64 + gy + oy);
          if (!cell) continue;
          for (const m of cell) {
            if (m.id <= n.id) continue;
            const dx = m.x - n.x, dy = m.y - n.y;
            const d2 = dx * dx + dy * dy;
            if (d2 > GRID * GRID || d2 < 1e-6) continue;
            const d = Math.sqrt(d2);
            if (d < 0.07) {
              const f = ((0.07 - d) / 0.07) * 0.6 * (1 - Math.min(n.w, m.w));
              ax -= (dx / d) * f; ay -= (dy / d) * f;
              m.vx += (dx / d) * f * dt; m.vy += (dy / d) * f * dt;
            }
            if (linkCount[n.id] < 3 && linkCount[m.id] < 4) {
              const a = (1 - d / GRID) * (0.1 + 0.28 * p);
              if (a > 0.025) { links.push(n.id, m.id, a); linkCount[n.id]++; linkCount[m.id]++; }
            }
          }
        }
      }
      n.vx = (n.vx + ax * dt) * Math.exp(-dt * (n.w > 0.5 ? 6 : 1.6));
      n.vy = (n.vy + ay * dt) * Math.exp(-dt * (n.w > 0.5 ? 6 : 1.6));
    }
    for (const n of nodes) { n.x += n.vx * dt; n.y += n.vy * dt; }

    // синаптические импульсы к соме
    const rate = indeterminate ? 4 : state === 'error' ? 0.3 : 0.3 + 7 * p * p;
    pulseAcc += rate * dt;
    while (pulseAcc >= 1 && pulses.length < 40) {
      pulseAcc -= 1;
      const s = nodes[(R() * nodes.length) | 0];
      if (s.w < 0.85 || s.parent < 0) continue;
      const path = [];
      let cur = s.id;
      while (cur >= 0 && path.length < 40) { path.push(cur); cur = nodes[cur].parent; }
      if (path.length >= 3) pulses.push({ path, t: 0, speed: rand(4, 7) });
    }
    if (pulseAcc > 1) pulseAcc = 1;
    for (let i = pulses.length - 1; i >= 0; i--) {
      const pl = pulses[i];
      pl.t += pl.speed * dt;
      if (pl.t >= pl.path.length - 1) { pulses.splice(i, 1); somaFlash = Math.min(1, somaFlash + 0.12); }
    }
  }

  // ---------- отрисовка ----------
  function draw() {
    if (!W || !H) return;
    const g = ctx;
    const cx = W / 2, cy = H / 2, RR = (Math.min(W, H) / 2) * 0.96;
    const X = (x) => cx + x * RR, Y = (y) => cy + y * RR;
    const p = Math.max(0, shown - loosen);
    const err = errFlash > 0 && (errFlash > 0.6 || Math.sin(time * 60) > 0);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.globalCompositeOperation = 'source-over';
    g.globalAlpha = 1;
    g.clearRect(0, 0, W, H);
    if (bg) g.drawImage(bg, 0, 0, W, H);

    g.globalCompositeOperation = 'lighter';
    // боке — дальний слой, медленный параллакс
    for (const b of bokeh) {
      const bx = b.x + Math.sin(time * 0.05 * b.z + b.ph) * 0.08 * b.z;
      const by = b.y + Math.cos(time * 0.04 * b.z + b.ph) * 0.06 * b.z;
      const s = b.r * RR * 2;
      g.globalAlpha = b.a * (0.7 + 0.3 * Math.sin(time * 0.3 + b.ph));
      g.drawImage(S[b.s], X(bx) - s / 2, Y(by) - s / 2, s, s);
    }

    // нити между соседями (пачками по яркости)
    g.lineWidth = 0.6;
    for (let bucket = 0; bucket < 3; bucket++) {
      const lo = [0, 0.08, 0.16][bucket], hi = [0.08, 0.16, 9][bucket];
      g.beginPath();
      for (let i = 0; i < links.length; i += 3) {
        const a = links[i + 2];
        if (a < lo || a >= hi) continue;
        const n = nodes[links[i]], m = nodes[links[i + 1]];
        g.moveTo(X(n.x), Y(n.y));
        g.lineTo(X(m.x), Y(m.y));
      }
      g.globalAlpha = [0.08, 0.16, 0.24][bucket] * (0.9 + 0.5 * p);
      g.strokeStyle = err ? 'rgb(255,70,170)' : bucket === 0 ? 'rgb(155,123,255)' : 'rgb(86,216,255)';
      g.stroke();
    }

    // структурные связи (дендриты): широкое свечение + тонкая жила
    const strength = 0.35 + 0.65 * p;
    for (let pass = 0; pass < 2; pass++) {
      for (let bucket = 0; bucket < 4; bucket++) {
        const lo = bucket / 4, hi = (bucket + 1) / 4 + (bucket === 3 ? 0.01 : 0);
        g.beginPath();
        let any = false;
        for (const n of nodes) {
          if (n.parent < 0) continue;
          const m = nodes[n.parent];
          const w = Math.min(n.w, m.w);
          if (w <= 0.02 || w < lo || w >= hi) continue;
          g.moveTo(X(n.x), Y(n.y));
          g.lineTo(X(m.x), Y(m.y));
          any = true;
        }
        if (!any) continue;
        const mid = (lo + hi) / 2;
        g.globalAlpha = (pass === 0 ? 0.13 : 0.75) * mid * strength;
        g.lineWidth = pass === 0 ? 3.2 : 1;
        g.strokeStyle = err ? 'rgb(255,70,170)' : pass === 0 ? 'rgb(45,240,207)' : 'rgb(140,230,255)';
        g.stroke();
      }
    }

    // импульсы
    for (const pl of pulses) {
      const i = Math.floor(pl.t), f = pl.t - i;
      const a = nodes[pl.path[i]], b = nodes[pl.path[Math.min(i + 1, pl.path.length - 1)]];
      const px = a.x + (b.x - a.x) * f, py = a.y + (b.y - a.y) * f;
      const s = RR * 0.09;
      g.globalAlpha = 0.9;
      g.drawImage(err ? S.magenta : S.amber, X(px) - s / 2, Y(py) - s / 2, s, s);
      g.globalAlpha = 0.35;
      const qx = a.x + (b.x - a.x) * Math.max(0, f - 0.5), qy = a.y + (b.y - a.y) * Math.max(0, f - 0.5);
      g.drawImage(S.amber, X(qx) - s * 0.35, Y(qy) - s * 0.35, s * 0.7, s * 0.7);
    }

    // узлы
    for (const n of nodes) {
      if (n.kind === 'nucleus') continue;
      const locked = n.w > 0.5;
      const spr = err ? S.magenta : locked ? (n.kind === 'tip' || n.kind === 'web' ? S.teal : n.kind === 'soma' ? S.amber : S.cyan) : S[n.free];
      const s = RR * 0.075 * n.size * (0.9 + 0.35 * n.w);
      g.globalAlpha = 0.62 + 0.36 * n.w;
      g.drawImage(spr, X(n.x) - s / 2, Y(n.y) - s / 2, s, s);
    }

    // сома: ядро светится тем сильнее, чем больше собрано
    const nu = nodes[0];
    const breathe = 0.5 + 0.5 * Math.sin(time * 1.5);
    const glow = (0.15 + 0.85 * nu.w) * (0.85 + 0.15 * breathe) + somaFlash * 0.6;
    let s = RR * (0.42 + 0.18 * p + somaFlash * 0.25);
    g.globalAlpha = Math.min(1, 0.35 * glow);
    g.drawImage(err ? S.magenta : S.amber, X(nu.x) - s / 2, Y(nu.y) - s / 2, s, s);
    s = RR * (0.3 + 0.1 * p);
    g.globalAlpha = Math.min(1, 0.55 * glow);
    g.drawImage(S.cyan, X(nu.x) - s / 2, Y(nu.y) - s / 2, s, s);
    s = RR * (0.12 + 0.04 * breathe * p);
    g.globalAlpha = Math.min(1, 0.4 + 0.6 * glow);
    g.drawImage(S.white, X(nu.x) - s / 2, Y(nu.y) - s / 2, s, s);

    // «сборка завершена»: ударная волна и вспышка
    if (bloomT >= 0) {
      const k = bloomT / 1.6;
      g.globalAlpha = (1 - k) * 0.9;
      g.lineWidth = 2.5 * (1 - k) + 0.5;
      g.strokeStyle = 'rgb(140,240,255)';
      g.beginPath();
      g.arc(X(nu.x), Y(nu.y), RR * (0.12 + k * 1.0), 0, Math.PI * 2);
      g.stroke();
      g.globalAlpha = Math.max(0, 0.5 - k) * 0.8;
      g.strokeStyle = 'rgb(255,181,74)';
      g.beginPath();
      g.arc(X(nu.x), Y(nu.y), RR * (0.08 + k * 0.7), 0, Math.PI * 2);
      g.stroke();
      const fs = RR * 1.4 * (0.4 + k);
      g.globalAlpha = Math.max(0, 0.35 * (1 - k * 1.5));
      g.drawImage(S.cyan, X(nu.x) - fs / 2, Y(nu.y) - fs / 2, fs, fs);
    }

    // мягкий круговой край
    g.globalAlpha = 1;
    g.globalCompositeOperation = 'destination-in';
    if (mask) g.drawImage(mask, 0, 0, W, H);
    g.globalCompositeOperation = 'source-over';
    updateReadout();
  }

  function updateReadout() {
    if (!readout) return;
    const pct = indeterminate ? '···' : Math.round(clamp01(shown) * 100) + '%';
    if (pct !== lastPctText) { rPct.textContent = pct; lastPctText = pct; }
    const lab = label || '';
    if (lab !== lastLabelText) { rLabel.textContent = lab; rLabel.title = lab; lastLabelText = lab; }
    readout.dataset.state = state;
  }

  // статичный кадр для prefers-reduced-motion
  function renderStatic() {
    if (!nodes || !W) return;
    shown = indeterminate ? 0.5 : target;
    const p = Math.max(0, shown - (state === 'error' ? 0.3 : 0));
    const full = state === 'done' && target >= 0.999;
    for (const n of nodes) {
      n.w = full ? 1 : nodeWeight(n, p);
      n.x = n.fx + (n.tx - n.fx) * n.w;
      n.y = n.fy + (n.ty - n.fy) * n.w;
    }
    links.length = 0;
    for (let i = 0; i < nodes.length; i++) {
      let c = 0;
      for (let j = i + 1; j < nodes.length && c < 3; j++) {
        const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
        if (d < GRID) { links.push(i, j, (1 - d / GRID) * (0.1 + 0.28 * p)); c++; }
      }
    }
    pulses.length = 0;
    bloomT = -1;
    loosen = state === 'error' ? 0.3 : 0;
    draw();
  }

  function frame(t) {
    raf = 0;
    if (!visible || document.hidden) return;
    const dt = lastT ? Math.min(0.05, (t - lastT) / 1000) : 0.016;
    lastT = t;
    step(dt);
    draw();
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (!visible || !root || document.hidden) return;
    resize();
    if (reduce.matches) { renderStatic(); return; }
    if (!raf) { lastT = 0; raf = requestAnimationFrame(frame); }
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  window.JarvisNeural = {
    setVisible(on) {
      try {
        visible = !!on;
        if (!init()) return;
        if (visible) start(); else stop();
      } catch (e) { /* декоративный виджет не должен ломать интерфейс */ }
    },
    setProgress(value, opts) {
      try {
        if (!init()) return;
        const o = opts || {};
        const st = ['idle', 'working', 'done', 'error'].includes(o.state) ? o.state : state;
        if (st === 'error' && state !== 'error') errFlash = 1;
        state = st;
        if (typeof o.label === 'string') label = o.label.slice(0, 40);
        if (value === null || value === undefined || isNaN(value)) {
          indeterminate = st === 'working';
        } else {
          indeterminate = false;
          target = clamp01(Number(value));
        }
        if (st === 'done' && (value === null || value === undefined)) target = 1;
        if (reduce.matches || !raf) renderStatic();
      } catch (e) { /* без падений */ }
    },
  };
})();
