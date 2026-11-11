// Online leagues: create or join with an invite code, see the shared table, race your league season.
// Uses helpers from app.js ($, el, S, api, teamName, trackName, openStart). textContent only.
"use strict";

S.league = null;           // the league open in the dialog (detail from the server), or null for the list

async function apiError(r, fallback) {
  try {
    const d = (await r.json()).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      const f = d.map((x) => (x.loc || []).slice(-1)[0]);
      if (f.includes("nickname")) return "Names are 2-20 letters, numbers, spaces, dots, dashes or underscores.";
      if (f.includes("name")) return "League names are 2-40 letters, numbers and simple punctuation.";
      if (f.includes("code")) return "Invite codes are 8 letters and numbers.";
    }
  } catch (_) { /* keep fallback */ }
  return fallback;
}

function fillTeamSelect(sel) {
  if (sel.options.length) return;
  for (const t of S.setup.teams) sel.append(new Option(t.name, t.id));
}

async function openLeagues(id) {
  if (!S.user) { $("authDialog").showModal(); return; }
  fillTeamSelect($("joinTeam")); fillTeamSelect($("newLeagueTeam"));
  $("leagueError").textContent = "";
  if (id != null) await showLeague(id); else await showLeagueList();
  if (!$("leagueDialog").open) $("leagueDialog").showModal();
}

async function showLeagueList() {
  S.league = null;
  const r = await api("/api/leagues");
  const leagues = r.ok ? await r.json() : [];
  const list = $("leagueList");
  list.replaceChildren();
  for (const lg of leagues) {
    const b = el("button", "season-box league-item"); b.type = "button";
    const text = el("div", "stext");
    text.append(el("b", null, lg.name),
      document.createTextNode(` · ${lg.members} player${lg.members === 1 ? "" : "s"} · ${lg.laps} laps · you are ${lg.your_nickname}` +
        (lg.is_owner ? " (owner)" : "")));
    b.append(text, el("span", "btn", "Open"));
    b.addEventListener("click", () => openLeagues(lg.id));
    list.append(b);
  }
  if (!leagues.length) list.append(el("p", "hint", "You're not in a league yet. Join one with a friend's invite code, or start your own and share its code."));
  $("leagueTitle").textContent = "Leagues";
  $("leagueLede").hidden = false;
  $("leagueList").hidden = false;
  $("leagueForms").hidden = false;
  $("leagueDetail").hidden = true;
  $("leagueLeaveBtn").hidden = $("leagueDeleteBtn").hidden = $("leagueRaceBtn").hidden = true;
  $("leagueBackBtn").textContent = "Back to menu";
}

async function showLeague(id) {
  const r = await api(`/api/leagues/${id}`);
  if (!r.ok) { await showLeagueList(); $("leagueError").textContent = "That league isn't available."; return; }
  const d = await r.json();
  S.league = d;
  const lg = d.league;
  $("leagueTitle").textContent = lg.name;
  $("leagueLede").hidden = true;
  $("leagueList").hidden = true;
  $("leagueForms").hidden = true;
  $("leagueDetail").hidden = false;
  $("leagueCode").textContent = lg.code;
  $("leagueCopied").textContent = "";
  const body = $("leagueTable");
  body.replaceChildren();
  for (const row of d.table) {
    const tr = el("tr", row.you ? "mine" : null);
    const who = el("td", null, row.nickname + (row.owner ? " ★" : ""));
    const team = el("td");
    const sw = el("span", "team-sw"); sw.style.background = teamColor(row.team);
    team.append(sw, document.createTextNode(row.team_name));
    tr.append(el("td", "r", row.pos), who, team,
      el("td", "r", row.finished ? "done" : `${row.rounds_done}/${d.rounds}`),
      el("td", "r", row.wins), el("td", "r", row.podiums),
      el("td", "r", row.best_finish ? `P${row.best_finish}` : "–"), el("td", "r pts", row.points));
    const act = el("td", "r");
    if (row.member_id != null) {
      const kick = el("button", "btn ghost danger small", "Remove"); kick.type = "button";
      kick.addEventListener("click", async () => {
        if (!confirm(`Remove ${row.nickname} from ${lg.name}? They keep their season.`)) return;
        const rr = await api(`/api/leagues/${lg.id}/members/${row.member_id}`, { method: "DELETE" });
        if (!rr.ok) $("leagueError").textContent = "Could not remove them.";
        await showLeague(lg.id);
      });
      act.append(kick);
    }
    tr.append(act);
    body.append(tr);
  }
  const me = d.table.find((x) => x.you);
  $("leagueRules").textContent = `${lg.laps} laps a race, ${d.rounds} rounds, one at every circuit. ` +
    "Every round is one race with all the players on the same grid; it starts when everyone has pressed Ready in the lobby. " +
    "Points are the F1 points your team scores (both cars). Ties go to the most wins, then 2nds, and so on. ★ runs the league.";
  $("leagueRaceBtn").hidden = !me || me.finished;
  if (me && !me.finished) $("leagueRaceBtn").textContent = `Race lobby: round ${me.rounds_done + 1}`;
  $("leagueLeaveBtn").hidden = false;
  $("leagueDeleteBtn").hidden = !lg.is_owner;
  $("leagueBackBtn").textContent = "All leagues";
}

