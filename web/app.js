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
    if (r.status === 422) {
      msg = kind === "signup" ? "Use a valid email and a password of 10+ characters." : "Enter a valid email and password.";
      try {
        // our own password rules come back as "Value error, <reason>"
        const d = (await r.json()).detail;
        const rule = Array.isArray(d) && d.map((x) => String(x.msg || "")).find((m) => m.startsWith("Value error, "));
        if (kind === "signup" && rule) msg = rule.slice("Value error, ".length) + ".";
      } catch (_) { /* keep default */ }
    }
    else { try { msg = (await r.json()).detail || msg; } catch (_) { /* keep default */ } }
    $("authError").textContent = msg;
    return;
  }
  $("authPassword").value = "";
  $("authDialog").close();
  await loadMe();
  openMenu();
}

// mode: "quick" (single race), "season" or "career": decides which parts of the screen show.
async function openStart(mode) {
  if (!S.user) { $("authDialog").showModal(); return; }
  if (typeof mode === "string") S.mode = mode;
  await Promise.all([loadHistory(), loadSeason(), loadCareer()]);
  renderSeasonBox();
  renderCareerBox();
  const m = S.mode || "quick";
  $("seasonBox").hidden = m !== "season";
  $("careerBox").hidden = m !== "career";
  $("startTitle").textContent = { quick: "Quick race", season: "Season", career: "Career" }[m];
  // Seasons and careers set the circuit from their calendar; team, laps and weather here are
  // only used when starting a new one.
  $("trackPicker").closest("fieldset").hidden = m !== "quick";
  $("trackBlurb").hidden = m !== "quick";
  $("seedInput").closest("label").hidden = m !== "quick";
  $("historyBox").hidden = m !== "quick" || $("historyList").children.length === 0;
  $("startBtn").hidden = m !== "quick";
  $("startDialog").showModal();
  redrawOutlines();
}

function redrawOutlines() {
  [...$("trackPicker").querySelectorAll("canvas")].forEach((cv, i) => drawOutline(cv, S.setup.tracks[i].outline));
}

