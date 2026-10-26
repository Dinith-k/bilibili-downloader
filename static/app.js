"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = id => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const sleep = ms => new Promise(r => setTimeout(r, ms));

// tiny DOM builder (no innerHTML)
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "value" || k === "disabled") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid);
  return e;
}

const fmtT = s => {
  s = Math.round(s || 0);
  const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (hh ? hh + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
};
const fmtBytes = n => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};
const fmtEta = s => s == null ? "--" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtDate = d => d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : "";

async function api(path, { method = "GET", body } = {}) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Request failed");
  return data;
}

const lsGet = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } };

const setMsg = (el, text, cls = "") => { el.textContent = text; el.className = cls; };

/* ------------------------------------------------------------------ tabs */
const tabLoaders = {};
function showTab(name) {
  for (const b of document.querySelectorAll("#tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const t of document.querySelectorAll(".tab")) t.hidden = t.id !== "tab-" + name;
  lsSet("bili-tab", name);
  tabLoaders[name]?.();
}
$("tabs").addEventListener("click", e => { if (e.target.dataset.tab) showTab(e.target.dataset.tab); });

/* ------------------------------------------------------------------ crop widget
   box = {x, y, w, h} as fractions of the video frame. */
const PRESETS = "bili-presets";

function makeCrop(host, { label = null, persistKey = null, region = false } = {}) {
  const root = $("tpl-crop").content.firstElementChild.cloneNode(true);
  host.append(root);
  const r = role => root.querySelector(`[data-role="${role}"]`);
  if (label) r("label").textContent = label;
  if (!window.HAS_FFMPEG) r("label").textContent += " (needs ffmpeg)";
  const S = { VW: 16, VH: 9, box: { x: .25, y: .25, w: .5, h: .5 }, drag: null, fallback: "" };
  const MIN = .05;
  const REGION = region;  // caption-strip mode: always full width, moves and resizes vertically only
  if (REGION) {
    root.classList.add("region");
    r("presets").closest(".row").hidden = true;
    r("ratio").closest(".trow").hidden = true;
    root.querySelector("label.chk").hidden = true;
    r("on").checked = true; r("body").hidden = false;
    r("ratio").value = "free";
    S.box = { x: 0, y: .84, w: 1, h: .16 };
  }

  // width/height in pixels the crop must keep, or null for free-form
  function ratioVal() {
    const v = r("ratio").value, V = S.VW / S.VH;
    if (v === "free") return null;
    if (v === "orig") return V;
    if (v === "custom") { const a = +r("cw").value, b = +r("ch").value; return a > 0 && b > 0 ? a / b : V; }
    const [a, b] = v.split(":"); return a / b;
  }
  const maxBox = R => { const V = S.VW / S.VH; return R >= V ? { w: 1, h: V / R } : { w: R / V, h: 1 }; };

  function layout() {
    const R = ratioVal();
    if (R != null) {
      const m = maxBox(R), s = r("size").value / 100, b = S.box;
      const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
      b.w = m.w * s; b.h = m.h * s;
      b.x = clamp(cx - b.w / 2, 0, 1 - b.w); b.y = clamp(cy - b.h / 2, 0, 1 - b.h);
    }
    render();
  }
  function render() {
    const free = ratioVal() == null, b = S.box, c = r("cbox").style;
    r("stage").classList.toggle("free", free);
    r("sizeRow").hidden = free;
    c.left = b.x * 100 + "%"; c.top = b.y * 100 + "%"; c.width = b.w * 100 + "%"; c.height = b.h * 100 + "%";
    r("sizeT").textContent = r("size").value + "%";
    const pw = Math.floor(S.VW * b.w / 2) * 2, ph = Math.floor(S.VH * b.h / 2) * 2;
    r("info").textContent = REGION
      ? `Caption strip: from ${Math.round(b.y * 100)}% to ${Math.round((b.y + b.h) * 100)}% of the picture height.`
      : `Output about ${pw} x ${ph} px at full quality. Drag the box to move it, drag the dots to resize.`;
  }
  function syncSize() {
    const R = ratioVal();
    if (R != null) r("size").value = Math.round(clamp(S.box.w / maxBox(R).w * 100, 10, 100));
  }

  // px, py = pointer position as fractions of the frame
  function resizeBox(hd, b, px, py) {
    if (REGION) {  // only the top and bottom edges of the strip can move
      if (!/[ns]/.test(hd)) return;
      hd = hd.includes("n") ? "n" : "s";
    }
    px = clamp(px, 0, 1); py = clamp(py, 0, 1);
    const R = ratioVal();
    if (R == null) {
      let x1 = b.x, y1 = b.y, x2 = b.x + b.w, y2 = b.y + b.h;
      if (hd.includes("w")) x1 = clamp(px, 0, x2 - MIN);
      if (hd.includes("e")) x2 = clamp(px, x1 + MIN, 1);
      if (hd.includes("n")) y1 = clamp(py, 0, y2 - MIN);
      if (hd.includes("s")) y2 = clamp(py, y1 + MIN, 1);
      S.box = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
      return;
    }
    // locked ratio: the opposite corner stays put
    const right = hd.includes("e"), down = hd.includes("s");
    const ax = right ? b.x : b.x + b.w, ay = down ? b.y : b.y + b.h;
    const q = R / (S.VW / S.VH);  // w/h as frame fractions
    let fw = Math.abs(px - ax), fh = Math.abs(py - ay);
    if (fh * q < fw) fw = fh * q; else fh = fw / q;
    if (fw < MIN) { fw = MIN; fh = fw / q; }
    const maxW = right ? 1 - ax : ax, maxH = down ? 1 - ay : ay;
    if (fw > maxW) { fw = maxW; fh = fw / q; }
    if (fh > maxH) { fh = maxH; fw = fh * q; }
    S.box = { x: right ? ax : ax - fw, y: down ? ay : ay - fh, w: fw, h: fh };
    syncSize();
  }

  const cbox = r("cbox");
  cbox.addEventListener("pointerdown", e => {
    e.preventDefault();
    cbox.setPointerCapture(e.pointerId);
    S.drag = { hd: e.target.dataset.h || "move", sx: e.clientX, sy: e.clientY, b: { ...S.box }, rect: r("stage").getBoundingClientRect() };
  });
  cbox.addEventListener("pointermove", e => {
    const d = S.drag; if (!d) return;
    if (d.hd === "move") {
      if (!REGION) S.box.x = clamp(d.b.x + (e.clientX - d.sx) / d.rect.width, 0, 1 - d.b.w);
      S.box.y = clamp(d.b.y + (e.clientY - d.sy) / d.rect.height, 0, 1 - d.b.h);
    } else {
      resizeBox(d.hd, d.b, (e.clientX - d.rect.left) / d.rect.width, (e.clientY - d.rect.top) / d.rect.height);
    }
    render();
  });
  for (const ev of ["pointerup", "pointercancel"]) cbox.addEventListener(ev, () => { if (S.drag) { S.drag = null; persist(); } });

  /* ---- state in / out ---- */
  const get = () => ({
    on: r("on").checked, ratio: r("ratio").value, cw: +r("cw").value, ch: +r("ch").value,
    size: +r("size").value, box: { ...S.box },
  });
  function set(s) {
    if (!s) return;
    if (!REGION) {
      r("on").checked = !!(s.on ?? s.cropOn);
      r("body").hidden = !r("on").checked;
    }
    if ([...r("ratio").options].some(o => o.value === s.ratio)) r("ratio").value = s.ratio;
    r("cw").value = s.cw || 16; r("ch").value = s.ch || 9;
    r("custom").hidden = r("ratio").value !== "custom";
    r("size").value = s.size || 100;
    if (s.box) {
      const w = clamp(+s.box.w || .5, MIN, 1), hh = clamp(+s.box.h || .5, MIN, 1);
      S.box = { w, h: hh, x: clamp(+s.box.x || 0, 0, 1 - w), y: clamp(+s.box.y || 0, 0, 1 - hh) };
    }
    if (REGION) { S.box.x = 0; S.box.w = 1; }
    layout();
  }
  function persist() { if (persistKey) lsSet(persistKey, get()); }

  // what the server needs: a box (free) or a ratio recipe that is resolved per video
  function spec() {
    const R = ratioVal(), b = S.box;
    if (R == null) return { ...b };
    return { ratio: r("ratio").value === "orig" ? "orig" : R, size: r("size").value / 100, cx: b.x + b.w / 2, cy: b.y + b.h / 2 };
  }

  function setImage(src, msg = "") {
    const img = r("img");
    S.usedFallback = false;
    r("msg").textContent = msg;
    img.onload = () => { r("msg").textContent = ""; };
    img.onerror = () => {
      r("msg").textContent = S.fallback && !S.usedFallback ? "Live frame unavailable - showing the cover image." : "Preview image unavailable.";
      if (S.fallback && !S.usedFallback) { S.usedFallback = true; img.src = S.fallback; }
    };
    img.src = src || "";
  }
  function setFrame({ width, height, src, fallback = "", msg = "" }) {
    S.VW = width || 16; S.VH = height || 9; S.fallback = fallback;
    r("stage").style.aspectRatio = `${S.VW} / ${S.VH}`;
    r("stage").style.maxWidth = `min(640px, ${(60 * S.VW / S.VH).toFixed(1)}vh)`;  // keep tall (vertical) videos on screen
    setImage(src, msg);
    layout();
  }

  /* ---- controls ---- */
  r("on").addEventListener("change", () => { r("body").hidden = !r("on").checked; persist(); });
  r("ratio").addEventListener("change", () => { r("custom").hidden = r("ratio").value !== "custom"; layout(); persist(); });
  for (const role of ["cw", "ch", "size"]) r(role).addEventListener("input", () => { layout(); persist(); });

  /* ---- named presets (browser storage, shared by every crop widget) ---- */
  function refreshPresets(selected = "") {
    const names = Object.keys(lsGet(PRESETS, {}));
    r("presets").replaceChildren(...[["", names.length ? "Saved crops..." : "No saved crops yet"], ...names.map(n => [n, n])]
      .map(([v, text]) => h("option", { value: v, text })));
    r("presets").value = selected;
  }
  r("presets").addEventListener("change", () => {
    const p = r("presets").value;
    if (p) { set({ ...lsGet(PRESETS, {})[p], on: true }); persist(); }
  });
  r("savePreset").addEventListener("click", () => {
    const name = (prompt("Name for these crop settings (e.g. Shorts 9:16):", r("presets").value || "") || "").trim();
    if (!name) return;
    const all = lsGet(PRESETS, {}); all[name] = { ...get(), on: true };
    lsSet(PRESETS, all); refreshPresets(name);
  });
  r("delPreset").addEventListener("click", () => {
    const name = r("presets").value; if (!name) return;
    const all = lsGet(PRESETS, {}); delete all[name]; lsSet(PRESETS, all); refreshPresets();
  });
  refreshPresets();
  layout();

  return { root, get, set, spec, setFrame, setImage, isOn: () => r("on").checked };
}

/* ------------------------------------------------------------------ background-music option (+ 15 s preview) */
function makeMusic(host, { getUrl, getTime }) {
  const root = $("tpl-music").content.firstElementChild.cloneNode(true);
  host.append(root);
  const r = role => root.querySelector(`[data-role="${role}"]`);
  // `wanted` is the saved choice. It survives a moment when the tool looks unavailable, so saving never drops it.
  let available = false, runId = 0, wanted = false;

  function setAvailable(ok) {
    available = !!ok;
    r("on").disabled = !available;
    r("on").checked = available && wanted;
    r("tryBox").hidden = !available;
    r("note").textContent = available
      ? "Runs an AI model on your PC, so it adds a lot of time to each download. Use the preview to check the result first."
      : "Not available right now: the AI audio component is still being set up (your choice is kept).";
  }
  r("on").addEventListener("change", () => { wanted = r("on").checked; });

  r("try").addEventListener("click", async () => {
    const url = getUrl().trim();
    if (!url) { r("status").textContent = "Add a video link first."; return; }
    const mine = ++runId;
    r("try").disabled = true; r("players").hidden = true; r("status").textContent = "Starting...";
    try {
      const { id } = await api("/api/music-preview", { method: "POST", body: { url, t: getTime() } });
      while (mine === runId) {
        const p = await api("/api/music-preview/" + id);
        if (p.status === "error") throw new Error(p.error);
        if (p.status === "done") {
          r("orig").src = `/api/music-preview/${id}/audio/original`;
          r("clean").src = `/api/music-preview/${id}/audio/clean`;
          r("players").hidden = false; r("status").textContent = "Ready. Play both clips and compare."; break;
        }
        r("status").textContent = `${p.stage}${p.percent ? " " + p.percent.toFixed(0) + "%" : ""}${p.eta ? " · about " + fmtEta(p.eta) + " left" : ""}`;
        await sleep(1000);
      }
    } catch (err) { r("status").textContent = err.message; }
    r("try").disabled = false;
  });

  setAvailable(false);
  return { root, setAvailable, isOn: () => (available ? r("on").checked : wanted),
           setOn: v => { wanted = !!v; r("on").checked = wanted && available; } };
}

/* ------------------------------------------------------------------ Chinese -> English captions (+ one-frame test) */
function makeCaptions(host, { getUrl, getTime }) {
  const root = $("tpl-caps").content.firstElementChild.cloneNode(true);
  host.append(root);
  const r = role => root.querySelector(`[data-role="${role}"]`);
  const strip = makeCrop(r("regionHost"), { region: true });
  let available = false, runId = 0, wanted = false;  // `wanted` = the saved choice, kept even if the tool looks unavailable
  const band = () => { const b = strip.get().box; return { y0: b.y, y1: b.y + b.h }; };

  function setAvailable(ok) {
    available = !!ok;
    r("on").disabled = !available;
    r("on").checked = available && wanted;
    r("box").hidden = !r("on").checked;
    r("note").textContent = available
      ? "Reads the Chinese captions on screen, translates them and draws English ones. It adds processing time, and the translation is automatic, so check the .srt file saved next to each video."
      : "Not available right now: the caption tools are still being set up (your choice is kept).";
  }
  r("on").addEventListener("change", () => { wanted = r("on").checked; r("box").hidden = !wanted; });

  r("try").addEventListener("click", async () => {
    const url = getUrl().trim();
    if (!url) { r("status").textContent = "Add a video link first."; return; }
    const mine = ++runId, { y0, y1 } = band();
    r("try").disabled = true; r("result").hidden = true; r("status").textContent = "Starting...";
    try {
      const { id } = await api("/api/captions-preview", { method: "POST", body: { url, t: getTime(), y0, y1, auto: r("auto").checked } });
      while (mine === runId) {
        const p = await api("/api/captions-preview/" + id);
        if (p.status === "error") throw new Error(p.error);
        if (p.status === "done") {
          if (!p.found) { r("status").textContent = "No Chinese captions found in that strip on this frame. Try another moment or move the strip."; break; }
          r("resImg").src = `/api/captions-preview/${id}/image`; r("zh").textContent = p.zh; r("en").textContent = p.en;
          r("result").hidden = false; r("status").textContent = "Done."; break;
        }
        r("status").textContent = `${p.stage}${p.percent ? " " + p.percent.toFixed(0) + "%" : ""}`;
        await sleep(1000);
      }
    } catch (err) { r("status").textContent = err.message; }
    r("try").disabled = false;
  });

  setAvailable(false);
  return {
    root, setAvailable,
    setFrame: f => strip.setFrame(f), setImage: (src, msg) => strip.setImage(src, msg),
    isOn: () => (available ? r("on").checked : wanted),
    get: () => ({ on: available ? r("on").checked : wanted, auto: r("auto").checked, region: band(), ui: strip.get() }),
    set: c => {
      wanted = !!c?.on; r("on").checked = wanted && available; r("box").hidden = !r("on").checked;
      r("auto").checked = c?.auto ?? true;
      const y0 = c?.region?.y0 ?? .84, y1 = c?.region?.y1 ?? 1;
      strip.set({ box: { x: 0, y: y0, w: 1, h: Math.max(.05, y1 - y0) } });
    },
  };
}

/* ------------------------------------------------------------------ English voice (replaces the spoken Chinese) */
function makeDub(host) {
  const root = $("tpl-dub").content.firstElementChild.cloneNode(true);
  host.append(root);
  const r = role => root.querySelector(`[data-role="${role}"]`);
  let available = false, wanted = false;  // `wanted` = the saved choice, kept even if the tool looks unavailable

  const fill = (select, voices) => {
    const keep = select.value;
    select.replaceChildren(...Object.entries(voices).map(([value, text]) => h("option", { value, text })));
    if (keep && voices[keep]) select.value = keep;
  };
  function setAvailable(ok, voices, voicesF) {
    available = !!ok;
    if (voices) fill(r("voice"), voices);
    if (voicesF) fill(r("voiceF"), voicesF);
    r("on").disabled = !available;
    r("on").checked = available && wanted;
    r("box").hidden = !r("on").checked;
    r("note").textContent = available
      ? "Speaks the translated captions (so check the .srt files for translation mistakes). Where a video has no captions, its voice is left alone. Adds a lot of processing time."
      : "Not available right now: the English voice, caption and music tools are all needed (your choice is kept).";
  }
  r("on").addEventListener("change", () => { wanted = r("on").checked; r("box").hidden = !wanted; });
  r("speed").addEventListener("input", () => { r("speedT").textContent = (+r("speed").value).toFixed(2) + "x"; });
  const audio = r("audio");
  audio.addEventListener("error", () => { r("status").textContent = "Could not load the sample."; r("hear").disabled = false; });
  const hear = (button, select) => button.addEventListener("click", async () => {
    button.disabled = true; r("status").textContent = "Preparing a sample (the first one takes a few seconds)...";
    audio.hidden = false; audio.src = `/api/voice-sample?voice=${encodeURIComponent(select.value)}`;
    try { await audio.play(); r("status").textContent = ""; } catch { r("status").textContent = "Press play to hear it."; }
    button.disabled = false;
  });
  hear(r("hear"), r("voice"));
  hear(r("hearF"), r("voiceF"));
  r("mode").addEventListener("change", () => { r("femaleRow").hidden = r("mode").value === "male"; });

  setAvailable(false);
  return {
    root, setAvailable, isOn: () => (available ? r("on").checked : wanted),
    get: () => ({ on: available ? r("on").checked : wanted, voice: r("voice").value || "am_michael",
                  voice_f: r("voiceF").value || "af_heart", mode: r("mode").value, speed: +r("speed").value }),
    set: c => {
      wanted = !!c?.on; r("on").checked = wanted && available; r("box").hidden = !r("on").checked;
      r("mode").value = c?.mode || (c?.match === false ? "male" : "auto");  // older saved settings only had match yes/no
      r("femaleRow").hidden = r("mode").value === "male";
      const pick = (select, want, fallback) => {  // the saved voice, else the default one
        const has = v => [...select.options].some(o => o.value === v);
        if (has(want)) select.value = want; else if (has(fallback)) select.value = fallback;
      };
      pick(r("voice"), c?.voice, "am_michael");
      pick(r("voiceF"), c?.voice_f, "af_heart");
      r("speed").value = c?.speed || 1; r("speedT").textContent = (+r("speed").value).toFixed(2) + "x";
    },
  };
}
const dubReady = s => !!(s.dub_available && s.captions_available && s.music_available);  // the voice needs all three tools

/* ------------------------------------------------------------------ Quick download tab */
const qCrop = makeCrop($("qCropHost"), { persistKey: "bili-last" });
let qInfo = null;
const qMusic = makeMusic($("qMusicHost"), {
  getUrl: () => $("qUrl").value,
  getTime: () => Math.floor(((qInfo && qInfo.duration) || 0) * 0.3),
});

// does this PC have the music-removal component? (asked once at start and whenever a tab needs it)
let musicAvailable = false;
async function refreshMusic() {
  try {
    const s = await api("/api/settings");
    musicAvailable = !!s.music_available;
    qMusic.setAvailable(musicAvailable);
    sMusic.setAvailable(musicAvailable);
    $("qCaps").disabled = !s.captions_available;
    if (!s.captions_available) $("qCaps").checked = false;
    $("qCapsNote").textContent = s.captions_available ? "" : "Not installed yet: the caption tools are still being set up.";
    $("qDub").disabled = !dubReady(s);
    if (!dubReady(s)) $("qDub").checked = false;
    $("qDubNote").textContent = dubReady(s) ? "" : "Not installed yet: the English voice, caption and music tools are all needed.";
    return s;
  } catch { return null; }
}

function setQualityOptions(select, heights) {
  const opts = [["best", "Best available"], ...heights.map(x => [String(x), x + "p"]), ["audio", "Audio only (mp3)"]];
  select.replaceChildren(...opts.map(([value, text]) => h("option", { value, text })));
}

function setupTrim(dur) {
  $("trimOn").checked = false; $("trimBox").style.display = "none";
  for (const id of ["startR", "endR"]) $(id).max = dur;
  $("startR").value = 0; $("endR").value = dur;
  updateTrim();
}
function updateTrim(changed) {
  let s = +$("startR").value, e = +$("endR").value;
  if (s >= e) {
    if (changed === "start") { s = Math.max(0, e - 1); $("startR").value = s; }
    else { e = Math.min(+$("endR").max, s + 1); $("endR").value = e; }
  }
  $("startT").textContent = fmtT(s); $("endT").textContent = fmtT(e);
  $("trimLen").textContent = `Clip length: ${fmtT(e - s)}`;
}
$("startR").addEventListener("input", () => updateTrim("start"));
$("endR").addEventListener("input", () => updateTrim("end"));
$("trimOn").addEventListener("change", () => { $("trimBox").style.display = $("trimOn").checked ? "block" : "none"; });
$("qQuality").addEventListener("change", () => lsSet("bili-quality", $("qQuality").value));

$("qForm").addEventListener("submit", async e => {
  e.preventDefault();
  $("qFetch").disabled = true; $("qPreview").style.display = "none"; setMsg($("qMsg"), "Fetching info...");
  try {
    const d = qInfo = await api("/api/info", { method: "POST", body: { url: $("qUrl").value } });
    $("qThumb").src = d.thumbnail || "";
    $("qName").textContent = d.title;
    $("qMeta").textContent = [d.uploader, d.duration ? fmtT(d.duration) : ""].filter(Boolean).join(" · ");
    setQualityOptions($("qQuality"), d.heights);
    const lastQ = lsGet("bili-quality", null);
    if (lastQ && [...$("qQuality").options].some(o => o.value === lastQ)) $("qQuality").value = lastQ;
    setupTrim(d.duration || 0);
    qCrop.setFrame({ width: d.width, height: d.height, src: d.thumbnail });
    qCrop.set(lsGet("bili-last", null));
    $("qPreview").style.display = "block"; setMsg($("qMsg"), "");
  } catch (err) { setMsg($("qMsg"), err.message, "err"); }
  $("qFetch").disabled = false;
});

$("useDefaults").addEventListener("click", async () => {
  try {
    const s = await api("/api/settings");
    if ([...$("qQuality").options].some(o => o.value === s.quality)) $("qQuality").value = s.quality;
    if (s.crop?.ui) qCrop.set({ ...s.crop.ui, on: s.crop.on });
    qMusic.setOn(s.remove_music);
    setMsg($("qMsg"), "Applied your default settings.", "ok");
  } catch (err) { setMsg($("qMsg"), err.message, "err"); }
});

$("qDl").addEventListener("click", async () => {
  $("qDl").disabled = true; $("qProg").hidden = false; $("qpBar").style.width = "0%"; setMsg($("qMsg"), "");
  try {
    const body = { url: $("qUrl").value, quality: $("qQuality").value };
    if ($("trimOn").checked) {
      body.start = +$("startR").value;
      if (+$("endR").value < +$("endR").max) body.end = +$("endR").value;
    }
    if (qCrop.isOn() && body.quality !== "audio") body.crop = qCrop.spec();
    if (qMusic.isOn()) body.remove_music = true;
    if ($("qCaps").checked && body.quality !== "audio") body.captions = true;
    if ($("qDub").checked && body.quality !== "audio") body.dub = true;
    const { id } = await api("/api/start", { method: "POST", body });
    while (true) {
      const p = await api("/api/progress/" + id);
      if (p.status === "error") throw new Error(p.error);
      $("qpBar").style.width = p.percent + "%";
      $("qpStage").textContent = p.stage === "Downloading clip"
        ? "Downloading and cutting clip... (no live progress for clips)"
        : `${p.stage} ${p.percent.toFixed(1)}%`;
      $("qpStats").textContent = p.stage.startsWith("Downloading")
        ? `${fmtBytes(p.speed)}/s · ${fmtBytes(p.downloaded)} / ${fmtBytes(p.total)} · ETA ${fmtEta(p.eta)}`
        : p.eta ? `About ${fmtEta(p.eta)} left (the AI runs on your PC)` : "";
      if (p.status === "done") {
        const a = h("a", { href: "/api/file/" + id, download: "" }); a.click();
        setMsg($("qMsg"), "Done! Saved to your library and your browser download should start now.", "ok");
        break;
      }
      await sleep(500);
    }
  } catch (err) { setMsg($("qMsg"), err.message, "err"); }
  $("qDl").disabled = false;
});

/* ------------------------------------------------------------------ Dashboard */
let channels = [], selected = null, videos = [], queue = { items: {}, version: null, order: [] };
const picked = new Set();
let rowRefs = new Map(), lastChannelsJson = "", settingsSummary = "";
const titles = new Map();  // bvid -> title, for the queue box

async function loadChannels() {
  const data = await api("/api/channels");
  const json = JSON.stringify(data);
  const wasSyncing = new Set(channels.filter(c => c.sync?.running).map(c => c.id));
  channels = data;
  if (json !== lastChannelsJson) { lastChannelsJson = json; renderChannels(); }
  if (selected == null && channels.length) {
    const saved = lsGet("bili-channel", null);  // reopen the channel you were last looking at
    selectChannel(channels.some(c => c.id === saved) ? saved : channels[0].id);
  }
  if (selected != null && !channels.some(c => c.id === selected)) { selected = null; $("videosCard").hidden = true; }
  // a sync that just finished: show the new videos
  if (selected != null && wasSyncing.has(selected) && !channels.find(c => c.id === selected)?.sync?.running) loadVideos();
  return data;
}

function renderChannels() {
  $("channels").replaceChildren(...channels.map(c => {
    const syncing = c.sync?.running;
    const pct = c.sync?.total ? Math.round(c.sync.done * 100 / c.sync.total) : 0;
    return h("div", { class: "chan" + (c.id === selected ? " sel" : ""), onclick: () => selectChannel(c.id) },
      h("b", { text: c.name || `Channel ${c.mid}` }),
      h("div", { class: "muted", text: `${c.total} videos · ${c.downloaded} downloaded` }),
      c.custom_settings ? h("div", { class: "muted", text: "Custom settings for this channel" }) : null,
      syncing ? h("div", { class: "muted", text: `${c.sync.phase}${c.sync.total ? ` ${c.sync.done}/${c.sync.total}` : "..."}` }) : null,
      syncing ? h("div", { class: "mini" }, h("div", { style: `width:${pct}%` })) : null,
      !syncing && c.sync?.error ? h("div", { class: "err muted", text: c.sync.error }) : null,
      !syncing && !c.sync && c.synced_at ? h("div", { class: "muted", text: "Synced " + new Date(c.synced_at * 1000).toLocaleString() }) : null,
      h("div", { class: "btns" },
        h("button", { type: "button", class: "small ghost", disabled: syncing, text: syncing ? "Syncing..." : "Sync",
          onclick: async e => { e.stopPropagation(); await api(`/api/channels/${c.id}/sync`, { method: "POST" }); loadChannels(); } }),
        h("button", { type: "button", class: "small danger", text: "Remove",
          onclick: async e => {
            e.stopPropagation();
            if (!confirm(`Remove "${c.name || c.mid}" from the dashboard? Downloaded files are kept.`)) return;
            await api(`/api/channels/${c.id}`, { method: "DELETE" }); loadChannels();
          } })));
  }));
}

function selectChannel(id) {
  selected = id; picked.clear(); lastChannelsJson = ""; renderChannels();
  lsSet("bili-channel", id);
  $("vSearch").value = ""; $("vFilter").value = "all"; $("vAll").checked = false;
  videos = []; $("vlist").replaceChildren();  // never show the previous channel's videos while the new list loads
  loadVideos();
  loadChannelSettings().catch(() => {});
}

async function loadVideos() {
  if (selected == null) return;
  const id = selected;
  const data = await api(`/api/channels/${id}/videos`);
  if (id !== selected) return;
  videos = data;
  await loadSummary();
  if (id !== selected) return;
  if ($("chSettings").open) ensureChannelFrame();
  for (const v of videos) titles.set(v.bvid, v.title || v.bvid);
  $("videosCard").hidden = false;
  renderVideos();
}

function shownVideos() {
  const term = $("vSearch").value.trim().toLowerCase(), f = $("vFilter").value;
  return videos.filter(v => (f === "all" || (f === "done") === v.downloaded) &&
    (!term || (v.title || "").toLowerCase().includes(term) || v.bvid.toLowerCase().includes(term)));
}

function renderVideos() {
  const ch = channels.find(c => c.id === selected);
  $("vTitle").textContent = ch ? (ch.name || `Channel ${ch.mid}`) : "Videos";
  const list = shownVideos();
  rowRefs = new Map();
  $("vlist").replaceChildren(...list.map(v => {
    const chk = h("input", { type: "checkbox", checked: picked.has(v.bvid),
      onchange: () => { chk.checked ? picked.add(v.bvid) : picked.delete(v.bvid); updateButtons(); } });
    const stat = h("div", { class: "vstat" });
    rowRefs.set(v.bvid, { stat, chk });
    return h("div", { class: "vrow" },
      chk,
      h("img", { src: v.thumbnail || "", alt: "", referrerpolicy: "no-referrer", loading: "lazy" }),
      h("div", {},
        h("div", { class: "t", text: v.title || "(loading details...)" }),
        h("div", { class: "muted", text: [v.duration ? fmtT(v.duration) : "", fmtDate(v.upload_date), v.bvid].filter(Boolean).join(" · ") })),
      stat);
  }));
  const have = videos.filter(v => v.downloaded).length;
  $("vCount").textContent = `${list.length} shown · ${videos.length} total · ${have} downloaded${settingsSummary}`;
  updateStatuses(); updateButtons();
}

function enqueue(bvids) {
  return api("/api/queue", { method: "POST", body: { bvids } }).then(tick);
}

function updateStatuses() {
  const byId = new Map(videos.map(v => [v.bvid, v]));
  for (const [bvid, { stat }] of rowRefs) {
    const v = byId.get(bvid), qs = queue.items[bvid];
    let badge, btn;
    if (qs?.status === "downloading") {
      badge = h("span", { class: "badge run", text: `${qs.stage} ${qs.percent.toFixed(0)}%` });
      btn = h("button", { type: "button", class: "small ghost", text: "Cancel", onclick: () => api("/api/queue/cancel", { method: "POST", body: { bvid } }).then(tick) });
    } else if (qs?.status === "queued") {
      badge = h("span", { class: "badge run", text: "Queued" });
      btn = h("button", { type: "button", class: "small ghost", text: "Cancel", onclick: () => api("/api/queue/cancel", { method: "POST", body: { bvid } }).then(tick) });
    } else if (qs?.status === "error") {
      badge = h("span", { class: "badge bad", title: qs.error, text: "Failed" });
      btn = h("button", { type: "button", class: "small", text: "Retry", title: qs.error, onclick: () => enqueue([bvid]) });
    } else if (v.downloaded) {
      badge = h("span", { class: "badge done", text: "✓ Downloaded",
        title: v.file_exists === false ? "Downloaded before. The file has since been deleted from your disk, but the video stays marked as downloaded." : null });
      btn = h("button", { type: "button", class: "small ghost", text: "Download again", onclick: () => enqueue([bvid]) });
    } else {
      badge = h("span", { class: "badge", text: "Not downloaded" });
      btn = h("button", { type: "button", class: "small", text: "Download", onclick: () => enqueue([bvid]) });
    }
    const sub = qs?.status === "downloading" && qs.speed ? h("span", { class: "muted", text: `${fmtBytes(qs.speed)}/s` }) : null;
    stat.replaceChildren(...[badge, sub, btn].filter(Boolean));
  }
}

function updateButtons() {
  const anyNew = videos.some(v => !v.downloaded && !queue.items[v.bvid]);
  $("dlNew").disabled = !anyNew;
  $("dlSel").disabled = picked.size === 0;
  $("dlSel").textContent = picked.size ? `Download selected (${picked.size})` : "Download selected";
}

function updateQueueBox() {
  const entries = Object.entries(queue.items);
  const cur = entries.find(([, s]) => s.status === "downloading");
  const waiting = entries.filter(([, s]) => s.status === "queued").length;
  $("queueBox").hidden = !cur && !waiting;
  if (!cur) { $("qTitle").textContent = `${waiting} waiting...`; $("qBar").style.width = "0%"; $("qInfo").textContent = ""; return; }
  const [bvid, s] = cur;
  $("qTitle").textContent = `${s.stage}: ${titles.get(bvid) || bvid}`;
  $("qBar").style.width = s.percent + "%";
  $("qInfo").textContent = [`${s.percent.toFixed(1)}%`,
    s.stage.startsWith("Downloading") && s.speed ? `${fmtBytes(s.speed)}/s · ${fmtBytes(s.downloaded)} / ${fmtBytes(s.total)} · ETA ${fmtEta(s.eta)}` : "",
    !s.stage.startsWith("Downloading") && s.eta ? `about ${fmtEta(s.eta)} left` : "",
    waiting ? `${waiting} more in the queue` : ""].filter(Boolean).join(" · ");
}

$("chanForm").addEventListener("submit", async e => {
  e.preventDefault();
  $("chanAdd").disabled = true; setMsg($("chanMsg"), "Looking up the channel...");
  try {
    const { id } = await api("/api/channels", { method: "POST", body: { input: $("chanInput").value } });
    $("chanInput").value = ""; setMsg($("chanMsg"), "Channel added. Reading its videos...", "ok");
    await loadChannels(); selectChannel(id);
  } catch (err) { setMsg($("chanMsg"), err.message, "err"); }
  $("chanAdd").disabled = false;
});

$("dlNew").addEventListener("click", async () => {
  const { added } = await api("/api/queue", { method: "POST", body: { channel_id: selected } });
  setMsg($("chanMsg"), added ? `Queued ${added} video(s). They download one at a time.` : "Nothing new to download.", added ? "ok" : "");
  tick();
});
$("dlSel").addEventListener("click", async () => { await enqueue([...picked]); picked.clear(); renderVideos(); });
$("qStop").addEventListener("click", () => api("/api/queue/cancel", { method: "POST", body: {} }).then(tick));
$("vSearch").addEventListener("input", renderVideos);
$("vFilter").addEventListener("change", renderVideos);
$("vAll").addEventListener("change", () => {
  for (const v of shownVideos()) $("vAll").checked ? picked.add(v.bvid) : picked.delete(v.bvid);
  renderVideos();
});

async function loadSummary() {
  try {
    let s, custom = false;
    if (selected != null) {
      const r = await api(`/api/channels/${selected}/settings`);
      s = r.effective; custom = !!r.override;
    } else {
      s = await api("/api/settings");
    }
    const q = s.quality === "best" ? "best quality" : s.quality === "audio" ? "audio only" : s.quality + "p";
    settingsSummary = ` · downloads use ${custom ? "this channel's own settings" : "the global settings"}: ${q}, crop ${s.crop?.on ? "on" : "off"}, music removal ${s.remove_music ? "on" : "off"}, English captions ${s.captions?.on ? "on" : "off"}, English voice ${s.dub?.on ? "on" : "off"}`;
  } catch { settingsSummary = ""; }
}

/* ---- per-channel settings: a channel can override quality, crop and music removal ---- */
const chCrop = makeCrop($("chCropHost"), { label: "Crop / change aspect ratio for this channel" });
const chMusic = makeMusic($("chMusicHost"), { getUrl: () => videos[0]?.url || "", getTime: () => 10 });
// a moment in the middle of the channel's newest video, where captions are usually on screen
const chTime = () => Math.floor(Math.min(60, (videos[0]?.duration || 40) * 0.4));
const chCaps = makeCaptions($("chCapsHost"), { getUrl: () => videos[0]?.url || "", getTime: chTime });
const chDub = makeDub($("chDubHost"));
let chFrameFor = null;  // channel whose preview frame is currently loaded

async function loadChannelSettings() {
  const id = selected;
  if (id == null) return;
  const r = await api(`/api/channels/${id}/settings`);
  if (id !== selected) return;
  chMusic.setAvailable(r.music_available);
  chCaps.setAvailable(r.captions_available);
  const custom = !!r.override, e = r.override || r.effective;  // when not custom, show the global values as the starting point
  chCaps.set(e.captions);
  chDub.setAvailable(dubReady(r), r.voices, r.voices_f); chDub.set(e.dub);
  $("chCustom").checked = custom; $("chFields").hidden = !custom;
  $("chSetState").textContent = custom ? "· custom for this channel" : "· using the global settings";
  $("chQuality").value = e.quality || "best";
  chMusic.setOn(e.remove_music);
  chCrop.set(e.crop?.ui ? { ...e.crop.ui, on: e.crop.on }
    : { on: false, ratio: "orig", cw: 16, ch: 9, size: 100, box: { x: .25, y: .25, w: .5, h: .5 } });
  chFrameFor = null; setMsg($("chMsg"), "");
  if ($("chSettings").open) ensureChannelFrame();
}

// real frame from this channel's newest video, loaded only when the panel is opened
async function ensureChannelFrame() {
  const v = videos[0];
  if (!v || chFrameFor === selected || $("chFields").hidden) return;
  chFrameFor = selected;
  try {
    const d = await api("/api/info", { method: "POST", body: { url: v.url } });
    if (chFrameFor !== selected) return;
    const frame = { width: d.width, height: d.height, src: `/api/frame?url=${encodeURIComponent(v.url)}&t=${chTime()}`,
      fallback: d.thumbnail || "", msg: "Loading frame..." };
    chCrop.setFrame(frame);
    chCaps.setFrame(frame);
  } catch (err) { setMsg($("chMsg"), "Preview unavailable: " + err.message, "err"); }
}

$("chSettings").addEventListener("toggle", () => { if ($("chSettings").open) ensureChannelFrame(); });
$("chCustom").addEventListener("change", () => {
  $("chFields").hidden = !$("chCustom").checked;
  if ($("chCustom").checked) ensureChannelFrame();
});
$("chSave").addEventListener("click", async () => {
  $("chSave").disabled = true;
  try {
    const override = $("chCustom").checked
      ? { quality: $("chQuality").value, remove_music: chMusic.isOn(), captions: chCaps.get(), dub: chDub.get(),
          crop: { on: chCrop.isOn(), ui: chCrop.get(), spec: chCrop.spec() } }
      : null;
    await api(`/api/channels/${selected}/settings`, { method: "PUT", body: { override } });
    setMsg($("chMsg"), override ? "Saved. Downloads for this channel use these settings." : "This channel now uses the global settings.", "ok");
    $("chSetState").textContent = override ? "· custom for this channel" : "· using the global settings";
    loadSummary().then(renderVideos); loadChannels();
  } catch (err) { setMsg($("chMsg"), err.message, "err"); }
  $("chSave").disabled = false;
});

tabLoaders.dashboard = async () => { await loadSummary(); await loadChannels(); if (selected != null) loadVideos(); };

let ticks = 0;
async function tick() {
  if (document.hidden) return;
  ticks++;
  try {
    const qd = await api("/api/queue");
    const changed = queue.version !== null && qd.version !== queue.version;
    queue = qd;
    updateQueueBox(); updateStatuses(); updateButtons();
    const syncing = channels.some(c => c.sync?.running);
    if (changed || syncing || ticks % 5 === 0) await loadChannels();
    if (changed || (syncing && ticks % 3 === 0)) { loadVideos(); }
    if (changed && !$("tab-library").hidden) loadLibrary();
  } catch { /* server restarting or busy */ }
}
setInterval(tick, 1200);

/* ------------------------------------------------------------------ Library */
let library = [];
async function loadLibrary() {
  library = await api("/api/library");
  renderLibrary();
}
const libGroup = d => d.channel || d.folder || "Other";
function renderLibrary() {
  const term = $("lSearch").value.trim().toLowerCase();
  // one entry per channel so the Library can be viewed one channel at a time
  const groups = [...new Set(library.map(libGroup))].sort();
  const keep = $("lChannel").value;
  $("lChannel").replaceChildren(h("option", { value: "", text: `All channels (${library.length})` }),
    ...groups.map(g => h("option", { value: g, text: `${g} (${library.filter(d => libGroup(d) === g).length})` })));
  $("lChannel").value = groups.includes(keep) ? keep : "";
  const only = $("lChannel").value;
  const rows = library.filter(d => (!only || libGroup(d) === only) &&
    (!term || (d.title || "").toLowerCase().includes(term) || libGroup(d).toLowerCase().includes(term)));
  $("lCount").textContent = `${rows.length} shown · ${library.length} downloaded in total`;
  $("lList").replaceChildren(...(rows.length ? rows.map(d => h("div", { class: "lrow" },
    h("div", {},
      h("div", { style: "font-weight:600", text: d.title || d.bvid }),
      h("div", { class: "muted", text: [d.channel || d.folder, d.quality === "best" ? "best quality" : d.quality === "audio" ? "audio" : d.quality + "p",
        d.crop ? "cropped" : "", d.nomusic ? "music removed" : "", fmtBytes(d.size), new Date(d.created_at * 1000).toLocaleString()].filter(Boolean).join(" · ") }),
      !d.exists ? h("span", { class: "badge", text: "File deleted from disk (still marked as downloaded)" }) : null),
    h("div", { class: "btns" },
      d.exists ? h("a", { href: `/api/library/${d.id}/file`, download: "" }, h("button", { type: "button", class: "small", text: "Save a copy" })) : null,
      d.exists ? h("button", { type: "button", class: "small ghost", text: "Show in folder",
        onclick: () => api(`/api/library/${d.id}/reveal`, { method: "POST" }).catch(err => alert(err.message)) }) : null,
      d.exists ? h("button", { type: "button", class: "small danger", text: "Delete file",
        title: "Frees disk space. The video stays marked as downloaded.",
        onclick: async () => {
          if (!confirm("Delete this file from your disk?\n\nThe video stays marked as downloaded, so it will not be downloaded again by accident.")) return;
          try { await api(`/api/library/${d.id}?file=1`, { method: "DELETE" }); } catch (err) { alert(err.message); }
          loadLibrary(); loadChannels();
        } }) : null,
      h("button", { type: "button", class: "small ghost", text: "Forget",
        title: "Remove this entry. The video is then no longer marked as downloaded.",
        onclick: async () => {
          if (!confirm("Forget this video?\n\nIt will no longer be marked as downloaded." + (d.exists ? " The file itself is kept on your disk." : ""))) return;
          await api(`/api/library/${d.id}?forget=1`, { method: "DELETE" });
          loadLibrary(); loadChannels();
        } })))) : [h("div", { class: "muted", text: "Nothing downloaded yet." })]));
}
$("lSearch").addEventListener("input", renderLibrary);
$("lChannel").addEventListener("change", renderLibrary);
tabLoaders.library = () => loadLibrary().catch(err => { $("lCount").textContent = err.message; });

/* ------------------------------------------------------------------ Settings */
const sCrop = makeCrop($("sCropHost"), { label: "Crop / change aspect ratio for all channel downloads" });
const sMusic = makeMusic($("sMusicHost"), {
  getUrl: () => $("sPreviewUrl").value,
  getTime: () => +$("sTime").value || 0,
});
const sCaps = makeCaptions($("sCapsHost"), {
  getUrl: () => $("sPreviewUrl").value,
  getTime: () => +$("sTime").value || 0,
});
const sDub = makeDub($("sDubHost"));
let sDur = 0, sDims = {}, sLoaded = false;

function frameSrc() { return `/api/frame?url=${encodeURIComponent($("sPreviewUrl").value.trim())}&t=${$("sTime").value}`; }

async function loadPreview() {
  const url = $("sPreviewUrl").value.trim();
  if (!url) { setMsg($("sMsg"), "Add a video link first.", "err"); return; }
  $("sLoad").disabled = true; setMsg($("sMsg"), "Loading preview...");
  try {
    const d = await api("/api/info", { method: "POST", body: { url } });
    sDur = d.duration || 0; sDims = { width: d.width, height: d.height };
    $("sTime").max = Math.max(0, sDur - 1); $("sTime").value = Math.floor(sDur * .15); $("sTimeT").textContent = fmtT($("sTime").value);
    $("sTimeRow").hidden = false;
    sCrop.setFrame({ ...sDims, src: frameSrc(), fallback: d.thumbnail || "", msg: "Loading frame..." });
    sCaps.setFrame({ ...sDims, src: frameSrc(), fallback: d.thumbnail || "", msg: "Loading frame..." });
    setMsg($("sMsg"), "");
  } catch (err) { setMsg($("sMsg"), err.message, "err"); }
  $("sLoad").disabled = false;
}
$("sLoad").addEventListener("click", loadPreview);
$("sTime").addEventListener("input", () => { $("sTimeT").textContent = fmtT($("sTime").value); });
$("sTime").addEventListener("change", () => {
  sCrop.setImage(frameSrc(), "Loading frame...");
  sCaps.setImage(frameSrc(), "Loading frame...");
});

$("sSave").addEventListener("click", async () => {
  $("sSave").disabled = true;
  try {
    await api("/api/settings", { method: "PUT", body: {
      quality: $("sQuality").value, download_dir: $("sDir").value, cookies_file: $("sCookies").value,
      preview_url: $("sPreviewUrl").value.trim(), remove_music: sMusic.isOn(), lookup_mode: $("sLookup").value,
      captions: sCaps.get(), dub: sDub.get(),
      crop: { on: sCrop.isOn(), ui: sCrop.get(), spec: sCrop.spec() },
    } });
    setMsg($("sMsg"), "Saved. New downloads will use these settings.", "ok");
    loadSummary();
  } catch (err) { setMsg($("sMsg"), err.message, "err"); }
  $("sSave").disabled = false;
});

tabLoaders.settings = async () => {
  if (sLoaded) return;
  try {
    const s = await api("/api/settings");
    sLoaded = true;
    qMusic.setAvailable(!!s.music_available); sMusic.setAvailable(!!s.music_available);
    sMusic.setOn(s.remove_music);
    sCaps.setAvailable(!!s.captions_available); sCaps.set(s.captions);
    sDub.setAvailable(dubReady(s), s.voices, s.voices_f); sDub.set(s.dub);
    $("sLookup").value = s.lookup_mode || "auto";
    const net = s.network || {};
    $("sNet").textContent = !net.vpn ? "No VPN detected on this PC."
      : net.direct_lookups ? `VPN detected. Video lookups are sent from your direct connection (${net.direct_lookups}) because Bilibili blocks most VPN addresses; the video files download through the VPN.`
      : "VPN detected. Lookups also go through the VPN, which Bilibili may block (error 412).";
    $("sQuality").value = s.quality; $("sDir").value = s.download_dir; $("sCookies").value = s.cookies_file || "";
    $("sPreviewUrl").value = s.preview_url || "";
    if (s.crop?.ui) sCrop.set({ ...s.crop.ui, on: s.crop.on });
    if (s.preview_url) loadPreview();
  } catch (err) { setMsg($("sMsg"), err.message, "err"); }
};

/* ------------------------------------------------------------------ start */
refreshMusic();
showTab(lsGet("bili-tab", "dashboard"));
if (lsGet("bili-tab", "dashboard") === "dashboard") { /* showTab already ran the loader */ }
