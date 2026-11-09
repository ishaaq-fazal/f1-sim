// Pit Wall front end. Draws what the Python server streams; all rules run on the server.
// DOM text is set with textContent only (never innerHTML), so names can't inject markup.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};

const S = {
  ws: null, setup: null, team: null, track: null, compounds: {},
  state: null, gapMode: "gap", speed: 1, paused: false,
  disp: new Map(),            // code -> displayed track position (smoothed)
  laps: new Map(),            // code -> [{lap, time, pit, tyre}]
  charted: new Set(), fastest: null, resultShown: false,
};

// ---------------------------------------------------------------- formatting
function fmtLap(s) {
  if (s == null) return "–";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
}
function fmtClock(s) {
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = Math.floor(s % 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- accounts
async function api(path, opts = {}) {
  const o = { credentials: "same-origin", headers: {}, ...opts };
  if (o.body) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
  let r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    // access token expired: try the refresh cookie once
    const rr = await fetch("/api/auth/refresh", { method: "POST", credentials: "same-origin" });
    if (rr.ok) r = await fetch(path, o);
  }
  return r;
}

async function loadMe() {
  const r = await api("/api/me");
  S.user = r.ok ? await r.json() : null;
  $("userChip").hidden = $("logoutBtn").hidden = !S.user;
  if (S.user) $("userChip").textContent = `${S.user.email} · ${S.user.role}`;
  return S.user;
}

async function submitAuth(kind) {
  const email = $("authEmail").value.trim(), password = $("authPassword").value;
  $("authError").textContent = "";
  const r = await api(`/api/auth/${kind}`, { method: "POST", body: { email, password } });
  if (!r.ok) {
    let msg = "Something went wrong.";
    if (r.status === 422) msg = kind === "signup" ? "Use a valid email and a password of 10+ characters." : "Enter a valid email and password.";
    else { try { msg = (await r.json()).detail || msg; } catch (_) { /* keep default */ } }
    $("authError").textContent = msg;
    return;
  }
  $("authPassword").value = "";
  $("authDialog").close();
  await loadMe();
  openStart();
}

async function openStart() {
  if (!S.user) { $("authDialog").showModal(); return; }
  await loadHistory();
  $("startDialog").showModal();
}

async function loadHistory() {
  const r = await api("/api/races");
  const list = $("historyList");
  const races = r.ok ? await r.json() : [];
  $("historyBox").hidden = races.length === 0;
  const teams = new Map((S.setup ? S.setup.teams : []).map((t) => [t.id, t.name]));
  list.replaceChildren(...races.slice(0, 8).map((x) => {
    const li = el("li", null, `${new Date(x.created_at).toLocaleString()} · ${teams.get(x.team) || x.team} · ${x.laps} laps · ${x.summary} · seed ${x.seed}`);
    const del = el("button", "del", "delete"); del.type = "button";
    del.addEventListener("click", async () => { await api(`/api/races/${x.id}`, { method: "DELETE" }); loadHistory(); });
    li.append(del);
    return li;
  }));
}

// ---------------------------------------------------------------- setup
async function init() {
  $("authForm").addEventListener("submit", (e) => { e.preventDefault(); submitAuth("login"); });
  $("signupBtn").addEventListener("click", () => {
    if ($("authForm").reportValidity()) submitAuth("signup");
  });
  $("logoutBtn").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    if (S.ws) S.ws.close();
    S.user = null; await loadMe(); $("authDialog").showModal();
  });
  const res = await fetch("/api/setup");
  S.setup = await res.json();
  const picker = $("teamPicker");
  S.setup.teams.forEach((t, i) => {
    const lab = el("label");
    const inp = el("input");
    inp.type = "radio"; inp.name = "team"; inp.value = t.id; inp.checked = i === 0;
    const dot = el("i"); dot.style.background = t.color;
    lab.append(inp, dot, el("span", null, t.name));
    picker.append(lab);
  });
  const seg = $("speedSeg");
  S.setup.speeds.forEach((v) => {
    const b = el("button", v === 1 ? "on" : "", `${v}×`);
    b.dataset.v = v;
    b.addEventListener("click", () => send({ type: "speed", value: v }, () => setSpeed(v)));
    seg.append(b);
  });
  $("gapSeg").addEventListener("click", (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (!v) return;
    S.gapMode = v;
    for (const b of $("gapSeg").children) b.classList.toggle("on", b.dataset.v === v);
    $("gapHead").textContent = v === "gap" ? "Gap" : "Int";
    if (S.state) renderTower(S.state);
  });
  $("pauseBtn").addEventListener("click", () => {
    S.paused = !S.paused;
    send({ type: "pause", paused: S.paused });
    $("pauseBtn").textContent = S.paused ? "Resume" : "Pause";
  });
  $("newRaceBtn").addEventListener("click", openStart);
  $("startForm").addEventListener("submit", (e) => { e.preventDefault(); startRace(); });
  $("againBtn").addEventListener("click", () => setTimeout(openStart, 0));
  window.addEventListener("resize", () => { S.trackCache = null; });
  initChart();
  await loadMe();
  openStart();
  requestAnimationFrame(drawLoop);
}

