// Race visuals: the cars on the track map, the lap time / gap-to-leader / position charts,
// and the lap-by-lap replay the server sends after the chequered flag.
// Loaded after app.js and uses its helpers ($, el, S, pointAt, renderTower, addLog, ...).
// DOM text is set with textContent only, like the rest of the front end.
"use strict";

S.chartMode = "lap";
S.hover = null;          // code of the car under the mouse on the track map
S.mapHits = [];          // [{code, x, y}] where each car was drawn, for hover and click
S.replay = null;         // replay data from the server once the race is over
S.rp = { active: false, t: 0, playing: true, speed: 30, last: 0, towerAt: 0, logCount: -1, cars: null, carsT: -1 };

const BATTLE_S = 1.0;    // cars this close to the car ahead get a ring on the map

// ---------------------------------------------------------------- small helpers
function hexLuma(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return 0.5;
  const n = parseInt(m[1], 16);
  return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
}
const numGap = (s) => (typeof s === "string" && /^\+\d+(\.\d+)?$/.test(s) ? parseFloat(s) : null);
const inDrs = (f) => S.track && S.track.drs.some(([a, b]) => (a <= b ? f >= a && f <= b : f >= a || f <= b));
const replayActive = () => S.rp.active;

function refreshTower() {
  if (replayActive()) renderReplayPanels(true);
  else if (S.state) renderTower(S.state);
}

// ---------------------------------------------------------------- per-lap table (live)
// Same rules as sim/replay.py: the leader's line-crossing time per lap, each car's gap to it,
// and the order cars crossed the line on each lap (index 0 = grid).
function lapTable() {
  if (S.replay) return { lapT: S.replay.lap_t, gaps: S.replay.gaps, positions: S.replay.positions };
  const crossings = new Map();
  for (const [code, laps] of S.laps) for (const l of laps) {
    if (l.t == null) continue;
    if (!crossings.has(l.lap)) crossings.set(l.lap, []);
    crossings.get(l.lap).push([l.t, code]);
  }
  const cars = S.state ? S.state.cars : [];
  const n = crossings.size ? Math.max(...crossings.keys()) : 0;
  const lapT = [], gaps = {}, positions = {};
  cars.forEach((c) => { gaps[c.code] = []; positions[c.code] = [c.grid]; });
  for (let k = 1; k <= n; k++) {
    const row = (crossings.get(k) || []).sort((a, b) => a[0] - b[0]);
    const lead = row.length ? row[0][0] : null;
    lapT.push(lead);
    const at = new Map(row.map(([t, code], i) => [code, [i + 1, t]]));
    cars.forEach((c) => {
      const hit = at.get(c.code);
      gaps[c.code].push(hit ? +(hit[1] - lead).toFixed(3) : null);
      positions[c.code].push(hit ? hit[0] : null);
    });
  }
  return { lapT, gaps, positions };
}

// ---------------------------------------------------------------- charts
let chart = null;

// Draws each charted driver's code at the end of their line, and the replay's current lap.
const chartDecor = {
  id: "raceDecor",
  afterDatasetsDraw(c) {
    const { ctx, chartArea: a, scales } = c;
    ctx.save();
    if (S.chartMode !== "lap") {
      ctx.font = "bold 10px ui-monospace, monospace"; ctx.textBaseline = "middle";
      c.data.datasets.forEach((ds, i) => {
        if (!ds.endLabel || !c.isDatasetVisible(i)) return;
        const meta = c.getDatasetMeta(i);
        for (let k = ds.data.length - 1; k >= 0; k--) {
          if (ds.data[k].y == null) continue;
          const p = meta.data[k];
          if (!p || p.x < a.left || p.y < a.top || p.y > a.bottom) break;
          ctx.fillStyle = ds.borderColor; ctx.fillText(ds.label, Math.min(p.x + 5, a.right - 24), p.y);
          break;
        }
      });
    }
    if (replayActive() && S.replay) {
      const x = scales.x.getPixelForValue(S.rp.lapF);
      if (x >= a.left && x <= a.right) {
        ctx.strokeStyle = "rgba(255,255,255,.55)"; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, a.top); ctx.lineTo(x, a.bottom); ctx.stroke();
      }
    }
    ctx.restore();
  },
};