async function loadHistory() {
  const r = await api("/api/races");
  const list = $("historyList");
  const races = r.ok ? await r.json() : [];
  $("historyBox").hidden = races.length === 0;
  const teams = new Map((S.setup ? S.setup.teams : []).map((t) => [t.id, t.name]));
  const tracks = new Map((S.setup ? S.setup.tracks : []).map((t) => [t.id, t.name]));
  list.replaceChildren(...races.slice(0, 8).map((x) => {
    const li = el("li", null, `${new Date(x.created_at).toLocaleString()} · ${tracks.get(x.track) || x.track} · ${teams.get(x.team) || x.team} · ${x.laps} laps · ${x.summary} · seed ${x.seed}`);
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
    inp.addEventListener("change", buildTyrePicker);
    const dot = el("i"); dot.style.background = t.color;
    lab.append(inp, dot, el("span", null, t.name));
    picker.append(lab);
  });
  buildTrackPicker();
  buildTyrePicker();
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
  $("newRaceBtn").addEventListener("click", () => openMenu());
  $("backToMenuBtn").addEventListener("click", () => { $("startDialog").close(); openMenu(); });
  $("startForm").addEventListener("submit", (e) => { e.preventDefault(); startRace(); });
  $("againBtn").addEventListener("click", () => setTimeout(() => {
    if (S.seasonRace && S.seasonRace.career) openHQ();
    else openStart(S.seasonRace ? "season" : "quick");
  }, 0));
  $("hqBtn").addEventListener("click", openHQ);
  $("hqGoBtn").addEventListener("click", hqGo);
  $("hqStandingsBtn").addEventListener("click", async () => {
    await loadSeason(S.career.season_id); $("hqDialog").close(); openStandings();
  });
  $("hqQuitBtn").addEventListener("click", async () => {
    if (!confirm("Abandon this career? All its seasons will be deleted.")) return;
    await api(`/api/careers/${S.career.id}`, { method: "DELETE" });
    S.career = null; $("hqBtn").hidden = true; $("hqDialog").close(); openMenu();
  });
  $("standingsBtn").addEventListener("click", openStandings);
  $("resultStandingsBtn").addEventListener("click", () => { $("resultDialog").close(); openStandings(); });
  $("nextRoundBtn").addEventListener("click", () => {
    $("standingsDialog").close();
    if (S.mode === "career") openHQ(); else openStart("season");
  });
  $("standingsSeg").addEventListener("click", (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (v) { S.standingsTab = v; renderStandings(); }
  });
  window.addEventListener("resize", () => { S.trackCache = null; });
  initChart();
  await loadMe();
  if (S.user) openMenu(); else $("authDialog").showModal();
  requestAnimationFrame(drawLoop);
}

function setSpeed(v) {
  S.speed = v;
  for (const b of $("speedSeg").children) b.classList.toggle("on", Number(b.dataset.v) === v);
}

// ---------------------------------------------------------------- circuit + tyre pickers
const COMPOUND_INFO = { S: ["Soft", "#ff3b3b"], M: ["Medium", "#ffd23f"], H: ["Hard", "#f2f2f2"],
  I: ["Inter", "#3fbf4f"], W: ["Wet", "#2f8cff"] };
const rating = (x, labels, cuts) => labels[cuts.filter((c) => x >= c).length];

function drawOutline(canvas, pts) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 130, h = canvas.clientHeight || 70;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const s = Math.min((w - 10) / (Math.max(...xs) - minX), (h - 10) / (Math.max(...ys) - minY));
  const ox = (w - (Math.max(...xs) - minX) * s) / 2, oy = (h - (Math.max(...ys) - minY) * s) / 2;
  ctx.beginPath();
  pts.forEach(([x, y], i) => { const X = ox + (x - minX) * s, Y = oy + (y - minY) * s; i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  ctx.closePath(); ctx.strokeStyle = "#c9cfdd"; ctx.lineWidth = 2.5; ctx.lineJoin = "round"; ctx.stroke();
  const [sx, sy] = pts[0]; ctx.fillStyle = "#e10600";
  ctx.beginPath(); ctx.arc(ox + (sx - minX) * s, oy + (sy - minY) * s, 3.5, 0, Math.PI * 2); ctx.fill();
}

function buildTrackPicker() {
  const picker = $("trackPicker");
  let blurb = $("trackBlurb");
  if (!blurb) { blurb = el("p", "track-blurb"); blurb.id = "trackBlurb"; picker.after(blurb); }
  const showBlurb = () => {
    const t = S.setup.tracks.find((x) => x.id === selectedTrack());
    blurb.textContent = t ? t.blurb : "";
  };
  picker.replaceChildren(...S.setup.tracks.map((t, i) => {
    const lab = el("label");
    const inp = el("input"); inp.type = "radio"; inp.name = "track"; inp.value = t.id; inp.checked = i === 0;
    inp.addEventListener("change", showBlurb);
    const cv = el("canvas");
    const wear = rating(t.wear, ["Low", "Medium", "High", "Very high"], [0.85, 1.1, 1.3]);
    const pass = rating(t.overtaking, ["Very hard", "Hard", "Medium", "Easy"], [0.5, 0.9, 1.2]);
    lab.append(inp, cv, el("span", "tname", t.name),
      el("span", "tmeta", `${(t.length_m / 1000).toFixed(2)} km`),
      el("span", "tmeta", `Tyre wear: ${wear}`), el("span", "tmeta", `Overtaking: ${pass}`));
    return lab;
  }));
  showBlurb();
  // draw after layout so the canvases have a size
  requestAnimationFrame(redrawOutlines);
}

function selectedTrack() {
  const r = document.querySelector('input[name="track"]:checked');
  return r ? r.value : "harbourline";
}

S.startTyres = {};
function buildTyrePicker() {
  const teamId = document.querySelector('input[name="team"]:checked').value;
  const team = S.setup.teams.find((t) => t.id === teamId);
  const drivers = S.career && S.career.team === teamId ? S.career.drivers : team.drivers;
  S.startTyres = {};
  $("tyrePicker").replaceChildren(...drivers.map((d) => {
    const row = el("div", "tyre-row");
    row.append(el("span", "who", `${d.name} (${d.code})`));
    const auto = el("button", "boxbtn on", "Auto"); auto.type = "button";
    auto.title = "Let the strategy model choose";
    auto.style.color = "#8b93a7"; auto.style.borderColor = "#8b93a7"; auto.style.background = "#8b93a7";
    const buttons = [auto];
    for (const k of ["S", "M", "H", "I", "W"]) {
      const [name, col] = COMPOUND_INFO[k];
      const b = el("button", "boxbtn", name); b.type = "button"; b.dataset.k = k;
      b.style.color = col; b.style.borderColor = col;
      buttons.push(b);
    }
    const paint = () => buttons.forEach((b) => {
      const on = (b.dataset.k || null) === (S.startTyres[d.code] || null);
      b.classList.toggle("on", on);
      b.style.background = on ? b.style.borderColor : "transparent";
    });
    buttons.forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.k) S.startTyres[d.code] = b.dataset.k; else delete S.startTyres[d.code];
      paint();
    }));
    paint();
    row.append(...buttons);
    return row;
  }));
}