function setSpeed(v) {
  S.speed = v;
  for (const b of $("speedSeg").children) b.classList.toggle("on", Number(b.dataset.v) === v);
}

function startRace() {
  const team = document.querySelector('input[name="team"]:checked').value;
  const laps = Number($("lapsSelect").value);
  const seedRaw = $("seedInput").value.trim();
  const msg = { type: "start", team, laps };
  if (seedRaw !== "") msg.seed = Number(seedRaw);
  connect(() => {
    S.ws.send(JSON.stringify(msg));
    $("startDialog").close();
  });
}

function connect(onOpen) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) return onOpen();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/race`);
  S.ws = ws;
  ws.addEventListener("open", onOpen);
  ws.addEventListener("message", (ev) => onMessage(JSON.parse(ev.data)));
  ws.addEventListener("close", (ev) => {
    if (ev.code === 4401) { S.user = null; loadMe(); $("authDialog").showModal(); }
    $("flag").textContent = "OFFLINE"; $("flag").className = "flag";
    $("pauseBtn").disabled = true;
  });
  ws.addEventListener("error", () => { $("startError").textContent = "Could not reach the race server."; });
}

function send(msg, after) {
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN) return;
  S.ws.send(JSON.stringify(msg));
  if (after) after();
}

// ---------------------------------------------------------------- messages
function onMessage(m) {
  if (m.type === "init") {
    S.track = prepTrack(m.track); S.trackCache = null;
    S.team = m.team; S.compounds = m.compounds; S.canCommand = m.can_command;
    S.disp.clear(); S.laps.clear(); S.charted.clear(); S.fastest = null; S.resultShown = false;
    S.paused = false; $("pauseBtn").textContent = "Pause"; $("pauseBtn").disabled = false;
    $("log").replaceChildren();
    $("trackName").textContent = `${m.track.name} · ${(m.track.length_m / 1000).toFixed(2)} km`;
    const t = S.setup.teams.find((x) => x.id === m.team);
    const chip = $("teamChip"); chip.textContent = t.name; chip.style.background = t.color; chip.style.color = "#000";
    resetChart();
  } else if (m.type === "state") {
    onState(m);
  } else if (m.type === "error") {
    addLog({ lap: S.state ? S.state.lap : "", kind: "dnf", text: `Server: ${m.detail}` });
  }
}

function onState(st) {
  const first = !S.state || S.state.t > st.t;
  S.state = st;
  if (first || S.charted.size === 0) {
    st.cars.filter((c) => c.controlled).forEach((c) => S.charted.add(c.code));
    if (st.cars[0]) S.charted.add(st.cars[0].code);
  }
  setSpeed(st.speed);
  st.laps.forEach((l) => {
    if (!S.laps.has(l.code)) S.laps.set(l.code, []);
    S.laps.get(l.code).push(l);
  });
  st.events.forEach(addLog);
  S.fastest = st.fastest;
  renderHeader(st);
  renderTower(st);
  renderWall(st);
  if (st.laps.length) updateChart();
  if (st.finished && !S.resultShown) { S.resultShown = true; showResults(st); }
}

function renderHeader(st) {
  $("lapCounter").textContent = `LAP ${st.lap}/${st.total_laps}`;
  $("raceClock").textContent = fmtClock(st.t);
  const f = $("flag");
  if (st.chequered) { f.className = "flag chequered"; f.textContent = "FINISH"; }
  else if (st.vsc) { f.className = "flag vsc"; f.textContent = "VSC"; }
  else { f.className = "flag green"; f.textContent = "GREEN"; }
  $("fastestLap").textContent = st.fastest ? `Fastest: ${st.fastest.code} ${fmtLap(st.fastest.time)} (lap ${st.fastest.lap})` : "";
}

function tyreBadge(code, age) {
  const wrap = el("span");
  const t = el("span", "tyre", code);
  t.style.color = (S.compounds[code] || {}).color || "#888";
  wrap.append(t);
  if (age !== undefined) wrap.append(el("span", "tyre-age", String(age)));
  return wrap;
}

function renderTower(st) {
  const body = $("towerBody");
  const rows = st.cars.map((c) => {
    const tr = el("tr");
    if (c.controlled) tr.classList.add("mine");
    if (c.retired) tr.classList.add("out");
    if (S.charted.has(c.code)) tr.classList.add("charted");
    tr.addEventListener("click", () => {
      S.charted.has(c.code) ? S.charted.delete(c.code) : S.charted.add(c.code);
      updateChart(); renderTower(S.state);
    });
    const bar = el("td", "teambar"); const i = el("i"); i.style.background = c.color; bar.append(i);
    const who = el("td"); who.append(el("span", "code", c.code));
    if (c.in_pit) who.append(el("span", "badge pit", "PIT"));
    else if (c.pit_request) who.append(el("span", "badge box", "BOX"));
    const gapTxt = S.gapMode === "gap" ? c.gap : (c.pos === 1 ? "INTERVAL" : c.interval);
    const last = el("td", "r", fmtLap(c.last));
    if (c.last != null && st.fastest && Math.abs(c.last - st.fastest.time) < 1e-6) last.classList.add("purple");
    else if (c.last != null && c.best != null && Math.abs(c.last - c.best) < 1e-6) last.classList.add("pb");
    const best = el("td", "r", fmtLap(c.best));
    if (st.fastest && st.fastest.code === c.code) best.classList.add("purple");
    const tyre = el("td"); tyre.append(tyreBadge(c.tyre, c.tyre_age));
    tr.append(el("td", null, String(c.pos)), bar, who, el("td", "r", c.finished && c.pos === 1 ? "WINNER" : gapTxt),
      last, best, tyre, el("td", "r", String(c.pits)));
    return tr;
  });
  body.replaceChildren(...rows);
}

// ---------------------------------------------------------------- pit wall
function renderWall(st) {
  const wall = $("wall");
  const mine = st.cars.filter((c) => c.controlled);
  // Build cards once per race, then update their contents.
  if (wall.dataset.team !== S.team || wall.children.length !== mine.length) {
    wall.dataset.team = S.team;
    wall.replaceChildren(...mine.map(buildCard));
  }
  mine.forEach((c) => updateCard(c, st));
}

function buildCard(c) {
  const card = el("div", "card"); card.dataset.code = c.code;
  const top = el("div", "card-top");
  top.append(el("span", "pos"), el("span", "who", `${c.name} (${c.code})`));
  const stat = el("div", "stat");
  for (const k of ["Gap", "Last", "Best", "Tyre", "Wear", "Stops"]) {
    const d = el("div"); d.append(el("span", null, k), el("b")); d.dataset.k = k; stat.append(d);
  }
  const wear = el("div", "wearbar"); wear.append(el("i"));
  const pred = el("p", "predict");
  const act = el("div", "actions");
  act.append(el("span", "label", "Box this lap for"));
  for (const k of ["S", "M", "H"]) {
    const b = el("button", "boxbtn", S.compounds[k].name);
    b.type = "button"; b.dataset.box = k; b.style.color = S.compounds[k].color; b.style.borderColor = S.compounds[k].color;
    b.addEventListener("click", () => {
      const cur = S.state.cars.find((x) => x.code === c.code);
      send({ type: "pit", driver: c.code, compound: cur && cur.pit_request === k ? null : k });
    });
    act.append(b);
  }
  act.append(el("span", "label", "Pace"));
  const seg = el("div", "seg small");
  for (const [k, label] of [["push", "Push"], ["normal", "Normal"], ["conserve", "Conserve"]]) {
    const b = el("button", null, label); b.type = "button"; b.dataset.mode = k;
    b.addEventListener("click", () => send({ type: "mode", driver: c.code, mode: k }));
    seg.append(b);
  }
  act.append(seg);
  card.append(top, stat, wear, pred, act);
  return card;
}

function updateCard(c, st) {
  const card = [...$("wall").children].find((n) => n.dataset.code === c.code);
  if (!card) return;
  card.querySelector(".pos").textContent = c.retired ? "DNF" : `P${c.pos}`;
  const set = (k, v) => { card.querySelector(`[data-k="${k}"] b`).textContent = v; };
  set("Gap", c.pos === 1 ? "Leader" : c.gap);
  set("Last", fmtLap(c.last));
  set("Best", fmtLap(c.best));
  set("Tyre", `${S.compounds[c.tyre].name} · ${c.tyre_age}L`);
  set("Wear", `${c.wear}%`);
  set("Stops", `${c.pits} (${c.used.join("/")})`);
  const bar = card.querySelector(".wearbar i");
  bar.style.width = `${Math.max(0, 100 - c.wear)}%`;
  bar.style.background = c.wear > 85 ? "#ff4d4d" : c.wear > 60 ? "#ffb020" : "#2fd16f";
  const done = c.finished || c.retired;
  card.querySelectorAll(".boxbtn").forEach((b) => {
    b.disabled = done || c.in_pit || !S.canCommand;
    const on = c.pit_request === b.dataset.box;
    b.classList.toggle("on", on);
    b.style.background = on ? S.compounds[b.dataset.box].color : "transparent";
  });
  card.querySelectorAll("[data-mode]").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === c.mode);
    b.disabled = done || !S.canCommand;
  });

  const pred = card.querySelector(".predict");
  const p = (st.predictions || []).find((x) => x.code === c.code);
  pred.replaceChildren();
  if (!S.canCommand) pred.append("Viewer account: you can watch, but only team principals can give orders. ");
  if (done) pred.append(c.finished ? "Finished." : "Retired.");
  else if (c.in_pit) pred.append(`In the pit lane: ${c.pit_timer.toFixed(1)}s`);
  else if (p) {
    pred.append("Model's best plan: ");
    if (!p.stops.length) pred.append(el("b", null, "no more stops, run to the flag"));
    else p.stops.forEach((s, i) => {
      if (i) pred.append(", then ");
      pred.append(el("b", null, `box ${s.in_laps <= 1 ? "this lap" : `in ${s.in_laps} laps (lap ${s.lap})`} for ${S.compounds[s.compound].name}s`));
    });
    if (c.used.length < 2 && !p.stops.length) pred.append(" (penalty risk)");
  }
}

// ---------------------------------------------------------------- log + results
function addLog(ev) {
  const li = el("li", `k-${ev.kind}`);
  li.append(el("span", "lapno", ev.lap !== "" ? `L${ev.lap}` : ""), el("span", null, ev.text));
  const log = $("log");
  log.prepend(li);
  while (log.children.length > 200) log.lastChild.remove();
}

function showResults(st) {
  const body = $("resultBody");
  const mine = st.cars.filter((c) => c.controlled).map((c) => `${c.code} P${c.pos}`).join(", ");
  $("resultLede").textContent = `Your team: ${mine}. Winner: ${st.cars[0].name}.`;
  body.replaceChildren(...st.cars.map((c) => {
    const tr = el("tr"); if (c.controlled) tr.classList.add("mine");
    const bar = el("td", "teambar"); const i = el("i"); i.style.background = c.color; bar.append(i);
    tr.append(el("td", null, String(c.pos)), bar, el("td", null, c.name),
      el("td", "r", c.pos === 1 ? "WINNER" : c.gap), el("td", "r", c.penalty ? `+${c.penalty}s pen` : ""),
      el("td", "r", `${c.pits} stop${c.pits === 1 ? "" : "s"}`));
    return tr;
  }));
  $("resultDialog").showModal();
}

// ---------------------------------------------------------------- lap chart
let chart = null;
function initChart() {
  if (!window.Chart) return;
  Chart.defaults.color = "#8b93a7";
  Chart.defaults.borderColor = "#262c3d";
  chart = new Chart($("lapChart"), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false, maintainAspectRatio: false, parsing: false, spanGaps: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "linear", title: { display: true, text: "Lap" }, ticks: { precision: 0 } },
        y: { title: { display: true, text: "Lap time (s)" } },
      },
      plugins: {
        legend: { labels: { boxWidth: 10 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmtLap(ctx.parsed.y)}` } },
      },
    },
  });
}
function resetChart() { if (chart) { chart.data.datasets = []; chart.update(); } }
function updateChart() {
  if (!chart || !S.state) return;
  const colors = new Map(S.state.cars.map((c) => [c.code, c.color]));
  const sets = [];
  const seenColor = new Set();
  let ys = [];
  for (const code of S.charted) {
    // Leave out lap 1 and pit laps so the scale shows racing pace.
    const pts = (S.laps.get(code) || []).map((l) => ({ x: l.lap, y: l.lap === 1 || l.pit ? null : l.time }));
    ys = ys.concat(pts.filter((p) => p.y != null).map((p) => p.y));
    const col = colors.get(code);
    const dashed = seenColor.has(col);   // teammates share a colour, so dash the second one
    seenColor.add(col);
    sets.push({ label: code, data: pts, borderColor: col, backgroundColor: col, borderDash: dashed ? [6, 4] : [],
      pointRadius: 2, borderWidth: 2, tension: 0.2 });
  }
  chart.data.datasets = sets;
  if (ys.length) {
    ys.sort((a, b) => a - b);
    const lo = ys[0], p90 = ys[Math.floor(ys.length * 0.9)];
    chart.options.scales.y.min = Math.floor(lo - 0.5);
    chart.options.scales.y.max = Math.ceil(Math.max(p90 + 1, lo + 3));
  }
  chart.update();
}