async function createLeague() {
  $("leagueError").textContent = "";
  const body = { name: $("newLeagueName").value.trim(), laps: Number($("newLeagueLaps").value),
                 team: $("newLeagueTeam").value, nickname: $("newLeagueNick").value.trim() };
  const r = await api("/api/leagues", { method: "POST", body });
  if (!r.ok) { $("leagueError").textContent = await apiError(r, "Could not create the league."); return; }
  const lg = await r.json();
  $("newLeagueName").value = "";
  await showLeague(lg.id);
}

async function joinLeague() {
  $("leagueError").textContent = "";
  const body = { code: $("joinCode").value.trim().toUpperCase(), team: $("joinTeam").value,
                 nickname: $("joinNick").value.trim() };
  const r = await api("/api/leagues/join", { method: "POST", body });
  if (!r.ok) { $("leagueError").textContent = await apiError(r, "Could not join that league."); return; }
  const lg = await r.json();
  $("joinCode").value = "";
  await showLeague(lg.id);
}

// League rounds are one shared race: open the lobby, pick tyres, press Ready. The server starts
// the race when every player is ready and sends "init" to everyone at once.
async function raceLeagueRound() {
  const lg = S.league && S.league.league;
  if (!lg) return;
  S.focusSeason = lg.your_season_id;
  $("leagueDialog").close();
  openLobby(lg.id);
}

S.lobby = null;
S.lobbyTyres = {};

function openLobby(leagueId) {
  S.lobby = null; S.lobbyTyres = {};
  $("lobbyError").textContent = "";
  $("lobbyPlayers").replaceChildren(el("li", "hint", "Connecting…"));
  $("lobbyForecast").hidden = true;
  $("lobbyTyres").replaceChildren();
  $("lobbyReadyBtn").disabled = true;
  if (!$("lobbyDialog").open) $("lobbyDialog").showModal();
  connect(() => send({ type: "lobby", league: leagueId }));
}

function leaveLobby() {
  if (S.ws) { S.ws.close(); S.ws = null; }      // closing the connection takes you out of the lobby
  S.lobby = null;
}

function renderLobby(m) {
  const first = !S.lobby || S.lobby.league !== m.league || S.lobby.round !== m.round;
  S.lobby = m;
  if (!$("lobbyDialog").open) return;
  const me = m.players.find((p) => p.you);
  $("lobbyTitle").textContent = `${m.name} · round ${m.round}`;
  $("lobbyLede").textContent = m.track
    ? `Round ${m.round} of ${m.rounds} at ${trackName(m.track)}, ${m.laps} laps. You race ${me ? teamName(me.team) : ""}.`
    : "This league's season is over.";
  const list = $("lobbyPlayers");
  list.replaceChildren(...m.players.map((p) => {
    const li = el("li", p.ready ? "ready" : p.online ? "online" : "away");
    const sw = el("span", "team-sw"); sw.style.background = teamColor(p.team);
    li.append(sw, el("b", null, p.nickname + (p.you ? " (you)" : "")), el("span", "hint", teamName(p.team)),
      el("span", "lobby-state", p.ready ? "Ready" : p.online ? "In the lobby" : "Not here yet"));
    return li;
  }));
  const waiting = m.players.filter((p) => !p.ready).map((p) => p.nickname);
  let status = m.blocker || (m.status === "racing" ? "This round is already racing." : "");
  if (!status) status = waiting.length ? `Waiting for ${waiting.join(", ")}.` : "Everyone's ready: lights out!";
  $("lobbyRules").textContent = "Everyone races on one grid at the same time, each commanding their own team. " +
    "The race starts as soon as every player is ready. The league owner controls the race speed and pause. " + status;
  const ready = !!(me && me.ready);
  $("lobbyReadyBtn").textContent = ready ? "Not ready" : "Ready";
  $("lobbyReadyBtn").disabled = !me || !!m.blocker || m.status === "racing";
  $("lobbyTyresBox").disabled = ready;
  if (first && me) {
    buildLobbyTyres(me.team);
    if (m.your_season && m.track) lobbyForecast(m.your_season);
  }
}

