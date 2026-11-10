// Main menu, sound settings and the guided tutorial. Uses helpers from app.js ($, el, S, openStart...).
"use strict";

// ---------------------------------------------------------------- main menu
function openMenu() {
  for (const id of ["startDialog", "resultDialog", "standingsDialog", "hqDialog"]) {
    if ($(id).open) $(id).close();
  }
  $("menuUser").textContent = S.user ? `Signed in as ${S.user.email}` : "";
  syncSoundControls();
  if (!$("menuDialog").open) $("menuDialog").showModal();
  // First visit in this browser: point at the tutorial.
  let seen = false;
  try { seen = localStorage.getItem("pitwall.tour") === "done"; } catch (_) { /* private mode */ }
  document.querySelector('.menu-card[data-mode="tutorial"]').classList.toggle("glow", !seen);
}

function chooseMode(mode) {
  $("menuDialog").close();
  if (mode === "tutorial") return startTutorial();
  if (mode === "career") {
    // straight to Team HQ when a career exists
    return loadCareer().then((c) => (c ? (S.mode = "career", openHQ()) : openStart("career")));
  }
  openStart(mode);
}

document.querySelectorAll(".menu-card").forEach((b) => b.addEventListener("click", () => chooseMode(b.dataset.mode)));
$("menuDialog").addEventListener("cancel", (e) => { if (!S.state) e.preventDefault(); });   // nothing behind it yet

// ---------------------------------------------------------------- sound settings
function syncSoundControls() {
  $("sndEffects").checked = Sound.settings.effects;
  $("sndEngine").checked = Sound.settings.engine;
  $("sndVolume").value = Sound.settings.volume;
  const on = Sound.settings.effects || Sound.settings.engine;
  $("soundBtn").textContent = on ? "Sound on" : "Sound off";
}
$("sndEffects").addEventListener("change", (e) => { Sound.set("effects", e.target.checked); syncSoundControls(); });
$("sndEngine").addEventListener("change", (e) => { Sound.set("engine", e.target.checked); syncSoundControls(); });
$("sndVolume").addEventListener("input", (e) => Sound.set("volume", Number(e.target.value)));
$("soundBtn").addEventListener("click", () => {
  const on = !(Sound.settings.effects || Sound.settings.engine);
  Sound.set("effects", on); Sound.set("engine", on);
  syncSoundControls();
});
syncSoundControls();

// ---------------------------------------------------------------- tutorial
const TOUR = [
  { sel: null, title: "Welcome to the pit wall",
    text: "This is a practice race, paused so we can look around. You run Aurora Racing's strategy: when to pit, which tyres, and how hard to push. The cars drive themselves." },
  { sel: ".track-panel", title: "The track map",
    text: "Every car, live. Yours have a white ring and a label. Green stretches are DRS zones where passing is easier; the grey line beside the start is the pit lane." },
  { sel: ".status", title: "Race status",
    text: "Flag (green, or yellow for a Virtual Safety Car), the lap counter, the race clock and the weather: rain now, how wet the track is and the chance of rain in the next 20 minutes." },
  { sel: "#speedSeg", title: "Race speed",
    text: "Watch at 1× or speed up to 50×. Pause any time to think. Your orders still work at any speed." },
  { sel: ".tower-panel", title: "Timing tower",
    text: "Positions, gap to the leader (or to the car ahead), last and best laps, the tyre each car is on with its age, and pit stops. Click a driver to add them to the lap chart." },
  { sel: "#wall .card", title: "Your driver",
    text: "Gap, lap times, tyre, wear and stops for each of your cars. The wear bar turns red as the tyre wears out; worn tyres lose a lot of time, and past the cliff they fall off fast." },
  { sel: "#wall .predict", title: "The strategy model",
    text: "The model's best plan for this car from here: when to stop and for which tyre. It updates as tyres wear and if rain comes. You can follow it or gamble." },
  { sel: "#wall .actions", title: "Your orders",
    text: "Box this lap for a tyre: Soft is fastest but wears quickest, Hard lasts longest, Inter and Wet are for rain. Click again to cancel. Pace: Push is faster but eats tyres, Conserve saves them." },
  { sel: ".chart-panel", title: "Lap times",
    text: "Lap times of the drivers you picked. Watch your times climb as the tyres wear: that's when a stop starts to pay off." },
  { sel: ".log-panel", title: "Race control",
    text: "Overtakes, pit stops, fastest laps, the safety car, retirements and weather news." },
  { sel: null, title: "Rules to remember",
    text: "In a dry race you must use two different dry tyres or take a 10 second penalty (using Inters or Wets waives that). A stop costs about 20 seconds, roughly half under the Virtual Safety Car. Press Finish and the race starts." },
];
let tourIdx = 0;