function initChart() {
  if (!window.Chart) return;
  Chart.defaults.color = "#8b93a7";
  Chart.defaults.borderColor = "#262c3d";
  chart = new Chart($("lapChart"), {
    type: "line",
    data: { datasets: [] },
    plugins: [chartDecor],
    options: {
      animation: false, maintainAspectRatio: false, parsing: false, spanGaps: false,
      interaction: { mode: "nearest", intersect: false },
      layout: { padding: { right: 30 } },
      scales: {
        x: { type: "linear", title: { display: true, text: "Lap" }, ticks: { precision: 0 } },
        y: { title: { display: true, text: "Lap time (s)" } },
      },
      plugins: {
        legend: { labels: { boxWidth: 10 } },
        tooltip: { callbacks: { label: chartTooltip } },
      },
    },
  });
  $("chartSeg").addEventListener("click", (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (!v) return;
    S.chartMode = v;
    for (const b of $("chartSeg").children) b.classList.toggle("on", b.dataset.v === v);
    $("chartTitle").textContent = { lap: "Lap times", gap: "Gap to leader", pos: "Positions" }[v];
    updateChart();
  });
}

function chartTooltip(ctx) {
  const y = ctx.parsed.y;
  if (S.chartMode === "gap") return `${ctx.dataset.label}: ${y === 0 ? "leader" : `+${y.toFixed(1)}s`}`;
  if (S.chartMode === "pos") return `${ctx.dataset.label}: P${y}`;
  return `${ctx.dataset.label}: ${fmtLap(y)}`;
}

function resetChart() { if (chart) { chart.data.datasets = []; chart.update(); } }

// Colour per car; the second car of a team is dashed so team-mates can be told apart.
function carStyles() {
  const out = new Map(), seen = new Set();
  const cars = S.state ? S.state.cars : (S.replay ? S.replay.cars : []);
  [...cars].sort((a, b) => a.grid - b.grid).forEach((c) => {
    out.set(c.code, { color: c.color, dash: seen.has(c.team) ? [6, 4] : [] });
    seen.add(c.team);
  });
  return out;
}

function updateChart() {
  if (!chart || !(S.state || S.replay)) return;
  const styles = carStyles();
  const y = chart.options.scales.y;
  chart.options.plugins.legend.display = S.chartMode === "lap";
  if (S.chartMode === "lap") {
    const sets = [];
    let ys = [];
    for (const code of S.charted) {
      // Leave out lap 1 and pit laps so the scale shows racing pace.
      const pts = (S.laps.get(code) || []).map((l) => ({ x: l.lap, y: l.lap === 1 || l.pit ? null : l.time }));
      ys = ys.concat(pts.filter((p) => p.y != null).map((p) => p.y));
      const st = styles.get(code) || { color: "#888", dash: [] };
      sets.push({ label: code, data: pts, borderColor: st.color, backgroundColor: st.color, borderDash: st.dash,
        pointRadius: 2, borderWidth: 2, tension: 0.2 });
    }
    chart.data.datasets = sets;
    Object.assign(y, { reverse: false, title: { display: true, text: "Lap time (s)" }, ticks: {} });
    if (ys.length) {
      ys.sort((a, b) => a - b);
      const lo = ys[0], p90 = ys[Math.floor(ys.length * 0.9)];
      y.min = Math.floor(lo - 0.5);
      y.max = Math.ceil(Math.max(p90 + 1, lo + 3));
    } else { y.min = undefined; y.max = undefined; }
    chart.options.scales.x.min = undefined;
    chart.update();
    return;
  }
  // Gap and positions: every car, with the charted ones bold and labelled.
  const table = lapTable();
  const gap = S.chartMode === "gap";
  const sets = [];
  let top = 0;
  for (const [code, st] of styles) {
    const vals = gap ? table.gaps[code] : table.positions[code];
    if (!vals) continue;
    const on = S.charted.has(code);
    const data = vals.map((v, i) => ({ x: gap ? i + 1 : i, y: v }));
    if (gap && on) vals.forEach((v) => { if (v != null) top = Math.max(top, v); });
    sets.push({ label: code, data, endLabel: on, borderColor: on ? st.color : st.color + "40",
      backgroundColor: st.color, borderDash: st.dash, borderWidth: on ? 2.5 : 1.2,
      pointRadius: on ? 1.5 : 0, pointHoverRadius: 4, tension: 0, order: on ? 0 : 1 });
  }
  chart.data.datasets = sets;
  if (gap) {
    Object.assign(y, { reverse: true, min: 0, max: Math.max(5, Math.ceil(top * 1.1 + 1)),
      title: { display: true, text: "Seconds behind the leader" }, ticks: {} });
    chart.options.scales.x.min = 1;
  } else {
    const n = styles.size || 20;
    Object.assign(y, { reverse: true, min: 1, max: n, title: { display: true, text: "Position" },
      ticks: { stepSize: 1, autoSkip: false, callback: (v) => (v === 1 || v % 5 === 0 ? `P${v}` : "") } });
    chart.options.scales.x.min = 0;
  }
  chart.update();
}