// ---------------------------------------------------------------- track drawing
function prepTrack(t) {
  const pts = t.points;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, area = 0;
  pts.forEach(([x, y], i) => {
    minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    const [x2, y2] = pts[(i + 1) % pts.length]; area += x * y2 - x2 * y;
  });
  return { ...t, minX, minY, maxX, maxY, inside: area > 0 ? 1 : -1 };
}

// Point and inward normal at a distance fraction round the lap.
function pointAt(tr, f) {
  f = ((f % 1) + 1) % 1;
  const d = tr.dist, n = d.length;
  let lo = 0, hi = n - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (d[mid] <= f) lo = mid; else hi = mid - 1; }
  const i = lo, j = (i + 1) % n;
  const span = (j === 0 ? 1 : d[j]) - d[i];
  const u = span > 0 ? (f - d[i]) / span : 0;
  const [x1, y1] = tr.points[i], [x2, y2] = tr.points[j];
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
  // inward normal: rotate tangent towards the inside of the loop
  const nx = (-dy / len) * tr.inside, ny = (dx / len) * tr.inside;
  return { x: x1 + dx * u, y: y1 + dy * u, nx, ny };
}

function view(canvas, tr) {
  const pad = 30;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const sx = (w - 2 * pad) / (tr.maxX - tr.minX), sy = (h - 2 * pad) / (tr.maxY - tr.minY);
  const s = Math.min(sx, sy);
  const ox = (w - (tr.maxX - tr.minX) * s) / 2 - tr.minX * s;
  const oy = (h - (tr.maxY - tr.minY) * s) / 2 - tr.minY * s;
  return { s, map: (x, y) => [ox + x * s, oy + y * s] };
}