function startTutorial() {
  S.mode = "quick";
  const msg = { type: "start", team: "aurora", laps: 15, track: "harbourline", weather: "dry", seed: 7 };
  connect(() => {
    S.ws.send(JSON.stringify(msg));
    S.ws.send(JSON.stringify({ type: "pause", paused: true }));
  });
  // wait for the race screen to fill in, then start the tour
  const wait = setInterval(() => {
    if (S.state && document.querySelector("#wall .card")) {
      clearInterval(wait);
      S.paused = true; $("pauseBtn").textContent = "Resume";   // the race waits for the tour
      showTour(0);
    }
  }, 100);
  setTimeout(() => clearInterval(wait), 10000);
}

function showTour(i) {
  tourIdx = i;
  const step = TOUR[i];
  $("tour").hidden = false;
  $("tourStep").textContent = `${i + 1} of ${TOUR.length}`;
  $("tourTitle").textContent = step.title;
  $("tourText").textContent = step.text;
  $("tourBack").disabled = i === 0;
  $("tourNext").textContent = i === TOUR.length - 1 ? "Finish" : "Next";
  placeTour();
}

function placeTour() {
  const step = TOUR[tourIdx];
  const spot = $("tourSpot"), card = $("tourCard");
  const target = step.sel ? document.querySelector(step.sel) : null;
  const vw = window.innerWidth, vh = window.innerHeight;
  if (!target) {
    spot.style.cssText = `left:${vw / 2}px;top:${vh / 2}px;width:0;height:0`;
    card.style.left = `${Math.max(16, vw / 2 - card.offsetWidth / 2)}px`;
    card.style.top = `${Math.max(16, vh / 2 - card.offsetHeight / 2)}px`;
    return;
  }
  target.scrollIntoView({ block: "nearest", behavior: "instant" });
  const r = target.getBoundingClientRect(), pad = 6;
  spot.style.cssText = `left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + pad * 2}px;height:${r.height + pad * 2}px`;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  let top = r.bottom + 14;
  if (top + ch > vh - 16) top = r.top - ch - 14;          // no room below: go above
  if (top < 16) top = Math.min(vh - ch - 16, Math.max(16, r.top + 16));   // big target: sit inside it
  const left = Math.min(vw - cw - 16, Math.max(16, r.left));
  card.style.left = `${left}px`; card.style.top = `${top}px`;
}

function endTour() {
  $("tour").hidden = true;
  try { localStorage.setItem("pitwall.tour", "done"); } catch (_) { /* private mode */ }
  S.paused = false; send({ type: "pause", paused: false }); $("pauseBtn").textContent = "Pause";
}

$("tourNext").addEventListener("click", () => (tourIdx >= TOUR.length - 1 ? endTour() : showTour(tourIdx + 1)));
$("tourBack").addEventListener("click", () => tourIdx > 0 && showTour(tourIdx - 1));
$("tourSkip").addEventListener("click", endTour);
window.addEventListener("resize", () => { if (!$("tour").hidden) placeTour(); });
document.addEventListener("keydown", (e) => {
  if ($("tour").hidden) return;
  if (e.key === "Escape") endTour();
  else if (e.key === "ArrowRight" || e.key === "Enter") $("tourNext").click();
  else if (e.key === "ArrowLeft") $("tourBack").click();
});