function buildLobbyTyres(teamId) {
  const team = S.setup.teams.find((t) => t.id === teamId);
  $("lobbyTyres").replaceChildren(...team.drivers.map((d) => {
    const row = el("div", "tyre-row");
    row.append(el("span", "who", `${d.name} (${d.code})`));
    const sel = el("select");
    sel.append(new Option("Auto (strategy model)", ""));
    for (const k of ["S", "M", "H", "I", "W"]) sel.append(new Option(COMPOUND_INFO[k][0], k));
    sel.value = S.lobbyTyres[d.code] || "";
    sel.addEventListener("change", () => { if (sel.value) S.lobbyTyres[d.code] = sel.value; else delete S.lobbyTyres[d.code]; });
    row.append(sel);
    return row;
  }));
}

async function lobbyForecast(seasonId) {
  const r = await api(`/api/seasons/${seasonId}/next`);
  if (!r.ok) return;
  const f = (await r.json()).forecast;
  const box = $("lobbyForecast");
  const head = el("div", "fc-head");
  head.append(el("b", null, "Forecast"),
    el("span", `fc-summary ${f.wet_start ? "wet" : f.rain_chance >= 45 ? "maybe" : "dry"}`, f.summary));
  box.replaceChildren(head, el("div", "fc-row", `Chance of rain during the race: ${f.rain_chance}% · track ${Math.round(f.track_wetness * 100)}% wet`));
  box.hidden = false;
}

$("lobbyReadyBtn").addEventListener("click", () => {
  const me = S.lobby && S.lobby.players.find((p) => p.you);
  if (!me) return;
  $("lobbyError").textContent = "";
  const msg = { type: "ready", ready: !me.ready };
  if (!me.ready && Object.keys(S.lobbyTyres).length) msg.tyres = { ...S.lobbyTyres };
  send(msg);
});
$("lobbyBackBtn").addEventListener("click", () => {
  const id = S.lobby && S.lobby.league;
  leaveLobby(); $("lobbyDialog").close();
  if (id) openLeagues(id); else openMenu();
});
$("lobbyDialog").addEventListener("cancel", (e) => { e.preventDefault(); $("lobbyBackBtn").click(); });

$("newLeagueBtn").addEventListener("click", createLeague);
$("joinBtn").addEventListener("click", joinLeague);
$("joinCode").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); joinLeague(); } });
$("leagueRaceBtn").addEventListener("click", raceLeagueRound);
$("leagueBackBtn").addEventListener("click", () => {
  if (S.league) { showLeagueList(); return; }
  $("leagueDialog").close(); openMenu();
});
$("leagueCopyBtn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("leagueCode").textContent); $("leagueCopied").textContent = "Copied"; }
  catch (_) { $("leagueCopied").textContent = "Select the code and copy it"; }
});
$("leagueLeaveBtn").addEventListener("click", async () => {
  const lg = S.league && S.league.league;
  if (!lg || !confirm(`Leave ${lg.name}? Your season stays in your Season list.`)) return;
  await api(`/api/leagues/${lg.id}/leave`, { method: "POST" });
  if (S.focusSeason === lg.your_season_id) S.focusSeason = null;
  await showLeagueList();
});
$("leagueDeleteBtn").addEventListener("click", async () => {
  const lg = S.league && S.league.league;
  if (!lg || !confirm(`Delete ${lg.name} for everyone? Players keep their seasons.`)) return;
  await api(`/api/leagues/${lg.id}`, { method: "DELETE" });
  S.focusSeason = null;
  await showLeagueList();
});
$("leagueDialog").addEventListener("cancel", (e) => { e.preventDefault(); $("leagueDialog").close(); openMenu(); });