function startRace() {
  const team = document.querySelector('input[name="team"]:checked').value;
  const laps = Number($("lapsSelect").value);
  const seedRaw = $("seedInput").value.trim();
  const msg = { type: "start", team, laps, track: selectedTrack(), weather: $("weatherSelect").value };
  const own = new Set(S.setup.teams.find((t) => t.id === team).drivers.map((d) => d.code));
  const tyres = Object.fromEntries(Object.entries(S.startTyres).filter(([c]) => own.has(c)));
  if (Object.keys(tyres).length) msg.tyres = tyres;
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
    S.seasonRace = m.season || null;
    S.engPrev = null;
    Sound.startLights();
    $("trackName").dataset.round = S.seasonRace ? `Round ${S.seasonRace.round}/${S.seasonRace.rounds} · ` : "";
    S.disp.clear(); S.laps.clear(); S.charted.clear(); S.fastest = null; S.resultShown = false;
    S.paused = false; $("pauseBtn").textContent = "Pause"; $("pauseBtn").disabled = false;
    $("log").replaceChildren();
    $("trackName").textContent = `${$("trackName").dataset.round}${m.track.name} · ${(m.track.length_m / 1000).toFixed(2)} km`;
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
  raceSounds(st);
  S.fastest = st.fastest;
  renderHeader(st);
  renderTower(st);
  renderWall(st);
  if (st.laps.length) updateChart();
  if (st.finished && !S.resultShown) { S.resultShown = true; showResults(st); }
}

// Engine note follows your lead car's speed; effects for your pit stops, passes and the flag.
function raceSounds(st) {
  const mine = new Set(st.cars.filter((c) => c.controlled).map((c) => c.code));
  for (const ev of st.events) {
    if (ev.kind === "pit" && mine.has(ev.code)) Sound.pitStop();
    else if (ev.kind === "finish") Sound.chequered();
    else if (ev.kind === "overtake" && mine.has(ev.code)) Sound.overtake();
    else if (ev.kind === "radio" && mine.has(ev.code)) Sound.radio();
  }
  const car = st.cars.find((c) => c.controlled && !c.finished && !c.retired);
  if (!car || st.finished || st.paused || !S.track) { Sound.engine(0, false); return; }
  let mps = 0;
  const prev = S.engPrev;
  if (prev && prev.code === car.code && st.t > prev.t) {
    let d = car.track_pos - prev.pos; d -= Math.floor(d);          // laps covered, wrapping at the line
    if (d < 0.5) mps = (d * S.track.length_m) / (st.t - prev.t);
  }
  S.engPrev = { code: car.code, pos: car.track_pos, t: st.t };
  Sound.engine(car.in_pit ? 18 : mps, true);
}