function drawTrackBase(ctx, tr, v) {
  const path = (from, to, off) => {
    ctx.beginPath();
    const steps = Math.max(2, Math.round((to - from) * 600));
    for (let k = 0; k <= steps; k++) {
      const p = pointAt(tr, from + (to - from) * (k / steps));
      const [x, y] = v.map(p.x + p.nx * off, p.y + p.ny * off);
      k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
  };
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  // pit lane, on the inside around the start line
  path(-0.035, 0.035, 55); ctx.strokeStyle = "#5b6378"; ctx.lineWidth = 4; ctx.stroke();
  // circuit
  path(0, 1, 0); ctx.strokeStyle = "#2a3042"; ctx.lineWidth = Math.max(10, 16 * v.s); ctx.stroke();
  path(0, 1, 0); ctx.strokeStyle = "#454d63"; ctx.lineWidth = Math.max(6, 12 * v.s); ctx.stroke();
  // DRS zones
  for (const [a, b] of tr.drs) {
    path(a, b < a ? b + 1 : b, 0); ctx.strokeStyle = "rgba(47,209,111,.55)"; ctx.lineWidth = 3; ctx.stroke();
  }
  // sector lines and start/finish
  const tick = (f, color, label) => {
    const p = pointAt(tr, f);
    const [x1, y1] = v.map(p.x + p.nx * 30, p.y + p.ny * 30);
    const [x2, y2] = v.map(p.x - p.nx * 30, p.y - p.ny * 30);
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.stroke();
    ctx.fillStyle = color; ctx.font = "bold 11px system-ui"; ctx.fillText(label, x2 + 4, y2 + 4);
  };
  tick(tr.sectors[0], "#ffd23f", "S2");
  tick(tr.sectors[1], "#ffd23f", "S3");
  tick(0, "#ffffff", "START");
}

function drawLoop() {
  const canvas = $("trackCanvas");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); S.trackCache = null;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (S.track) {
    const v = view(canvas, S.track);
    if (!S.trackCache) {
      const off = document.createElement("canvas");
      off.width = canvas.width; off.height = canvas.height;
      const octx = off.getContext("2d"); octx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawTrackBase(octx, S.track, v);
      S.trackCache = off;
    }
    ctx.drawImage(S.trackCache, 0, 0, w, h);
    if (S.state) drawCars(ctx, v);
  }
  requestAnimationFrame(drawLoop);
}

