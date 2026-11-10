// Pit Wall sounds. Everything is synthesised with the Web Audio API (no audio files to load).
// Browsers only allow sound after the user has clicked something, so the audio starts on the
// first click. Settings are remembered in this browser only.
"use strict";

const Sound = (() => {
  const KEY = "pitwall.sound";
  const settings = { effects: true, engine: true, volume: 0.6 };
  try { Object.assign(settings, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (_) { /* defaults */ }

  let ctx = null, master = null, eng = null, noiseBuf = null;
  let engSpeed = 0;          // smoothed speed, m/s
  let engOn = false;

  function save() { try { localStorage.setItem(KEY, JSON.stringify(settings)); } catch (_) { /* private mode */ } }

  function unlock() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = settings.volume;
      master.connect(ctx.destination);
      noiseBuf = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
      const d = noiseBuf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    if (ctx.state === "suspended") ctx.resume();
  }

  // ------------------------------------------------------------ engine
  function buildEngine() {
    const out = ctx.createGain(); out.gain.value = 0;
    const filter = ctx.createBiquadFilter(); filter.type = "lowpass"; filter.Q.value = 6;
    const o1 = ctx.createOscillator(); o1.type = "sawtooth";
    const o2 = ctx.createOscillator(); o2.type = "square";
    const g2 = ctx.createGain(); g2.gain.value = 0.35;
    // a fast wobble gives the note its rasp
    const lfo = ctx.createOscillator(); lfo.frequency.value = 38;
    const lfoGain = ctx.createGain(); lfoGain.gain.value = 6;
    lfo.connect(lfoGain); lfoGain.connect(o1.frequency);
    o1.connect(filter); o2.connect(g2); g2.connect(filter); filter.connect(out); out.connect(master);
    o1.start(); o2.start(); lfo.start();
    return { out, filter, o1, o2 };
  }

  // Called with each race update: speed of your lead car in m/s, and whether it is moving.
  function engine(speedMps, running) {
    if (!ctx || ctx.state !== "running") return;
    const want = settings.engine && running;
    if (want && !eng) eng = buildEngine();
    if (!eng) return;
    engSpeed += (speedMps - engSpeed) * 0.35;
    const v = Math.max(0, Math.min(95, engSpeed));
    // 8 gears: revs climb through each gear and drop on the shift
    const span = 95 / 8;
    const rev = v < 2 ? 0.25 : 0.42 + 0.58 * ((v % span) / span);
    const f = 70 + 230 * rev;
    const now = ctx.currentTime;
    eng.o1.frequency.setTargetAtTime(f, now, 0.05);
    eng.o2.frequency.setTargetAtTime(f * 2, now, 0.05);
    eng.filter.frequency.setTargetAtTime(500 + 2600 * rev, now, 0.05);
    eng.out.gain.setTargetAtTime(want ? 0.05 + 0.07 * rev : 0, now, 0.15);
    engOn = want;
  }

  function stopEngine() {
    if (eng && ctx) eng.out.gain.setTargetAtTime(0, ctx.currentTime, 0.1);
    engOn = false; engSpeed = 0;
  }

  // ------------------------------------------------------------ effects
  function beep(freq, at, dur, gain = 0.25, type = "sine") {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.value = freq;
    g.gain.setValueAtTime(0, at);
    g.gain.linearRampToValueAtTime(gain, at + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    o.connect(g); g.connect(master); o.start(at); o.stop(at + dur + 0.02);
  }

  function noise(at, dur, { gain = 0.3, freq = 2000, q = 1, type = "bandpass", rattle = 0 } = {}) {
    const src = ctx.createBufferSource(); src.buffer = noiseBuf; src.loop = true;
    const f = ctx.createBiquadFilter(); f.type = type; f.frequency.value = freq; f.Q.value = q;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, at);
    g.gain.linearRampToValueAtTime(gain, at + 0.008);
    g.gain.setValueAtTime(gain, at + dur * 0.7);
    g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
    src.connect(f); f.connect(g);
    if (rattle) {   // the "brrrt" of a wheel gun: amplitude chopped at `rattle` Hz
      const am = ctx.createGain(); am.gain.value = 0.5;
      const lfo = ctx.createOscillator(); lfo.type = "square"; lfo.frequency.value = rattle;
      const depth = ctx.createGain(); depth.gain.value = 0.5;
      lfo.connect(depth); depth.connect(am.gain); g.connect(am); am.connect(master);
      lfo.start(at); lfo.stop(at + dur + 0.05);
    } else {
      g.connect(master);
    }
    src.start(at); src.stop(at + dur + 0.05);
  }

  const ok = () => ctx && ctx.state === "running" && settings.effects;

  // Five red lights, then lights out.
  function startLights() {
    if (!ok()) return;
    const t = ctx.currentTime + 0.05;
    for (let i = 0; i < 5; i++) beep(660, t + i * 0.35, 0.18, 0.18, "square");
    beep(990, t + 5 * 0.35 + 0.25, 0.5, 0.25, "square");
  }

  // Pit stop: jacks up, four wheel guns off and on, jack drops, limiter beeps away.
  function pitStop() {
    if (!ok()) return;
    const t = ctx.currentTime + 0.02;
    noise(t, 0.12, { gain: 0.5, freq: 180, type: "lowpass" });                       // front jack
    for (let i = 0; i < 4; i++) noise(t + 0.15 + i * 0.05, 0.28, { gain: 0.35, freq: 3200, q: 2, rattle: 55 });
    for (let i = 0; i < 4; i++) noise(t + 1.0 + i * 0.06, 0.32, { gain: 0.35, freq: 2900, q: 2, rattle: 60 });
    noise(t + 1.7, 0.15, { gain: 0.6, freq: 140, type: "lowpass" });                 // jack drop
    for (let i = 0; i < 3; i++) beep(1400, t + 1.95 + i * 0.12, 0.06, 0.12, "square");  // pit limiter
  }

  function chequered() {
    if (!ok()) return;
    const t = ctx.currentTime + 0.05;
    [523, 659, 784, 1047].forEach((f, i) => beep(f, t + i * 0.16, 0.5, 0.2, "triangle"));
    noise(t + 0.4, 2.2, { gain: 0.12, freq: 1200, q: 0.4 });                         // crowd
    stopEngine();
  }

  function overtake() {
    if (!ok()) return;
    const t = ctx.currentTime + 0.01;
    beep(880, t, 0.08, 0.12, "triangle"); beep(1320, t + 0.08, 0.12, 0.12, "triangle");
  }

  function radio() {
    if (!ok()) return;
    const t = ctx.currentTime + 0.01;
    noise(t, 0.08, { gain: 0.15, freq: 1800, q: 3 });
    beep(1200, t + 0.09, 0.07, 0.1, "square");
  }

  // ------------------------------------------------------------ settings
  function set(key, value) {
    settings[key] = value;
    if (master && key === "volume") master.gain.setTargetAtTime(value, ctx.currentTime, 0.05);
    if (key === "engine" && !value) stopEngine();
    save();
  }

  document.addEventListener("pointerdown", unlock, { capture: true });
  document.addEventListener("keydown", unlock, { capture: true });

  return { unlock, engine, stopEngine, startLights, pitStop, chequered, overtake, radio, set, settings,
           get engineOn() { return engOn; } };
})();