function renderWeather(wx) {
  const box = $("weatherBox");
  if (!wx) { box.hidden = true; return; }
  box.hidden = false;
  const raining = wx.rain > 0.03;
  box.className = "weather" + (raining || wx.wetness > 0.1 ? " rain" : "");
  const sky = raining ? `${wx.rain_label[0].toUpperCase()}${wx.rain_label.slice(1)} rain` : "No rain";
  const fc = wx.forecast.map((f) => `${f.in_min}m ${f.chance}%`).join(" · ");
  box.replaceChildren(`${sky} · track ${Math.round(wx.wetness * 100)}% wet`, el("span", "fc", `rain chance ${fc}`));
  S.weather = wx;
}

function renderHeader(st) {
  renderWeather(st.weather);
  $("lapCounter").textContent = `LAP ${st.lap}/${st.total_laps}`;
  $("raceClock").textContent = fmtClock(st.t);
  const f = $("flag");
  if (st.chequered) { f.className = "flag chequered"; f.textContent = "FINISH"; }
  else if (st.vsc && st.weather && st.weather.rain > 0.03) { f.className = "flag vsc"; f.textContent = "VSC · RAIN"; }
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
  for (const k of ["S", "M", "H", "I", "W"]) {
    const b = el("button", "boxbtn", k === "I" ? "Inter" : S.compounds[k].name);
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
  else if (p && p.weather_call) {
    pred.append("Weather call: ");
    pred.append(el("b", "call", `box for ${S.compounds[p.weather_call].name}s, about ${p.gain_s}s a lap faster in these conditions`));
  } else if (p && p.stay_wet) {
    pred.append("Model's best plan: ");
    pred.append(el("b", null, `stay on ${S.compounds[c.tyre].name}s while the track is wet`));
  } else if (p) {
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

async function showResults(st) {
  const body = $("resultBody");
  const mine = st.cars.filter((c) => c.controlled).map((c) => `${c.code} P${c.pos}`).join(", ");
  $("resultLede").textContent = `Your team: ${mine}. Winner: ${st.cars[0].name}.`;
  const season = S.seasonRace;
  $("resultStandingsBtn").hidden = !season;
  $("againBtn").textContent = season && season.career ? "Team HQ"
    : season && season.round < season.rounds ? "Next round" : "New race";
  if (season) {
    const pts = st.cars.filter((c) => c.controlled).reduce((a, c) => a + racePoints(c), 0);
    await loadSeason(season.id);
    const mineTeam = S.season && S.season.teams.find((t) => t.team === S.season.season.team);
    $("resultLede").textContent += ` Round ${season.round} of ${season.rounds}: your team scored ${pts} point${pts === 1 ? "" : "s"}` +
      (mineTeam ? ` and is P${mineTeam.pos} in the teams' championship on ${mineTeam.points}.` : ".") +
      (season.career ? ` Prize money: $${(1 + 0.4 * pts).toFixed(1)}M.` : "");
  }
  body.replaceChildren(...st.cars.map((c) => {
    const tr = el("tr"); if (c.controlled) tr.classList.add("mine");
    const bar = el("td", "teambar"); const i = el("i"); i.style.background = c.color; bar.append(i);
    tr.append(el("td", null, String(c.pos)), bar, el("td", null, c.name),
      el("td", "r", c.pos === 1 ? "WINNER" : c.gap), el("td", "r", c.penalty ? `+${c.penalty}s pen` : ""),
      el("td", "r", `${c.pits} stop${c.pits === 1 ? "" : "s"}`));
    if (season) tr.append(el("td", "r pts", racePoints(c) ? `+${racePoints(c)}` : ""));
    return tr;
  }));
  $("resultDialog").showModal();
}

// ---------------------------------------------------------------- season
const POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1];
const racePoints = (c) => (c.retired ? 0 : POINTS[c.pos - 1] || 0);
const teamName = (id) => (S.setup.teams.find((t) => t.id === id) || { name: id }).name;
const teamColor = (id) => (S.setup.teams.find((t) => t.id === id) || { color: "#888" }).color;
const trackName = (id) => (S.setup.tracks.find((t) => t.id === id) || { name: id }).name;
S.standingsTab = "drivers";

// Loads the season to show: the given one, else your latest unfinished one, else your latest one.
async function loadSeason(id) {
  if (id === undefined) {
    const r = await api("/api/seasons");
    const list = r.ok ? await r.json() : [];
    const pick = list.find((x) => !x.finished) || list[0];
    id = pick ? pick.id : null;
  }
  S.season = null;
  if (id != null) {
    const r = await api(`/api/seasons/${id}`);
    if (r.ok) S.season = await r.json();
  }
  $("standingsBtn").hidden = !S.season;
  return S.season;
}

function myTeamStanding() {
  return S.season && S.season.teams.find((t) => t.team === S.season.season.team);
}

function renderSeasonBox() {
  const box = $("seasonBox");
  const sz = S.season && S.season.season;
  const text = el("div", "stext");
  const buttons = [];
  if (sz && !sz.finished) {
    const next = sz.rounds_done + 1;
    const mine = myTeamStanding();
    text.append(el("b", null, `Season with ${teamName(sz.team)}`),
      document.createTextNode(` · round ${next} of ${ROUNDS()} at ${trackName(S.season.rounds[next - 1].track)} · ${sz.laps} laps · ${sz.weather}` +
        (mine ? ` · teams' championship P${mine.pos} on ${mine.points} pts` : "")));
    const go = el("button", "btn primary", `Start round ${next}`); go.type = "button";
    go.addEventListener("click", () => startSeasonRound(sz.id));
    const table = el("button", "btn", "Standings"); table.type = "button";
    table.addEventListener("click", () => { $("startDialog").close(); openStandings(); });
    const quit = el("button", "btn ghost danger", "Abandon season"); quit.type = "button";
    quit.addEventListener("click", async () => {
      if (!confirm("Abandon this season? Its standings will be deleted.")) return;
      await api(`/api/seasons/${sz.id}`, { method: "DELETE" });
      await loadSeason(); renderSeasonBox();
    });
    buttons.push(go, table, quit);
    // your starting tyre picks below apply to the season's team
    const radio = document.querySelector(`input[name="team"][value="${sz.team}"]`);
    if (radio && !radio.checked) { radio.checked = true; buildTyrePicker(); }
  } else {
    text.append(el("b", null, "Season mode"),
      document.createTextNode(`: ${ROUNDS()} races, one at every circuit, scored with F1 points (25-18-15-12-10-8-6-4-2-1). ` +
        "Uses the team, laps and weather you pick below, and you keep that team all season."));
    const go = el("button", "btn", "Start a season"); go.type = "button";
    go.addEventListener("click", startSeason);
    buttons.push(go);
    if (sz) {
      const last = el("button", "btn ghost", "Last season's standings"); last.type = "button";
      last.addEventListener("click", () => { $("startDialog").close(); openStandings(); });
      buttons.push(last);
    }
  }
  box.replaceChildren(text, ...buttons);
}

async function startSeason() {
  const team = document.querySelector('input[name="team"]:checked').value;
  const body = { team, laps: Number($("lapsSelect").value), weather: $("weatherSelect").value };
  const r = await api("/api/seasons", { method: "POST", body });
  if (!r.ok) {
    let msg = "Could not start a season.";
    try { msg = (await r.json()).detail || msg; } catch (_) { /* keep default */ }
    $("startError").textContent = typeof msg === "string" ? msg : "Could not start a season.";
    return;
  }
  const sz = await r.json();
  await loadSeason(sz.id);
  startSeasonRound(sz.id);
}

function startSeasonRound(id) {
  const sz = S.season.season;
  // team, circuit, laps and weather come from the season on the server; this just names it
  const msg = { type: "start", team: sz.team, laps: sz.laps, season: id };
  if (Object.keys(S.startTyres).length) msg.tyres = { ...S.startTyres };
  connect(() => {
    S.ws.send(JSON.stringify(msg));
    $("startDialog").close();
  });
}

async function openStandings() {
  if (!S.season) await loadSeason();
  if (!S.season) return;
  renderStandings();
  $("standingsDialog").showModal();
}

function renderStandings() {
  const d = S.season, sz = d.season;
  for (const b of $("standingsSeg").children) b.classList.toggle("on", b.dataset.v === S.standingsTab);
  $("standingsLede").textContent = `${teamName(sz.team)} · ${sz.finished ? "season complete" : `after ${sz.rounds_done} of ${ROUNDS()} rounds`}` +
    ` · ${sz.laps} laps · ${sz.weather}`;
  $("standingsRules").textContent = `Points for P1 to P10: ${d.points_system.join(", ")}. Retirements score nothing. Ties go to the most wins, then most 2nd places.`;
  const nb = $("nextRoundBtn");
  nb.hidden = sz.finished;
  nb.textContent = `Go to round ${sz.rounds_done + 1}`;
  const head = (...cols) => { const tr = el("tr"); cols.forEach(([t, cls]) => tr.append(el("th", cls, t))); return tr; };
  const bar = (team) => { const td = el("td", "teambar"); const i = el("i"); i.style.background = teamColor(team); td.append(i); return td; };
  let rows = [];
  if (S.standingsTab === "drivers") {
    $("standingsHead").replaceChildren(head(["P"], [""], ["Driver"], ["Team"], ["Wins", "r"], ["Podiums", "r"], ["Pts", "r"]));
    rows = d.drivers.map((x) => {
      const tr = el("tr"); if (x.team === sz.team) tr.classList.add("mine");
      tr.append(el("td", null, String(x.pos)), bar(x.team), el("td", null, `${x.name} (${x.code})`),
        el("td", null, teamName(x.team)), el("td", "r", String(x.wins)), el("td", "r", String(x.podiums)),
        el("td", "r pts", String(x.points)));
      return tr;
    });
  } else if (S.standingsTab === "teams") {
    $("standingsHead").replaceChildren(head(["P"], [""], ["Team"], ["Wins", "r"], ["Pts", "r"]));
    rows = d.teams.map((x) => {
      const tr = el("tr"); if (x.team === sz.team) tr.classList.add("mine");
      tr.append(el("td", null, String(x.pos)), bar(x.team), el("td", null, x.name),
        el("td", "r", String(x.wins)), el("td", "r pts", String(x.points)));
      return tr;
    });
  } else {
    const names = new Map(d.drivers.map((x) => [x.code, x.name]));
    $("standingsHead").replaceChildren(head(["Rd"], ["Circuit"], ["Winner"], ["Your pts", "r"]));
    rows = d.rounds.map((x) => {
      const tr = el("tr");
      tr.classList.add(x.done ? "done" : x.round === sz.rounds_done + 1 ? "next" : "todo");
      tr.append(el("td", null, String(x.round)), el("td", null, trackName(x.track)),
        el("td", null, x.done ? (names.get(x.winner) || x.winner || "–") : x.round === sz.rounds_done + 1 ? "next" : ""),
        el("td", "r pts", x.done ? String(x.your_points) : ""));
      return tr;
    });
  }
  if (!rows.length) {
    const tr = el("tr"); const td = el("td", null, "No rounds raced yet."); td.colSpan = 7; tr.append(td); rows = [tr];
  }
  $("standingsBody").replaceChildren(...rows);
}

// ---------------------------------------------------------------- career
const money = (m) => `$${Number(m).toFixed(1)}M`;

async function loadCareer(id) {
  if (id === undefined) {
    const r = await api("/api/careers");
    const list = r.ok ? await r.json() : [];
    S.career = list[0] || null;
  } else {
    const r = await api(`/api/careers/${id}`);
    S.career = r.ok ? await r.json() : null;
  }
  $("hqBtn").hidden = !S.career;
  return S.career;
}

function renderCareerBox() {
  const box = $("careerBox");
  const c = S.career;
  const text = el("div", "stext");
  const buttons = [];
  if (c) {
    const where = c.season_finished ? "season over" : `round ${c.rounds_done + 1} of ${ROUNDS()} at ${trackName(track8(c.rounds_done + 1))}`;
    text.append(el("b", null, `Career with ${teamName(c.team)}`),
      document.createTextNode(` · season ${c.season_no} · ${where} · budget ${money(c.budget)}`));
    const hq = el("button", "btn primary", "Team HQ"); hq.type = "button";
    hq.addEventListener("click", () => { $("startDialog").close(); openHQ(); });
    buttons.push(hq);
    const radio = document.querySelector(`input[name="team"][value="${c.team}"]`);
    if (radio) { radio.checked = true; buildTyrePicker(); }
  } else {
    text.append(el("b", null, "Career mode"),
      document.createTextNode(": season after season with one team. Earn prize money, develop the car between races and sign drivers. Uses the team, laps and weather picked below."));
    const go = el("button", "btn", "Start a career"); go.type = "button";
    go.addEventListener("click", startCareer);
    buttons.push(go);
  }
  box.replaceChildren(text, ...buttons);
}

const ROUNDS = () => S.setup.schedule.length;
const track8 = (round) => S.setup.schedule[(round - 1) % ROUNDS()];

async function startCareer() {
  const team = document.querySelector('input[name="team"]:checked').value;
  const body = { team, laps: Number($("lapsSelect").value), weather: $("weatherSelect").value };
  const r = await api("/api/careers", { method: "POST", body });
  if (!r.ok) {
    let msg = "Could not start a career.";
    try { const d = (await r.json()).detail; if (typeof d === "string") msg = d; } catch (_) { /* keep */ }
    $("startError").textContent = msg;
    return;
  }
  S.career = await r.json();
  $("hqBtn").hidden = false;
  $("startDialog").close();
  openHQ();
}

async function openHQ() {
  if (!S.career) await loadCareer(); else await loadCareer(S.career.id);
  if (!S.career) return openStart("career");
  $("hqError").textContent = "";
  renderHQ();
  if (!$("hqDialog").open) $("hqDialog").showModal();
}

async function careerPost(path, body) {
  const r = await api(`/api/careers/${S.career.id}/${path}`, { method: "POST", body });
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    $("hqError").textContent = data && typeof data.detail === "string" ? data.detail : "That didn't work.";
    return;
  }
  $("hqError").textContent = "";
  S.career = data;
  renderHQ();
}

function renderHQ() {
  const c = S.career;
  const pre = c.rounds_done === 0 && !c.season_finished;
  $("hqLede").textContent = `${teamName(c.team)} · Season ${c.season_no} · ` +
    (c.season_finished ? "season complete" : `${c.rounds_done} of ${ROUNDS()} rounds raced`) + ` · ${c.laps} laps · ${c.weather}`;
  const stat = (label, value) => { const d = el("div", null, label); d.prepend(el("b", null, value)); return d; };
  const wages = c.drivers.reduce((a, d) => a + d.salary, 0);
  $("hqMoney").replaceChildren(stat("Budget", money(c.budget)), stat("Driver wages this season", money(wages)),
    stat("Prize money won", money(c.income)), stat("Spent on the car", money(c.spent.upgrades || 0)));

  $("hqUpgrades").replaceChildren(...c.upgrades.map((u) => {
    const tr = el("tr");
    const btn = el("button", "btn", u.next_cost == null ? "Maxed" : `Upgrade · ${money(u.next_cost)}`); btn.type = "button";
    btn.disabled = u.next_cost == null || u.next_cost > c.budget || c.season_finished;
    btn.addEventListener("click", () => careerPost("upgrade", { area: u.area }));
    tr.append(el("td", null, u.label), el("td", "lvl", `Lv ${u.level}`), el("td", "r", null));
    tr.lastChild.append(btn);
    return tr;
  }));

  $("hqDrivers").replaceChildren(...c.drivers.map((d) => {
    const tr = el("tr"); if (d.years <= 0) tr.classList.add("expired");
    const cell = el("td", "r");
    if (d.years <= 0 && pre) {
      for (const y of [1, 2, 3]) {
        const b = el("button", "btn", `Renew ${y}y`); b.type = "button";
        b.addEventListener("click", () => careerPost("renew", { code: d.code, years: y }));
        cell.append(b);
      }
    }
    tr.append(el("td", null, `${d.name} (${d.code})`), el("td", null, `Rating ${d.rating}`),
      el("td", null, `${money(d.salary)} a season`),
      el("td", null, d.years > 0 ? `${d.years} season${d.years === 1 ? "" : "s"} left` : "Contract over"), cell);
    return tr;
  }));

  $("hqMarketHint").textContent = pre ? "· sign before round 1; buying out a contract costs half its remaining wages"
    : "· opens again before the next season";
  $("hqMarket").replaceChildren(...c.market.map((m) => {
    const tr = el("tr");
    const cell = el("td", "r");
    if (pre) {
      const years = el("select");
      for (const y of [1, 2, 3]) { const o = el("option", null, `${y} season${y === 1 ? "" : "s"}`); o.value = y; years.append(o); }
      years.value = "2";
      cell.append(years);
      for (const seat of c.drivers) {
        const b = el("button", "btn", `Replace ${seat.code}`); b.type = "button";
        b.addEventListener("click", () => careerPost("sign", { code: m.code, replace: seat.code, years: Number(years.value) }));
        cell.append(b);
      }
    }
    tr.append(el("td", null, `${m.name} (${m.code})`), el("td", null, `Rating ${m.rating}`),
      el("td", null, `${money(m.salary)} a season`), cell);
    return tr;
  }));

  $("hqHistoryBox").hidden = c.history.length === 0;
  $("hqHistory").replaceChildren(...c.history.map((h) => {
    const tr = el("tr");
    tr.append(el("td", null, `Season ${h.season}`), el("td", null, `Teams' P${h.team_position}`),
      el("td", null, `${h.points} pts`), el("td", null, `Champion ${h.champion}`),
      el("td", "r", `+${money(h.prize)} prize, −${money(h.salaries)} wages`));
    return tr;
  }));

  const go = $("hqGoBtn");
  go.disabled = false;
  if (c.season_finished) go.textContent = `Start season ${c.season_no + 1}`;
  else go.textContent = `Start round ${c.rounds_done + 1} · ${trackName(track8(c.rounds_done + 1))}`;
  if (!c.ready && !c.season_finished) { go.disabled = true; $("hqError").textContent = c.blocker; }
}

async function hqGo() {
  const c = S.career;
  if (c.season_finished) return careerPost("next-season");
  // starting tyres: use the start screen's picker for this team's drivers
  const msg = { type: "start", team: c.team, laps: c.laps, season: c.season_id };
  const own = new Set(c.drivers.map((d) => d.code));
  const tyres = Object.fromEntries(Object.entries(S.startTyres).filter(([k]) => own.has(k)));
  if (Object.keys(tyres).length) msg.tyres = tyres;
  connect(() => { S.ws.send(JSON.stringify(msg)); $("hqDialog").close(); });
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
    if (S.weather) drawRain(ctx, w, h, S.weather);
    if (S.state) drawCars(ctx, v);
  }
  requestAnimationFrame(drawLoop);
}

// Wet track tint plus falling rain streaks, heavier with the rain intensity.
const drops = Array.from({ length: 220 }, () => [Math.random(), Math.random(), 0.6 + Math.random() * 0.8]);
function drawRain(ctx, w, h, wx) {
  if (wx.wetness > 0.02) { ctx.fillStyle = `rgba(60,120,220,${0.10 * wx.wetness})`; ctx.fillRect(0, 0, w, h); }
  if (wx.rain < 0.03) return;
  const n = Math.round(drops.length * Math.min(1, wx.rain));
  const t = performance.now() / 1000;
  ctx.strokeStyle = "rgba(170,200,255,0.35)"; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const [dx, dy, sp] = drops[i];
    const x = ((dx * w + t * 40 * sp) % w + w) % w, y = ((dy * h + t * 420 * sp) % h + h) % h;
    ctx.moveTo(x, y); ctx.lineTo(x - 3, y + 10);
  }
  ctx.stroke();
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
