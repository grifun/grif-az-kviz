// Shared client code: WebSocket connection, hex board rendering, timer, sounds.
"use strict";

const AZ = (() => {
  const ROWS = 7;
  const S = 50;                       // hex circumradius
  const W = Math.sqrt(3) * S;         // hex width (pointy-top)
  const COORDS = [];
  for (let r = 0; r < ROWS; r++) for (let c = 0; c <= r; c++) COORDS.push([r, c]);
  const NS = "http://www.w3.org/2000/svg";
  const CLASS = { "-1": "empty", 0: "p0", 1: "p1", 2: "black" };

  const center = ([r, c]) => [(c - r / 2) * W, r * 1.5 * S];

  function hexPoints(cx, cy, s) {
    const pts = [];
    for (let k = 0; k < 6; k++) {
      const a = Math.PI / 180 * (60 * k - 90);
      pts.push((cx + s * Math.cos(a)).toFixed(2) + "," + (cy + s * Math.sin(a)).toFixed(2));
    }
    return pts.join(" ");
  }

  function el(name, attrs = {}, parent) {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }

  function gradient(defs, id, top, bottom) {
    const g = el("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    el("stop", { offset: "0%", "stop-color": top }, g);
    el("stop", { offset: "100%", "stop-color": bottom }, g);
  }

  // Triangle frame whose sides run parallel to the board sides at distance d from the cell centres.
  function framePoints(d) {
    const ys = 6 * 1.5 * S, xs = 3 * W, L = Math.hypot(xs, ys);
    const topY = -d * L / xs;
    const botY = ys + d;
    const botX = (d * L + xs * botY) / ys;
    return [[0, topY], [-botX, botY], [botX, botY]];
  }

  /** Build the SVG board once; returns an object with update(state, opts). */
  function createBoard(svg, onClick) {
    svg.innerHTML = "";
    svg.classList.add("board");
    const defs = el("defs", {}, svg);
    gradient(defs, "g-empty", "#ffffff", "#b8c4de");
    gradient(defs, "g-p0", "#ffb04d", "#e06a00");
    gradient(defs, "g-p1", "#5aa8ff", "#1458c8");
    gradient(defs, "g-black", "#454a56", "#16181d");

    const frame = framePoints(S * 1.35);
    el("polygon", {
      points: frame.map(p => p.map(v => v.toFixed(1)).join(",")).join(" "),
      fill: "rgba(255,255,255,.04)", stroke: "rgba(255,255,255,.18)", "stroke-width": 4,
      "stroke-linejoin": "round",
    }, svg);
    const pad = 12;
    const minX = frame[1][0] - pad, maxX = frame[2][0] + pad;
    const minY = frame[0][1] - pad, maxY = frame[1][1] + pad;
    svg.setAttribute("viewBox", `${minX} ${minY} ${maxX - minX} ${maxY - minY}`);

    const cells = COORDS.map((rc, i) => {
      const [x, y] = center(rc);
      const g = el("g", { class: "cell empty", "data-i": i }, svg);
      el("polygon", { class: "hex", points: hexPoints(x, y, S - 2) }, g);
      el("polygon", { class: "shine", points: hexPoints(x, y, S - 9) }, g);
      const t = el("text", { x, y: y + 2 }, g);
      g.addEventListener("click", () => onClick && onClick(i));
      return { g, t };
    });

    function update(state, opts = {}) {
      const win = new Set(state.win_cells || []);
      cells.forEach(({ g, t }, i) => {
        const st = state.cells[i];
        const cls = ["cell", CLASS[st]];
        if (state.active === i) cls.push("active");
        if (win.has(i)) cls.push("win");
        if (opts.clickable && opts.clickable(i)) cls.push("clickable");
        g.setAttribute("class", cls.join(" "));
        const label = state.labels[i];
        if (t.textContent !== label) t.textContent = label;
        t.setAttribute("font-size", label.length > 1 ? 30 : 36);
      });
    }
    return { update };
  }

  /** WebSocket with auto-reconnect. Calls onState(state) for each update. */
  function connect({ onState, onError, onStatus }) {
    const gid = location.pathname.split("/")[2];
    const token = new URLSearchParams(location.search).get("t") || "";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/${gid}` + (token ? `?t=${encodeURIComponent(token)}` : "");
    let ws, retry = 0, dead = false, pingTimer;

    function open() {
      ws = new WebSocket(url);
      ws.onopen = () => {
        retry = 0;
        onStatus && onStatus(true);
        clearInterval(pingTimer);
        pingTimer = setInterval(() => send("ping"), 25000);  // keep proxies from idling us out
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          AZ.clockOffset = msg.state.now * 1000 - Date.now();
          onState(msg.state);
        } else if (msg.type === "error") {
          if (msg.fatal) { dead = true; fatal(msg.message); }
          else onError ? onError(msg.message) : toast(msg.message);
        }
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        onStatus && onStatus(false);
        if (dead) return;
        retry = Math.min(retry + 1, 6);
        setTimeout(open, 500 * 2 ** retry);
      };
    }

    function send(action, data) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ action, data }));
      else if (action !== "ping") toast(I18N.t("no_connection"));
    }

    open();
    return { send, gid, token };
  }

  let toastTimer;
  function toast(text) {
    let t = document.querySelector(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
  }

  function fatal(text) {
    const d = document.createElement("div");
    d.className = "fatal";
    d.innerHTML = `<div><p></p><p><a href="/"></a></p></div>`;
    d.querySelector("p").textContent = text;
    d.querySelector("a").textContent = I18N.t("new_game_link");
    document.body.appendChild(d);
  }

  /** Seconds remaining on the state's timer (null if none). */
  function timeLeft(timer) {
    if (!timer) return null;
    const now = (Date.now() + (AZ.clockOffset || 0)) / 1000;
    return Math.max(0, timer.ends_at - now);
  }

  function runTimerBar(getTimer, bar, label) {
    function tick() {
      const timer = getTimer();
      const left = timeLeft(timer);
      if (left === null) {
        bar.hidden = true;
        if (label) label.textContent = "";
      } else {
        bar.hidden = false;
        bar.querySelector("div").style.width = (100 * left / timer.duration) + "%";
        bar.classList.toggle("low", left <= 3);
        if (label) label.textContent = Math.ceil(left) + " s";
      }
      requestAnimationFrame(tick);
    }
    tick();
  }

  // ---- tiny synth for sound effects ----
  let actx = null;
  function audio() {
    if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === "suspended") actx.resume();
    return actx;
  }
  function tone(freq, start, dur, type = "sine", vol = 0.18) {
    const ctx = audio();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.value = freq;
    const t0 = ctx.currentTime + start;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(vol, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(ctx.destination);
    o.start(t0); o.stop(t0 + dur + 0.05);
  }
  function sweep(from, to, dur, vol = 0.12) {
    const ctx = audio();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "triangle";
    const t0 = ctx.currentTime;
    o.frequency.setValueAtTime(from, t0);
    o.frequency.exponentialRampToValueAtTime(to, t0 + dur);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(vol, t0 + dur * 0.3);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(ctx.destination);
    o.start(t0); o.stop(t0 + dur + 0.05);
  }
  // Clock tick: a short filtered noise click on top of a pitched blip.
  function click(freq, vol) {
    const ctx = audio();
    const len = Math.floor(ctx.sampleRate * 0.03);
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / len) ** 4;
    const src = ctx.createBufferSource(), bp = ctx.createBiquadFilter(), g = ctx.createGain();
    src.buffer = buf; bp.type = "bandpass"; bp.frequency.value = freq; bp.Q.value = 6; g.gain.value = vol * 4;
    src.connect(bp).connect(g).connect(ctx.destination);
    src.start();
    tone(freq, 0, 0.05, "square", vol * 0.25);
  }
  const SOUNDS = {
    pick: () => sweep(260, 1100, 0.6),
    open: () => { tone(784, 0, 0.12, "triangle", 0.12); tone(1175, 0.08, 0.25, "triangle", 0.12); },
    tick: () => click(2200, 0.18),
    tock: () => click(1500, 0.18),
    tickLow: () => click(2900, 0.32),
    timeout: () => { tone(196, 0, 0.7, "sawtooth", 0.14); tone(185, 0, 0.7, "square", 0.06); },
    correct: () => { tone(660, 0, 0.15); tone(990, 0.12, 0.3); },
    wrong: () => tone(160, 0, 0.45, "sawtooth", 0.12),
    black: () => { tone(220, 0, 0.25, "square", 0.08); tone(150, 0.2, 0.4, "square", 0.08); },
    win: () => [523, 659, 784, 1047].forEach((f, i) => tone(f, i * 0.14, 0.35, "triangle")),
    tie: () => { tone(440, 0, 0.3); tone(440, 0.35, 0.3); },
  };
  function play(kind) {
    try {
      if (!SOUNDS[kind]) return;
      const ctx = audio();
      if (ctx.state === "running") SOUNDS[kind]();
    } catch (_) { /* audio unavailable */ }
  }

  /**
   * Plays a tick-tock every second while the state's timer runs and a buzzer when it hits zero.
   * `enabled()` decides whether this window is the sound source.
   */
  function runTicker(getTimer, enabled) {
    let key = null, lastSec = null, buzzed = false, n = 0;
    setInterval(() => {
      const timer = getTimer();
      if (!timer) { key = null; return; }
      const k = timer.ends_at;
      if (k !== key) { key = k; lastSec = null; buzzed = timeLeft(timer) <= 0; n = 0; }
      const left = timeLeft(timer);
      const sec = Math.ceil(left);
      if (left > 0 && sec !== lastSec) {
        if (lastSec !== null && enabled()) play(sec <= 3 ? "tickLow" : (n % 2 ? "tock" : "tick"));
        lastSec = sec; n++;
      } else if (left <= 0 && !buzzed) {
        buzzed = true;
        if (enabled()) play("timeout");
      }
    }, 50);
  }

  const phaseText = (phase) => (phase === "over" ? "" : I18N.t("phase_" + phase));

  /** Localised text for a resolved question (state.reveal). */
  function outcomeText(r, names) {
    const name = names[r.owner] || "";
    const key = { correct: "outcome_correct", steal_correct: "outcome_correct", black: "outcome_black", steal_declined: "outcome_declined",
      yesno_correct: "outcome_yesno_correct", yesno_wrong: "outcome_yesno_wrong" }[r.outcome];
    return key ? I18N.t(key, { name }) : "";
  }

  /** Answer as shown to people: yes/no answers are stored as "yes"/"no". */
  const answerText = (kind, a) => (kind === "yesno" && (a === "yes" || a === "no") ? I18N.t(a) : a);

  // ---- question-set picker (home page + referee) ----

  /**
   * Render the list of sets grouped by language.
   * opts: { selected: Set<id>, onToggle(id, checked), used: bool (show used counts) }
   */
  function renderSets(box, sets, opts) {
    box.innerHTML = "";
    const langs = [I18N.lang, ...I18N.LANGS.filter((l) => l !== I18N.lang)];   // current language first
    for (const lang of langs) {
      const mine = sets.filter((s) => s.lang === lang);
      if (!mine.length) continue;
      const h = document.createElement("div");
      h.className = "setgroup";
      h.textContent = I18N.t("lang_" + lang);
      box.appendChild(h);
      for (const s of mine) {
        const row = document.createElement("label");
        row.className = "setrow";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = opts.selected.has(s.id);
        cb.onchange = () => opts.onToggle(s.id, cb.checked);
        const txt = document.createElement("span");
        txt.className = "setname";
        const name = document.createElement("b");
        name.textContent = s.name[I18N.lang] || s.name.cs;
        const meta = document.createElement("small");
        const parts = [];
        if (s.open) parts.push(I18N.t("n_open", { n: s.open }));
        if (s.yesno) parts.push(I18N.t("n_yesno", { n: s.yesno }));
        if (opts.used && s.used && (s.used.open || s.used.yesno)) parts.push(I18N.t("used_n", { n: s.used.open + s.used.yesno }));
        meta.textContent = parts.join(" · ");
        const desc = document.createElement("small");
        desc.className = "setdesc";
        desc.textContent = (s.description && (s.description[I18N.lang] || s.description.cs)) || "";
        txt.append(name, meta, desc);
        const pv = document.createElement("button");
        pv.type = "button";
        pv.className = "btn small";
        pv.textContent = I18N.t("preview");
        pv.onclick = (e) => { e.preventDefault(); previewSet(s); };
        row.append(cb, txt, pv);
        box.appendChild(row);
      }
    }
  }

  /** Show a set's questions in a modal dialog. `set` is a summary; `items` may be given directly. */
  async function previewSet(set, items) {
    let dlg = document.getElementById("setPreview");
    if (!dlg) {
      dlg = document.createElement("dialog");
      dlg.id = "setPreview";
      dlg.className = "preview";
      dlg.innerHTML = `<div class="pvhead"><h3></h3><button class="btn small" type="button"></button></div><ol></ol>`;
      dlg.querySelector("button").onclick = () => dlg.close();
      dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
      document.body.appendChild(dlg);
    }
    dlg.querySelector("h3").textContent = `${I18N.t("questions_in_set")}: ${set.name[I18N.lang] || set.name.cs}`;
    dlg.querySelector(".pvhead button").textContent = I18N.t("close");
    const list = dlg.querySelector("ol");
    list.innerHTML = `<li>${I18N.t("loading")}</li>`;
    dlg.showModal();
    try {
      const data = items || await (await fetch(`/api/sets/${encodeURIComponent(set.id)}`)).json();
      list.innerHTML = "";
      for (const [kind, arr] of [["open", data.open || []], ["yesno", data.yesno || []]]) {
        for (const q of arr) {
          const li = document.createElement("li");
          const a = document.createElement("b");
          a.textContent = answerText(kind, q.a);
          li.append(document.createTextNode(q.q + " → "), a);
          list.appendChild(li);
        }
      }
    } catch (_) {
      list.innerHTML = "";
      toast(I18N.t("no_connection"));
    }
  }

  return { createBoard, connect, toast, timeLeft, runTimerBar, runTicker, play, audio, hexPoints,
    phaseText, outcomeText, answerText, renderSets, previewSet, clockOffset: 0 };
})();