// ---------------------------------------------------------------- track map: cars
// The cars shown on the map: the live state, or the replay at its current time.
function mapCars() {
  if (replayActive()) return replayCars();
  return S.state ? S.state.cars : [];
}

function drawCarsLayer(ctx, v, w, h) {
  const cars = mapCars();
  S.mapHits = [];
  if (!cars.length) return;
  const flag = replayActive() ? S.rp.flag : (S.state.sc ? "s" : S.state.vsc ? "v" : "g");
  if (flag === "s") drawSafetyCar(ctx, v, cars, !replayActive());
  drawCars(ctx, v, cars, !replayActive(), flag);
  if (replayActive()) {
    ctx.font = "bold 11px system-ui"; ctx.fillStyle = "rgba(225,6,0,.9)";
    ctx.fillRect(10, 10, 62, 20); ctx.fillStyle = "#fff"; ctx.fillText("REPLAY", 18, 24);
  }
  drawHoverCard(ctx, cars, w, h);
}

function drawSafetyCar(ctx, v, cars, live) {
  const lead = cars.find((c) => !c.retired && !c.in_pit && !c.finished);
  if (!lead || (live && !S.disp.has(lead.code))) return;
  const p = pointAt(S.track, ((live ? S.disp.get(lead.code) : lead.track_pos) + 0.012) % 1);
  const [x, y] = v.map(p.x, p.y);
  ctx.fillStyle = "#ffd400"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5;
  ctx.fillRect(x - 9, y - 6, 18, 12); ctx.strokeRect(x - 9, y - 6, 18, 12);
  ctx.fillStyle = "#000"; ctx.font = "bold 9px ui-monospace, monospace"; ctx.fillText("SC", x - 6, y + 3);
}

