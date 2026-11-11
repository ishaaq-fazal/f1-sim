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
    "Points are the F1 points your own team scores in your season (both cars). Ties go to the most wins, then 2nds, and so on. " +
    "Everyone gets the same weather in each round. ★ runs the league.";
  $("leagueRaceBtn").hidden = !me || me.finished;
  if (me && !me.finished) $("leagueRaceBtn").textContent = `Race round ${me.rounds_done + 1}`;
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

// Racing a league round is a normal season round: the season screen, pinned to the league's season.
async function raceLeagueRound() {
  const lg = S.league && S.league.league;
  if (!lg) return;
  S.focusSeason = lg.your_season_id;
  $("leagueDialog").close();
  openStart("season");
}

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