function drawCars(ctx, v) {
  const cars = [...S.state.cars].reverse();  // leader drawn on top
  const pitSlots = new Map();
  cars.forEach((c) => {
    if (c.retired) return;
    let target = c.track_pos;
    const prev = S.disp.has(c.code) ? S.disp.get(c.code) : target;
    let diff = target - prev; diff -= Math.round(diff);   // shortest way round the loop
    const shown = Math.abs(diff) > 0.2 ? target : prev + diff * 0.25;
    S.disp.set(c.code, shown);
    let p = pointAt(S.track, shown), off = 0;
    if (c.in_pit) {
      const slot = pitSlots.size; pitSlots.set(c.code, slot);
      p = pointAt(S.track, 0.005 + slot * 0.004); off = 55;
    }
    const [x, y] = v.map(p.x + p.nx * off, p.y + p.ny * off);
    ctx.beginPath(); ctx.arc(x, y, c.controlled ? 7 : 5.5, 0, Math.PI * 2);
    ctx.fillStyle = c.color; ctx.globalAlpha = c.finished ? 0.45 : 1; ctx.fill();
    ctx.lineWidth = c.controlled ? 2.5 : 1; ctx.strokeStyle = c.controlled ? "#fff" : "#000"; ctx.stroke();
    ctx.globalAlpha = 1;
    if (c.controlled || c.pos <= 3) {
      ctx.font = "bold 11px ui-monospace, monospace";
      const label = `${c.pos} ${c.code}`;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(7,8,12,.85)"; ctx.fillRect(x + 9, y - 16, tw + 8, 15);
      ctx.fillStyle = c.controlled ? "#fff" : "#cfd5e3"; ctx.fillText(label, x + 13, y - 5);
    }
  });
}

init().catch((e) => { console.error(e); $("startError").textContent = "Could not load race setup."; });