function drawCars(ctx, v, cars, smooth, flag) {
  const tr = S.track;
  const pitSlots = new Map();
  const drawn = [];
  // Where each car is: smoothed towards the server's position when live, exact in the replay.
  cars.forEach((c) => {
    if (c.retired) return;
    let f = c.track_pos;
    if (smooth) {
      const prev = S.disp.has(c.code) ? S.disp.get(c.code) : f;
      let diff = f - prev; diff -= Math.round(diff);          // shortest way round the loop
      f = Math.abs(diff) > 0.2 ? f : prev + diff * 0.25;
      f = ((f % 1) + 1) % 1;
      S.disp.set(c.code, f);
    }
    let p = pointAt(tr, f), off = 0;
    if (c.in_pit) {
      const slot = pitSlots.size; pitSlots.set(c.code, slot);
      p = pointAt(tr, 0.005 + slot * 0.004); off = 55;
    }
    const [x, y] = v.map(p.x + p.nx * off, p.y + p.ny * off);
    drawn.push({ c, f, x, y, nx: p.nx, ny: p.ny });
  });

  // Motion trails behind running cars.
  ctx.lineCap = "round";
  drawn.forEach(({ c, f }) => {
    if (c.in_pit || c.finished) return;
    const len = 0.018;
    for (let k = 0; k < 4; k++) {
      const a = f - len * (k + 1) / 4, b = f - len * k / 4;
      const pa = pointAt(tr, a), pb = pointAt(tr, b);
      const [x1, y1] = v.map(pa.x, pa.y), [x2, y2] = v.map(pb.x, pb.y);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.strokeStyle = c.color; ctx.globalAlpha = 0.45 * (1 - k / 4); ctx.lineWidth = 4 - k * 0.6; ctx.stroke();
    }
  });
  ctx.globalAlpha = 1;

  // Battles: within a second of the car ahead (green in a DRS zone). None behind a Safety Car.
  const now = performance.now() / 1000;
  if (flag === "g") drawn.forEach(({ c, f, x, y }) => {
    const g = numGap(c.interval);
    if (c.pos === 1 || c.in_pit || c.finished || g == null || g > BATTLE_S) return;
    const drs = inDrs(f);
    ctx.beginPath(); ctx.arc(x, y, 11 + 1.5 * Math.sin(now * 6), 0, Math.PI * 2);
    ctx.strokeStyle = drs ? "rgba(47,209,111,.9)" : "rgba(255,168,0,.85)"; ctx.lineWidth = 2; ctx.stroke();
  });

  // Cars, leader on top; each dot carries its position.
  [...drawn].sort((a, b) => b.c.pos - a.c.pos).forEach(({ c, x, y }) => {
    const r = c.controlled ? 8.5 : 7;
    ctx.globalAlpha = c.finished ? 0.55 : 1;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = c.color; ctx.fill();
    ctx.lineWidth = c.controlled ? 2.5 : 1.2; ctx.strokeStyle = c.controlled ? "#fff" : "#000"; ctx.stroke();
    if (S.hover === c.code) { ctx.beginPath(); ctx.arc(x, y, r + 4, 0, Math.PI * 2); ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke(); }
    ctx.fillStyle = hexLuma(c.color) > 0.55 ? "#000" : "#fff";
    ctx.font = `bold ${c.pos > 9 ? 8 : 9}px ui-monospace, monospace`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(String(c.pos), x, y + 0.5);
    ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
    ctx.globalAlpha = 1;
    S.mapHits.push({ code: c.code, x, y });
  });

  // Name tags: your cars always, then the leader, the hovered car and charted cars where they fit.
  const placed = [];
  const fits = (r) => !placed.some((q) => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h);
  const want = drawn.filter(({ c }) => c.controlled || c.pos === 1 || S.hover === c.code || S.charted.has(c.code));
  want.sort((a, b) => (b.c.controlled - a.c.controlled) || (a.c.pos - b.c.pos));
  ctx.font = "bold 11px ui-monospace, monospace";
  want.forEach(({ c, x, y }) => {
    const label = c.in_pit ? `${c.code} PIT` : c.code;
    const w = ctx.measureText(label).width + 8, h = 15;
    const spots = [[x + 11, y - 20], [x + 11, y + 6], [x - w - 11, y - 20], [x - w - 11, y + 6], [x - w / 2, y - 30], [x - w / 2, y + 14]];
    let spot = spots.find(([sx, sy]) => fits({ x: sx, y: sy, w, h }));
    if (!spot) { if (!c.controlled) return; spot = spots[0]; }
    const [sx, sy] = spot;
    placed.push({ x: sx, y: sy, w, h });
    ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(sx + (sx > x ? 0 : w), sy + h / 2); ctx.stroke();
    ctx.fillStyle = "rgba(7,8,12,.88)"; ctx.fillRect(sx, sy, w, h);
    ctx.fillStyle = c.color; ctx.fillRect(sx, sy, 3, h);
    ctx.fillStyle = c.controlled ? "#fff" : "#cfd5e3"; ctx.fillText(label, sx + 6, sy + 11);
  });
}

function drawHoverCard(ctx, cars, w, h) {
  if (!S.hover) return;
  const hit = S.mapHits.find((m) => m.code === S.hover);
  const c = cars.find((k) => k.code === S.hover);
  if (!hit || !c) return;
  const lines = [
    `P${c.pos}  ${c.name}`,
    c.pos === 1 ? "Leader" : `Gap ${c.gap}${c.interval && c.interval !== c.gap ? ` · int ${c.interval}` : ""}`,
    `${(S.compounds[c.tyre] || { name: c.tyre }).name} · ${c.tyre_age} laps · ${c.pits} stop${c.pits === 1 ? "" : "s"}`,
  ];
  ctx.font = "12px system-ui";
  const bw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 20, bh = 62;
  let bx = hit.x + 16, by = hit.y + 14;
  if (bx + bw > w - 4) bx = hit.x - bw - 16;
  if (by + bh > h - 4) by = hit.y - bh - 14;
  ctx.fillStyle = "rgba(11,13,18,.95)"; ctx.strokeStyle = c.color; ctx.lineWidth = 1.5;
  ctx.fillRect(bx, by, bw, bh); ctx.strokeRect(bx, by, bw, bh);
  ctx.fillStyle = "#fff"; ctx.font = "bold 12px system-ui"; ctx.fillText(lines[0], bx + 10, by + 18);
  ctx.fillStyle = "#cfd5e3"; ctx.font = "12px system-ui";
  ctx.fillText(lines[1], bx + 10, by + 36); ctx.fillText(lines[2], bx + 10, by + 52);
}

function carAtMouse(ev) {
  const r = $("trackCanvas").getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  let best = null, bd = 14 * 14;
  for (const m of S.mapHits) {
    const d = (m.x - mx) ** 2 + (m.y - my) ** 2;
    if (d < bd) { bd = d; best = m.code; }
  }
  return best;
}

// ---------------------------------------------------------------- replay
function onReplay(r) {
  const idx = new Map(r.cars.map((c, i) => [c.code, i]));
  // When each car took the flag, to order finishers as the classification does.
  const finalPos = new Map((S.state ? S.state.cars : []).map((c) => [c.code, c.pos]));
  r.meta = r.cars.map((c) => ({ ...c, controlled: c.team === S.team, final: finalPos.get(c.code) || 99 }));
  r.idx = idx;
  r.tEnd = r.frames.length ? r.frames[r.frames.length - 1][0] : 0;
  S.replay = r;
  $("replayBtn").hidden = false;
  $("mapReplayBtn").hidden = false;
  updateChart();
}

function frameAt(t) {
  const fr = S.replay.frames;
  let lo = 0, hi = fr.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (fr[mid][0] <= t) lo = mid; else hi = mid - 1; }
  const a = fr[lo], b = fr[Math.min(lo + 1, fr.length - 1)];
  const u = b[0] > a[0] ? Math.min(1, Math.max(0, (t - a[0]) / (b[0] - a[0]))) : 0;
  return { a, b, u };
}

// The replay at time t, shaped like the live state's cars so the map and tower can draw it.
function replayCars() {
  const r = S.replay, t = S.rp.t;
  if (S.rp.cars && S.rp.carsT === t) return S.rp.cars;
  const { a, b, u } = frameAt(t);
  S.rp.flag = a[3] || "g";
  const rows = r.meta.map((m, i) => {
    const st = u > 0.5 ? b[2][i] : a[2][i];
    const d = st === "x" || st === "f" ? (u > 0.5 ? b[1][i] : a[1][i]) : a[1][i] + (b[1][i] - a[1][i]) * u;
    const laps = r.laps[m.code];
    let k = 0;
    while (k < laps.length && laps[k][1] <= t) k++;
    const done = laps.slice(0, k);
    let lastPit = 0, pits = 0, best = null;
    done.forEach(([lap, , time, , pit]) => {
      if (pit) { pits++; lastPit = lap; }
      else if (lap > 1 && (best == null || time < best)) best = time;
    });
    const tyre = (laps[Math.min(k, laps.length - 1)] || [0, 0, 0, "M"])[3];
    return {
      code: m.code, name: m.name, team: m.team, color: m.color, grid: m.grid, controlled: m.controlled,
      d, k, track_pos: ((d % 1) + 1) % 1, lap: Math.max(0, Math.floor(d)),
      in_pit: st === "p", retired: st === "x", finished: st === "f", final: m.final,
      last: k ? done[k - 1][2] : null, best, tyre, tyre_age: k - lastPit, pits,
      pit_request: null, yielding: false,
    };
  });
  rows.sort((p, q) => {
    const g = (c) => (c.finished ? 0 : c.retired ? 2 : 1);
    if (g(p) !== g(q)) return g(p) - g(q);
    if (p.finished) return p.final - q.final;
    return q.d - p.d;
  });
  const lead = rows[0];
  rows.forEach((c, i) => {
    c.pos = i + 1;
    const ahead = rows[i - 1];
    c.gap = gapText(c, lead);
    c.interval = ahead ? gapText(c, ahead) : "";
  });
  S.rp.cars = rows; S.rp.carsT = t;
  return rows;
}

// Gap in seconds: from the line-crossing times once both cars have finished, otherwise from
// the distance between them at the car's own pace (as the live timing does), or laps down.
function gapText(c, ref) {
  if (c === ref) return "LEADER";
  if (c.retired) return "DNF";
  const L = S.replay.laps;
  if (c.finished && ref.finished) {
    const down = ref.k - c.k;
    if (down >= 1) return `+${down} LAP${down > 1 ? "S" : ""}`;
    return `+${Math.max(0, L[c.code][c.k - 1][1] - L[ref.code][ref.k - 1][1]).toFixed(1)}`;
  }
  const down = Math.floor(ref.d - c.d);
  if (down >= 1) return `+${down} LAP${down > 1 ? "S" : ""}`;
  if (c.k < 1 && ref.k < 1) return "–";
  const pace = c.best || c.last || 90;
  return `+${Math.max(0, (ref.d - c.d) * pace).toFixed(1)}`;
}

function startReplay() {
  if (!S.replay) return;
  if ($("resultDialog").open) $("resultDialog").close();
  S.rp.active = true; S.rp.t = 0; S.rp.playing = true; S.rp.last = performance.now(); S.rp.logCount = -1;
  S.rp.savedLog = [...$("log").children];
  $("replayBar").hidden = false; $("mapReplayBtn").hidden = true;
  document.querySelector(".wall-panel").classList.add("replaying");
  $("rpPlay").textContent = "Pause";
  renderReplayPanels(true);
}

function exitReplay() {
  if (!S.rp.active) return;
  S.rp.active = false; S.rp.cars = null;
  $("replayBar").hidden = true; $("mapReplayBtn").hidden = !S.replay;
  document.querySelector(".wall-panel").classList.remove("replaying");
  if (S.rp.savedLog) $("log").replaceChildren(...S.rp.savedLog);
  if (S.state) { renderHeader(S.state); renderTower(S.state); }
  updateChart();
}

function replaySeek(t) {
  S.rp.t = Math.max(0, Math.min(S.replay.tEnd, t));
  renderReplayPanels(true);
}

// Jump to the moment the leader started lap n (n = 1 is the start).
function replaySeekLap(n) {
  const lt = S.replay.lap_t;
  n = Math.max(1, Math.min(S.replay.total_laps, n));
  replaySeek(n <= 1 ? 0 : lt[n - 2] + 0.01);
}

function replayLeaderLap() {
  const lt = S.replay.lap_t;
  let n = 0;
  while (n < lt.length && lt[n] <= S.rp.t) n++;
  return n;     // laps the leader has completed
}

function tickReplay() {
  if (!S.rp.active) return;
  const now = performance.now();
  if (S.rp.playing) {
    S.rp.t += ((now - S.rp.last) / 1000) * S.rp.speed;
    if (S.rp.t >= S.replay.tEnd) { S.rp.t = S.replay.tEnd; S.rp.playing = false; $("rpPlay").textContent = "Play"; }
  }
  S.rp.last = now;
  if (now - S.rp.towerAt > 250) renderReplayPanels(false);
}

function renderReplayPanels(force) {
  const r = S.replay;
  S.rp.towerAt = performance.now();
  const cars = replayCars();
  const done = replayLeaderLap();
  const leadD = cars[0] ? cars[0].d : 0;
  S.rp.lapF = Math.max(0, Math.min(r.total_laps, cars[0] && cars[0].finished ? r.total_laps : leadD));
  const lap = Math.min(r.total_laps, done + 1);
  $("lapCounter").textContent = `LAP ${lap}/${r.total_laps}`;
  $("raceClock").textContent = fmtClock(S.rp.t);
  const f = $("flag");
  f.className = "flag " + (S.rp.flag === "g" ? "replay" : "vsc");
  f.textContent = S.rp.flag === "s" ? "REPLAY · SC" : S.rp.flag === "v" ? "REPLAY · VSC" : "REPLAY";
  $("rpLap").textContent = `Lap ${lap}/${r.total_laps}`;
  if (document.activeElement !== $("rpScrub")) $("rpScrub").value = String(Math.round((S.rp.t / (r.tEnd || 1)) * 1000));
  // Fastest lap so far.
  let fastest = null;
  for (const c of cars) if (c.best != null && (!fastest || c.best < fastest.time)) fastest = { code: c.code, time: c.best };
  renderTower({ cars, fastest });
  // Race control messages up to now.
  const evs = r.events.filter((e) => e.t <= S.rp.t);
  if (force || evs.length !== S.rp.logCount) {
    S.rp.logCount = evs.length;
    $("log").replaceChildren();
    evs.slice(-200).forEach(addLog);
  }
  if (chart) chart.draw();
}

// ---------------------------------------------------------------- wiring
(function wire() {
  const canvas = $("trackCanvas");
  canvas.addEventListener("mousemove", (ev) => {
    S.hover = carAtMouse(ev);
    canvas.style.cursor = S.hover ? "pointer" : "";
  });
  canvas.addEventListener("mouseleave", () => { S.hover = null; });
  canvas.addEventListener("click", (ev) => {
    const code = carAtMouse(ev);
    if (!code) return;
    S.charted.has(code) ? S.charted.delete(code) : S.charted.add(code);
    updateChart(); refreshTower();
  });
  $("replayBtn").addEventListener("click", startReplay);
  $("mapReplayBtn").addEventListener("click", startReplay);
  $("rpExit").addEventListener("click", exitReplay);
  $("rpPlay").addEventListener("click", () => {
    if (!S.rp.playing && S.rp.t >= S.replay.tEnd) S.rp.t = 0;
    S.rp.playing = !S.rp.playing; S.rp.last = performance.now();
    $("rpPlay").textContent = S.rp.playing ? "Pause" : "Play";
  });
  $("rpPrev").addEventListener("click", () => {
    const n = replayLeaderLap() + 1;
    // Within the first few seconds of a lap, go back one more.
    const start = n <= 1 ? 0 : S.replay.lap_t[n - 2];
    replaySeekLap(S.rp.t - start < 3 ? n - 1 : n);
  });
  $("rpNext").addEventListener("click", () => replaySeekLap(replayLeaderLap() + 2));
  $("rpScrub").addEventListener("input", (e) => replaySeek((Number(e.target.value) / 1000) * S.replay.tEnd));
  $("rpSpeed").addEventListener("click", (e) => {
    const v = e.target.dataset && e.target.dataset.v;
    if (!v) return;
    S.rp.speed = Number(v);
    for (const b of $("rpSpeed").children) b.classList.toggle("on", b.dataset.v === v);
  });
  (function loop() { tickReplay(); requestAnimationFrame(loop); })();
})();
