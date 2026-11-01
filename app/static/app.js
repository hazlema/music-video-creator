"use strict";

// ================================================================ helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const money = (v) => (v == null ? "?" : "$" + (v > 0 && v < 1 ? v.toFixed(3) : v.toFixed(2)));
function tc(t) {
  t = Math.max(0, t || 0);
  return `${Math.floor(t / 60)}:${(t % 60).toFixed(2).padStart(5, "0")}`;
}
const tcShort = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), err ? 9000 : 4500);
}

async function api(method, path, body, headers = {}) {
  const opt = { method, headers: { ...headers } };
  if (body instanceof Blob) opt.body = body;
  else if (body !== undefined) {
    opt.body = JSON.stringify(body);
    opt.headers["Content-Type"] = "application/json";
  }
  let r;
  try {
    r = await fetch("/api/" + path, opt);
  } catch (_) {
    throw new Error("Can't reach the app server, so nothing was changed. Start it again (see app/README.md), then retry.");
  }
  const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function act(fn) {
  try {
    return await fn();
  } catch (e) {
    toast(e.message, true);
  }
}

// ================================================================== state
const S = {
  view: "library", slug: null, tab: "timeline", st: null, peaks: null,
  pps: 40, sel: [], anchor: null, t: 0, playing: false, previewMode: "clips",
  drafts: {}, proposal: null, drag: null, est: {}, seenTasks: {}, inspKey: "", setupDraft: {}, busy: null,
};
const audio = new Audio();
audio.preload = "auto";

const fileUrl = (rel) => `/api/songs/${S.slug}/file/${rel}`;
const plan = () => S.st?.plan || [];
const secById = (id) => plan().find((s) => s.uid === id);
const stillOf = (s) => s.stills.find((v) => v.n === s.still);
const clipOf = (s) => s.clips.find((c) => c.n === s.clip);
const len = (s) => s.end - s.start;
const captionText = (a, b) => (S.st?.lines || []).filter((l) => a <= (l.start + l.end) / 2 && (l.start + l.end) / 2 < b).map((l) => l.text).join(" / ");
const sectionAt = (t) => plan().find((s) => s.start <= t && t < s.end);
const running = (kind, uid) => (S.st?.tasks || []).some((t) => t.state === "running" && t.kind === kind && (uid === undefined || t.uid === uid));

// ================================================================= router
function route() {
  // Leaving Setup (tab or song) saves anything still waiting to be saved.
  if (S.view === "song" && Object.keys(S.setupDraft).length) saveSetup();
  const m = location.hash.match(/^#\/song\/([a-z0-9-]+)(?:\/(setup|timeline))?/);
  if (m) openSong(m[1], m[2]);
  else if (location.hash === "#/settings") openSettings();
  else openLibrary();
}
window.addEventListener("hashchange", route);

// ================================================================ library
async function openLibrary() {
  S.view = "library";
  audio.pause();
  const list = await act(() => api("GET", "songs")) || [];
  $("#app").innerHTML = `
  <main class="library">
    <a class="btn quiet settings-link" href="#/settings">Settings</a>
    <h1>Songs into music videos</h1>
    <p class="lede">Each song gets a timeline: timed lyrics, a storyboard, a still for every shot, then a clip for every still you approve.</p>
    ${list.length ? `<ul class="song-grid">${list.map((s) => `
      <li><a href="#/song/${s.slug}">
        <span class="cover">${s.cover ? `<img src="${`/api/songs/${s.slug}/file/${s.cover}`}" alt="" loading="lazy">` : `<i>No stills yet</i>`}</span>
        <span class="t">${esc(s.title)}</span>
        <span class="muted small">${esc(s.stage)}${s.sections ? ` · ${s.sections} sections` : ""}${s.length ? ` · ${tcShort(s.length)}` : ""}</span>
        <span class="muted small">${money(s.spent)} spent</span>
      </a></li>`).join("")}</ul>` : `<p class="muted">No songs yet. Start one below.</p>`}
    <form class="new-song" id="newSong">
      <input type="text" name="title" placeholder="Song title" required aria-label="Song title">
      <button class="btn primary">New song</button>
    </form>
  </main>`;
  $("#newSong").onsubmit = (e) => {
    e.preventDefault();
    act(async () => {
      const r = await api("POST", "songs", { title: e.target.title.value });
      location.hash = `#/song/${r.slug}/setup`;
    });
  };
}

// =============================================================== settings
// App-wide, not per song. For now: where Blender is, for music_video.blend.
async function openSettings() {
  S.view = "settings";
  audio.pause();
  const cfg = await act(() => api("GET", "settings")) || { blender: "" };
  $("#app").innerHTML = `
  <main class="library settings">
    <a class="btn quiet" href="#/">← Songs</a>
    <h1>Settings</h1>
    <section class="block">
      <h2>Export</h2>
      <label class="field"><span>Blender (optional). With it, export also writes <b>music_video.blend</b>: the song, every clip at its timecode and a marker per lyric line, ready for fades and fixes in Blender's Video Editor. Without it you still get the MP4 and lyrics.srt.</span>
        <input type="text" id="blPath" value="${esc(cfg.blender)}" placeholder="/path/to/blender" spellcheck="false"></label>
      <div class="actions">
        <button class="btn" id="blFind">Find Blender</button>
        <button class="btn primary" id="blTest">Test and save</button>
        <button class="btn quiet" id="blClear" ${cfg.blender ? "" : "hidden"}>Don't use Blender</button>
      </div>
      <div id="blFound"></div>
      <p class="small" id="blStatus">${cfg.blender ? `Saved: <code>${esc(cfg.blender)}</code>` : "Not set, so exports don't include a .blend."}</p>
    </section>
  </main>`;
  const input = $("#blPath"), status = $("#blStatus");
  const show = (html, cls = "") => { status.className = `small ${cls}`; status.innerHTML = html; };
  $("#blFind").onclick = () => act(async () => {
    $("#blFind").disabled = true; $("#blFind").textContent = "Looking…";
    const r = await api("GET", "settings/blender/find").finally(() => { $("#blFind").disabled = false; $("#blFind").textContent = "Find Blender"; });
    $("#blFound").innerHTML = r.found.length
      ? `<p class="small muted">Found on this machine. Click one to use it, then test it:</p><div class="found">${r.found.map((f) =>
          `<button class="btn small" data-bl="${esc(f.path)}">Blender ${esc(f.version)} <code>${esc(f.path)}</code></button>`).join("")}</div>`
      : `<p class="small">No Blender found in the usual places. Paste the path to the <code>blender</code> program, for example from the folder you unpacked it into.</p>`;
    $$("[data-bl]").forEach((b) => (b.onclick = () => { input.value = b.dataset.bl; show("Click <b>Test and save</b> to check it."); }));
  });
  $("#blTest").onclick = () => act(async () => {
    $("#blTest").disabled = true; show("Testing: writing a small project with this Blender…");
    try {
      const r = await api("POST", "settings/blender/test", { path: input.value.trim() });
      if (!r.ok) return show(`<b>Didn't work.</b> ${esc(r.error).replace(/\n/g, "<br>")}`, "bad");
      await api("PUT", "settings", { blender: input.value.trim() });
      $("#blClear").hidden = false;
      show(`<b>Works.</b> Blender ${esc(r.version)} wrote a test project in ${r.seconds} s. Saved; exports will include music_video.blend.`, "good");
    } finally { $("#blTest").disabled = false; }
  });
  $("#blClear").onclick = () => act(async () => {
    await api("PUT", "settings", { blender: "" });
    input.value = ""; $("#blClear").hidden = true;
    show("Blender turned off. Exports will skip the .blend.");
  });
}

// =================================================================== song
async function openSong(slug, tab) {
  const fresh = S.slug !== slug || S.view !== "song";
  S.view = "song";
  if (fresh) {
    Object.assign(S, { slug, sel: [], anchor: null, t: 0, drafts: {}, setupDraft: {}, proposal: null, est: {}, peaks: null, seenTasks: {}, inspKey: "" });
    audio.pause();
    S.playing = false;
  }
  const st = await act(() => api("GET", `songs/${slug}`));
  if (!st) return;
  S.st = st;
  st.tasks.forEach((t) => (S.seenTasks[t.tid] = t.state));
  S.tab = tab || (st.plan.length ? "timeline" : "setup");
  if (S.tab === "setup") S.models = null;   // fetch the model list (and sidecar status) fresh on each visit
  if (fresh && st.has_audio) {
    audio.src = fileUrl("song.wav");
    S.peaks = await api("GET", `songs/${slug}/peaks`).catch(() => null);
  }
  if (fresh && st.length) S.pps = fitPps();
  renderShell();
}

function setState(st) {
  const hadAudio = S.st?.has_audio;
  S.st = st;
  notifyTasks(st.tasks);
  if (!hadAudio && st.has_audio) {
    audio.src = fileUrl("song.wav") + "?" + Date.now();
    api("GET", `songs/${S.slug}/peaks`).then((p) => { S.peaks = p; S.pps = fitPps(); update(); });
  }
  S.sel = S.sel.filter((id) => secById(id));
  update();
}

async function refresh() {
  if (S.view !== "song") return;
  const st = await api("GET", `songs/${S.slug}`).catch(() => null);
  if (st && S.view === "song" && st.slug === S.slug) setState(st);
}

function notifyTasks(list) {
  for (const t of list) {
    const before = S.seenTasks[t.tid];
    S.seenTasks[t.tid] = t.state;
    if (before !== "running" || t.state === "running") continue;
    const where = t.uid && secById(t.uid) ? ` (${tc(secById(t.uid).start)})` : "";
    if (t.state === "failed") toast(`${t.label} failed${where}: ${t.msg}`, true);
    else if (t.kind === "transcribe") { toast("Transcribed and matched to your lyrics."); S.pps = fitPps(); }
    else if (t.kind === "storyboard") toast(`Storyboard written. ${t.msg}`);
    else if (t.kind === "export") { S.busy = null; $("#busy")?.open && $("#busy").close(); toast(t.msg); openResult(); }
  }
}

let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = (S.st?.tasks || []).some((t) => t.state === "running") || plan().some((s) => s.job && s.job.state !== "unknown");
  pollTimer = setTimeout(async () => { await refresh(); schedulePoll(); }, busy ? 1500 : 8000);
}

function renderShell() {
  const st = S.st;
  $("#app").innerHTML = `
  <div class="shell">
    <header class="topbar">
      <a class="btn quiet" href="#/">Songs</a>
      <a class="btn quiet" href="#/settings" title="App settings: Blender for export">Settings</a>
      <span class="title">${esc(st.meta.title || st.slug)}</span>
      <div class="tabs" role="group" aria-label="View">
        <button data-tab="setup" aria-pressed="${S.tab === "setup"}">Setup</button>
        <button data-tab="timeline" aria-pressed="${S.tab === "timeline"}">Timeline</button>
      </div>
      <span class="spacer"></span>
      <span class="spend" id="spend"></span>
      <button class="btn" id="btnStoryboard">Write storyboard</button>
      <button class="btn" id="btnStills">Generate missing stills</button>
      <button class="btn" id="btnAnimate">Animate…</button>
      <button class="btn primary" id="btnExport">Export…</button>
    </header>
    ${S.tab === "setup" ? `<section class="setup" id="setup"></section>` : `
    <section class="work">
      <div class="stage">
        <div class="screen" id="screen">
          <img id="scrImg" alt="" hidden><video id="scrVid" muted playsinline hidden></video>
          <div class="empty" id="scrEmpty" hidden></div>
          <div class="cap" id="scrCap"></div><div class="src" id="scrSrc"></div>
        </div>
        <div class="transport">
          <button class="btn play" id="btnPlay" aria-label="Play">▶</button>
          <span class="time" id="time"></span>
          <label class="check small"><input type="checkbox" id="stillsOnly" ${S.previewMode === "stills" ? "checked" : ""}> Stills only</label>
          <span class="muted small">Space plays, arrow keys step through sections, Shift-click selects a range, Alt+A selects all, Esc clears.</span>
        </div>
      </div>
      <aside class="inspector" id="inspector"></aside>
    </section>
    <div class="toolbar" id="toolbar"></div>
    <div class="timeline" id="timeline"><div class="tl-inner" id="tlInner"></div></div>`}
  </div>
  <dialog id="dlg"></dialog><dialog id="busy" class="busy"></dialog>`;

  $$("[data-tab]").forEach((b) => (b.onclick = () => (location.hash = `#/song/${S.slug}/${b.dataset.tab}`)));
  $("#btnStoryboard").onclick = writeStoryboard;
  $("#btnStills").onclick = generateMissing;
  $("#btnAnimate").onclick = () => openAnimate(S.sel.length ? S.sel : null);
  $("#btnExport").onclick = openExport;
  if (S.tab === "timeline") {
    $("#btnPlay").onclick = togglePlay;
    $("#stillsOnly").onchange = (e) => { S.previewMode = e.target.checked ? "stills" : "clips"; updateScreen(true); };
    bindTimeline();
  }
  S.inspKey = "";
  update();
  schedulePoll();
}

function update() {
  if (S.view !== "song" || !S.st) return;
  const st = S.st;
  $("#spend").innerHTML = `Spent <b>${money(st.spent)}</b>`;
  $("#btnStoryboard").disabled = running("storyboard") || !st.plan.length;
  $("#btnStoryboard").textContent = running("storyboard") ? "Writing storyboard…" : st.plan.some((s) => s.image_prompt) ? "Rewrite storyboard" : "Write storyboard";
  const missing = st.plan.filter((s) => s.still === null && s.image_prompt && !s.locked && !running("still", s.uid)).length;
  // Always clickable: when there's nothing to make, the click says why.
  $("#btnStills").textContent = missing ? `Generate ${missing} missing stills` : "Generate missing stills";
  $("#btnAnimate").disabled = !st.plan.length;
  $("#btnExport").disabled = !st.plan.length || running("export");
  $("#btnExport").textContent = running("export") ? exportProgress() : "Export…";
  renderBusy();
  if (S.tab === "setup") renderSetup();
  else {
    if (!S.drag) renderTimeline();
    renderToolbar();
    renderInspector();
    updateScreen(true);
  }
  schedulePoll();
}

function exportProgress() {
  const t = (S.st.tasks || []).find((t) => t.kind === "export" && t.state === "running");
  return t?.progress ? `Exporting ${Math.round(t.progress * 100)}%` : "Exporting…";
}

// ========================================================= progress window
// Long jobs (storyboard, a batch of stills, export) show a window with a
// clock while they run. "Keep working" hides it; the job carries on.
let busyTimer = null;

function openBusy(kind, since, total = 0) {
  S.busy = { kind, since, total, hidden: false };
  clearInterval(busyTimer);
  busyTimer = setInterval(renderBusy, 1000);
  renderBusy();
}

function busyTasks() {
  const b = S.busy;
  return (S.st?.tasks || []).filter((t) => t.kind === b.kind && t.at >= b.since);
}

function renderBusy() {
  const dlg = $("#busy");
  const b = S.busy;
  if (!dlg || !b) { if (dlg?.open) dlg.close(); return; }
  const list = busyTasks();
  const live = list.filter((t) => t.state === "running");
  if (!live.length && (list.length || Date.now() / 1000 - b.since > 10)) {
    S.busy = null;
    clearInterval(busyTimer);
    if (dlg.open) dlg.close();
    if (b.kind === "still") {
      const failed = list.filter((t) => t.state === "failed").length;
      toast(`${list.length - failed} stills done${failed ? `, ${failed} failed. Failed sections show a Retry button.` : "."}`, failed > 0);
    }
    return;
  }
  if (b.hidden) { if (dlg.open) dlg.close(); return; }
  const secs = Math.max(0, Math.round(Date.now() / 1000 - b.since));
  const elapsed = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
  let title, msg, frac = null;
  if (b.kind === "storyboard") {
    title = "Writing the storyboard";
    msg = (live[0]?.msg || "Starting") + ". This usually takes a minute or two.";
  } else if (b.kind === "still") {
    const total = Math.max(b.total, list.length);
    const done = list.filter((t) => t.state !== "running").length;
    const failed = list.filter((t) => t.state === "failed").length;
    title = "Generating stills";
    msg = `${done} of ${total} done${failed ? `, ${failed} failed` : ""}. Four render at a time; finished ones appear on the timeline.`;
    frac = total ? done / total : 0;
  } else {
    title = "Exporting the video";
    msg = live[0]?.msg || "Starting";
    frac = live[0]?.progress || 0;
  }
  dlg.innerHTML = `
    <div class="busy-body">
      <div class="clock" aria-hidden="true"><i class="hand-h"></i><i class="hand-m"></i></div>
      <div>
        <h2>${esc(title)}</h2>
        <p>${esc(msg)}</p>
        ${frac !== null ? `<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(frac * 100)}"><i style="width:${Math.round(frac * 100)}%"></i></div>` : ""}
        <p class="muted small">${elapsed} elapsed</p>
      </div>
    </div>
    <div class="df"><span class="note">You can keep working; this carries on in the background.</span><button class="btn" id="busyHide">Keep working</button></div>`;
  $("#busyHide").onclick = () => { b.hidden = true; dlg.close(); };
  if (!dlg.open && !$("#dlg")?.open) dlg.showModal();
}

// ================================================================== setup

// Setup fields keep unsaved text in S.setupDraft and save themselves shortly
// after typing stops, so a redraw (polling, uploads) never loses what was typed.
const SETUP_FIELDS = {
  fTitle: "title", fLyrics: "lyrics", fBrief: "brief", fStyle: "style",
};
const MODEL_FIELDS = { transcribe: "transcribe_model", llm: "llm_model", image: "image_model", video: "video_model" };
let setupTimer = null;

function setupSaved(field) {
  const st = S.st;
  return field === "lyrics" ? st.lyrics : field === "brief" ? st.brief : st.meta[field];
}

function setupVal(field) {
  return field in S.setupDraft ? S.setupDraft[field] : setupSaved(field) ?? "";
}

function renderSetup() {
  const el = $("#setup");
  const st = S.st, m = st.meta;
  // Rebuild only when the form's structure changes; otherwise just refresh the buttons.
  const key = JSON.stringify([S.slug, st.has_audio, st.length, m.refs.map((r) => r.id)]);
  if (el.dataset.key === key) { renderSetupStatus(); return; }
  el.dataset.key = key;
  const focus = document.activeElement?.id && el.contains(document.activeElement)
    ? { id: document.activeElement.id, a: document.activeElement.selectionStart, b: document.activeElement.selectionEnd } : null;
  const refLabels = Object.fromEntries($$("[data-ref-label]", el).map((i) => [i.dataset.refLabel, i.value]));
  el.innerHTML = `
  <div class="setup-grid">
    <div>
      <h2>Song</h2>
      <label class="field"><span>Title</span><input type="text" id="fTitle" value="${esc(setupVal("title"))}"></label>
      <div class="dropzone" id="audioDrop" tabindex="0">${st.has_audio ? `Song loaded, ${tc(st.length)} long. Drop a new file to replace it.` : "Drop the song here (WAV, MP3, FLAC), or click to choose"}</div>
      <input type="file" id="audioFile" accept="audio/*,.wav" hidden>
      ${st.has_audio ? `<audio controls src="${fileUrl("song.wav")}?v=${st.length}" style="width:100%;margin-top:8px"></audio>` : ""}
      <div class="lyrics" style="margin-top:18px">
        <label class="field"><span>Lyrics, one line per caption. These words are used exactly; transcription only supplies timing. Lines like [Chorus] mark song parts and never become captions.</span>
        <textarea id="fLyrics" spellcheck="false">${esc(setupVal("lyrics"))}</textarea></label>
      </div>
      <div class="step" id="setupStatus"></div>
    </div>
    <div>
      <h2>Look</h2>
      <div class="brief"><label class="field"><span>Brief: the video's premise, its look and mood, a recurring motif, how the mood changes. All optional.</span>
        <textarea id="fBrief">${esc(setupVal("brief"))}</textarea></label></div>
      <label class="field"><span>Style line, added to every shot's image prompt</span><input type="text" id="fStyle" value="${esc(setupVal("style"))}" placeholder="e.g. 35mm film, muted teal and rust grade, soft grain"></label>
      <h2 style="margin-top:22px">Global references</h2>
      <p class="muted small" style="margin-top:-6px">Characters, places and props every shot uses. Name each one the way the storyboard should refer to it. You can switch any of them off per section.</p>
      <div class="refs-grid">${m.refs.map((r) => `
        <div class="ref-card"><img src="${fileUrl("refs/" + r.file)}" alt="">
          <input type="text" value="${esc(refLabels[r.id] ?? r.label)}" data-ref-label="${r.id}" aria-label="Reference name">
          <button class="x" data-ref-del="${r.id}">Remove</button></div>`).join("")}</div>
      <div class="dropzone" id="refDrop" tabindex="0">Drop reference images here, or click to choose</div>
      <input type="file" id="refFile" accept="image/png,image/jpeg,image/webp" multiple hidden>
      <h2 style="margin-top:22px">Models</h2>
      <div class="models" id="models"></div>
      <div class="step"><button class="btn primary" id="btnSaveSetup">Save setup</button><span class="muted small" id="saveNote"></span></div>
    </div>
  </div>`;

  for (const [id, field] of Object.entries(SETUP_FIELDS)) {
    const input = $("#" + id);
    const changed = () => { S.setupDraft[field] = input.value; noteSave("Unsaved changes"); clearTimeout(setupTimer); setupTimer = setTimeout(saveSetup, 800); };
    input.oninput = changed;
    input.onchange = changed;
    input.onblur = () => { if (Object.keys(S.setupDraft).length) { clearTimeout(setupTimer); saveSetup(); } };
  }
  const pick = (drop, input, handler) => {
    drop.onclick = () => input.click();
    drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); handler([...e.dataTransfer.files]); };
    input.onchange = () => handler([...input.files]);
  };
  pick($("#audioDrop"), $("#audioFile"), (files) => files[0] && act(async () => {
    $("#audioDrop").textContent = `Uploading ${files[0].name}…`;
    setState(await api("POST", `songs/${S.slug}/audio`, files[0], { "X-Filename": files[0].name }));
    toast("Song uploaded.");
  }));
  pick($("#refDrop"), $("#refFile"), (files) => act(async () => {
    for (const f of files) setState(await api("POST", `songs/${S.slug}/refs`, f, { "X-Filename": f.name, "X-Label": f.name.replace(/\.[^.]+$/, "") }));
  }));
  $$("[data-ref-label]").forEach((i) => (i.onchange = () => act(async () => setState(await api("PATCH", `songs/${S.slug}/refs/${i.dataset.refLabel}`, { label: i.value })))));
  $$("[data-ref-del]").forEach((b) => (b.onclick = () => act(async () => setState(await api("DELETE", `songs/${S.slug}/refs/${b.dataset.refDel}`)))));
  $("#btnSaveSetup").onclick = () => saveSetup().then((ok) => ok && toast("Setup saved."));
  renderModels();
  if (focus) {
    const t = $("#" + focus.id);
    if (t) { t.focus(); try { t.setSelectionRange(focus.a, focus.b); } catch (_) {} }
  }
  noteSave(Object.keys(S.setupDraft).length ? "Unsaved changes" : "");
  renderSetupStatus();
}

function noteSave(text) {
  const n = $("#saveNote");
  if (n) n.textContent = text;
}

// --------------------------------------------------------- model pickers
// Lists come from OpenRouter (via /api/models) so names and prices are current.
// "Other model ID…" keeps any model reachable by typing its ID.
function setupDraftSet(field, value) {
  S.setupDraft[field] = value;
  noteSave("Unsaved changes");
  clearTimeout(setupTimer);
  setupTimer = setTimeout(saveSetup, 800);
}

function renderModels() {
  const el = $("#models");
  if (!el) return;
  if (!S.models) {
    el.innerHTML = `<p class="muted small">Loading the model lists…</p>`;
    if (!S.modelsLoading) {
      S.modelsLoading = true;
      api("GET", "models").then((m) => { S.models = m; }).catch(() => { S.models = { llm: [], image: [], video: [], still_costs: {} }; })
        .finally(() => { S.modelsLoading = false; renderModels(); });
    }
    return;
  }
  const M = S.models;
  const vid = M.video.find((v) => v.id === setupVal("video_model"));
  const res = setupVal("video_resolution");
  const perSec = (v, r) => (v?.per_second?.[r] != null ? `$${v.per_second[r].toFixed(3).replace(/0$/, "")}/s` : "");
  const picker = (kind, label, list, optLabel, hint) => {
    const field = MODEL_FIELDS[kind], cur = setupVal(field);
    const known = list.some((o) => o.id === cur);
    const other = S.modelOther?.[kind] || !known;
    return `<label class="field model-${kind}"><span>${label}</span>
      <select data-model="${kind}">
        ${list.map((o) => `<option value="${esc(o.id)}" ${!other && o.id === cur ? "selected" : ""}>${esc(optLabel(o))}</option>`).join("")}
        <option value="" ${other ? "selected" : ""}>Other model ID…</option>
      </select>
      ${other ? `<input type="text" data-model-id="${kind}" value="${esc(cur)}" placeholder="provider/model-id" aria-label="${label} model ID" style="margin-top:6px">` : ""}
      <small class="hint">${other ? "Any OpenRouter model ID." : hint}</small></label>`;
  };
  const llm = M.llm.find((o) => o.id === setupVal("llm_model"));
  const cost = M.still_costs?.[setupVal("image_model")];
  const imgLocal = setupVal("image_model").startsWith("local/");
  const resList = vid ? vid.resolutions : ["480p", "720p", "1080p"];
  el.innerHTML = `
    ${picker("transcribe", "Transcription", M.transcribe || [], (o) => o.name,
      (M.transcribe || []).find((o) => o.id === setupVal("transcribe_model"))?.note || "Times the lyrics. Change it before transcribing.")}
    ${picker("llm", "Storyboard", M.llm, (o) => o.name, llm ? llm.price : "Writes the storyboard and prompt changes.")}
    ${picker("image", "Stills", M.image, (o) => o.name, imgLocal
      ? (M.sidecar ? `Free · the local model is running (${esc(M.sidecar.mode)} mode).` : `<b class="warn">The local model isn't running.</b> ${esc(M.sidecar_help)}`)
      : cost ? `About $${cost.toFixed(3)} a still on your songs so far.` : "Price shows here after its first still.")}
    ${picker("video", "Video", M.video, (o) => `${o.name} (from ${perSec(o, o.resolutions.reduce((a, r) => (o.per_second[r] < o.per_second[a] ? r : a)))})`,
      vid ? `${perSec(vid, res) || "Price unknown"} at ${esc(res)} · clips up to ${vid.max_seconds} s` : "Only models that can animate a still are listed.")}
    <label class="field"><span>Video resolution</span>
      <select data-res>${resList.map((r) => `<option value="${r}" ${r === res ? "selected" : ""}>${r}${vid ? ` · ${perSec(vid, r)}` : ""}</option>`).join("")}
        ${resList.includes(res) ? "" : `<option value="${esc(res)}" selected>${esc(res)} (not offered by this model)</option>`}</select>
      <small class="hint">Export is 854×480, so 480p is enough unless you'll reuse the clips.</small></label>`;

  $$("[data-model]", el).forEach((sel) => (sel.onchange = () => {
    const kind = sel.dataset.model;
    S.modelOther = { ...S.modelOther, [kind]: !sel.value };
    if (sel.value) {
      setupDraftSet(MODEL_FIELDS[kind], sel.value);
      if (kind === "video") {   // keep the resolution one the new model offers
        const v = M.video.find((x) => x.id === sel.value);
        if (v && !v.resolutions.includes(setupVal("video_resolution"))) setupDraftSet("video_resolution", v.resolutions[0]);
      }
    }
    if (kind === "image" && sel.value.startsWith("local/")) {   // is the sidecar up right now?
      api("GET", "models").then((m) => { S.models = m; renderModels(); }).catch(() => {});
    }
    renderModels();
    if (!sel.value) $(`[data-model-id="${kind}"]`)?.focus();
  }));
  $$("[data-model-id]", el).forEach((inp) => {
    inp.oninput = () => setupDraftSet(MODEL_FIELDS[inp.dataset.modelId], inp.value.trim());
    inp.onblur = () => { if (Object.keys(S.setupDraft).length) { clearTimeout(setupTimer); saveSetup(); } };
  });
  $("[data-res]", el).onchange = (e) => { setupDraftSet("video_resolution", e.target.value); renderModels(); };
}

async function saveSetup() {
  clearTimeout(setupTimer);
  const sent = { ...S.setupDraft }, slug = S.slug;
  if (!Object.keys(sent).length) return true;
  noteSave("Saving…");
  const ok = await act(async () => {
    const st = await api("PUT", `songs/${slug}/setup`, sent);
    // Keep anything typed while the save was in flight.
    if (slug === S.slug) for (const [f, v] of Object.entries(sent)) if (S.setupDraft[f] === v) delete S.setupDraft[f];
    if (st.slug === S.slug) {
      $(".topbar .title") && ($(".topbar .title").textContent = st.meta.title || st.slug);
      setState(st);
    }
    return true;
  });
  noteSave(ok ? (Object.keys(S.setupDraft).length ? "Unsaved changes" : "All changes saved") : "Not saved. Check the message and try again.");
  return ok;
}

function renderSetupStatus() {
  const st = S.st, el = $("#setupStatus");
  if (!el) return;
  const busy = running("transcribe");
  const task = st.tasks.find((t) => t.kind === "transcribe" && t.state === "running");
  const kept = st.plan.some((s) => s.stills.length || s.locked);
  el.innerHTML = `
    <button class="btn primary" id="btnTranscribe" ${busy || !st.has_audio ? "disabled" : ""}>${busy ? esc(task?.msg || "Transcribing…") : st.lines.length ? "Transcribe again" : "Transcribe and match lyrics"}</button>
    ${st.has_transcript ? `<button class="btn" id="btnAlign" ${busy ? "disabled" : ""}>Match lyrics again (free)</button>` : ""}
    ${st.lines.length ? `<a class="btn quiet" href="#/song/${S.slug}/timeline">${st.lines.length} lines timed. Open the timeline</a>` : ""}
    ${kept && st.lines.length ? `<p class="muted small" style="width:100%;margin:4px 0 0">Matching again updates the caption track only; your sections and their stills stay as they are.</p>` : ""}`;
  const go = (path) => act(async () => { if (await saveSetup()) setState(await api("POST", `songs/${S.slug}/${path}`)); });
  $("#btnTranscribe") && ($("#btnTranscribe").onclick = () => go("transcribe"));
  $("#btnAlign") && ($("#btnAlign").onclick = () => go("align").then(() => toast("Lyrics matched again.")));
}

// ============================================================== timeline
function fitPps() {
  const w = ($("#timeline")?.clientWidth || window.innerWidth) - 20;
  return S.st?.length ? clamp(w / S.st.length, 2, 300) : 40;
}

function renderTimeline() {
  const st = S.st, inner = $("#tlInner");
  if (!inner) return;
  const pps = S.pps, L = st.length || 1;
  inner.style.width = `${L * pps + 30}px`;
  const step = [1, 2, 5, 10, 15, 30, 60].find((s) => s * pps >= 56) || 60;
  let ticks = "";
  for (let t = 0; t <= L; t += step) ticks += `<div class="tick" style="left:${t * pps}px">${tcShort(t)}</div>`;

  const caps = st.lines.map((l, i) => `
    <div class="capline ${l.low && !l.confirmed ? "low" : ""}" data-line="${i}" style="left:${l.start * pps}px;width:${Math.max(4, (l.end - l.start) * pps)}px"
      title="${esc(l.text)}${l.low && !l.confirmed ? "\nTiming is a guess here. Drag the edges to fix it, or double-click to confirm it's right." : "\nDrag the edges to adjust timing."}">
      <span class="h l" data-cap-edge="start"></span>${esc(l.text)}<span class="h r" data-cap-edge="end"></span></div>`).join("");

  const sel = new Set(S.sel);
  const secs = st.plan.map((s) => {
    const still = stillOf(s);
    const busyStill = running("still", s.uid);
    const label = s.kind === "gap" && !captionText(s.start, s.end) ? "Instrumental" : captionText(s.start, s.end) || "No lyrics";
    let badge = "";
    if (s.job) badge = s.job.state === "unknown" ? "Job unknown" : "Rendering clip";
    else if (s.static) badge = "Static";
    else if (s.clip !== null && !s.stale) badge = "Clip";
    return `<div class="sec ${s.kind === "gap" ? "gap" : ""} ${sel.has(s.uid) ? "sel" : ""} ${s.locked ? "locked" : ""}" data-uid="${s.uid}"
        style="left:${s.start * pps}px;width:${Math.max(3, len(s) * pps - 1)}px" title="${esc(tc(s.start) + " – " + tc(s.end) + "  " + label)}">
      <canvas data-wave="${s.uid}" data-wave-a="${s.start}" data-wave-b="${s.end}"></canvas>
      <div class="thumb">${still ? `<img src="${fileUrl("stills/" + still.file)}" alt="" loading="lazy" draggable="false">` : ""}
        ${badge && len(s) * pps > 64 ? `<span class="badge">${badge}</span>` : ""}${busyStill ? `<div class="spin"><i></i></div>` : ""}</div>
      <div class="lab"><span class="dot ${s.status}" title="${STATUS_TEXT[s.status]}"></span><span class="txt">${esc(label)}</span></div>
    </div>`;
  }).join("");

  // Empty stretches keep their waveform so you can see what's there.
  let holes = "", cursor = 0;
  for (const s of [...st.plan, { start: L, end: L }]) {
    if (s.start - cursor > 0.05) {
      holes += `<div class="hole" style="left:${cursor * pps}px;width:${(s.start - cursor) * pps}px" title="Empty: drag across it to add a gap section${st.meta.timing_locked ? "" : ", or drag a neighbour's edge over it"}">
        <canvas data-wave-a="${cursor}" data-wave-b="${s.start}" data-dim="1"></canvas>${(s.start - cursor) * pps > 90 ? `<span>Empty. Drag to add a gap section</span>` : ""}</div>`;
    }
    cursor = Math.max(cursor, s.end);
  }

  // One handle per boundary: shared when sections touch, per edge otherwise.
  // No handles at all while timing is locked, so a stray drag can't move an edge.
  let bounds = "";
  if (!st.meta.timing_locked) st.plan.forEach((s, k) => {
    const prev = st.plan[k - 1];
    if (!prev || Math.abs(prev.end - s.start) > 0.02) {
      if (prev) bounds += `<div class="bound" data-b-uid="${prev.uid}" data-b-edge="end" style="left:${prev.end * pps}px"></div>`;
      bounds += `<div class="bound" data-b-uid="${s.uid}" data-b-edge="start" style="left:${s.start * pps}px"></div>`;
    } else bounds += `<div class="bound" data-b-uid="${s.uid}" data-b-edge="start" style="left:${s.start * pps}px"></div>`;
  });
  const last = st.plan[st.plan.length - 1];
  if (last && !st.meta.timing_locked) bounds += `<div class="bound" data-b-uid="${last.uid}" data-b-edge="end" style="left:${last.end * pps}px"></div>`;

  inner.innerHTML = `<div class="ruler" id="ruler">${ticks}</div><div class="captions">${caps}</div>
    <div class="track" id="track">${holes}${secs}${bounds}</div><div class="playhead" id="playhead"></div>`;
  drawWaves();
  placePlayhead();
  if (!st.plan.length) {
    inner.insertAdjacentHTML("beforeend", `<p class="muted" style="position:absolute;left:16px;top:120px">${st.has_audio ? "Transcribe the song in Setup to cut it into sections." : "Upload the song in Setup to start."}</p>`);
  }
}

const STATUS_TEXT = {
  failed: "Failed or job unknown", stale: "Clip out of date", check: "Caption timing is a guess",
  split: "Split needed: longer than the clip limit", locked: "Locked", approved: "Approved", draft: "Draft",
};

function drawWaves() {
  const P = S.peaks;
  if (!P) return;
  const dpr = window.devicePixelRatio || 1;
  $$("canvas[data-wave-a]").forEach((c) => {
    const a = +c.dataset.waveA, s = c.dataset.wave ? secById(c.dataset.wave) : null;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w) return;
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    const g = c.getContext("2d");
    g.scale(dpr, dpr);
    g.fillStyle = c.dataset.dim ? "#3b4a63" : s && S.sel.includes(s.uid) ? "#b9cdf0" : s?.kind === "gap" ? "#4f6385" : "#8ea8d2";
    const per = P.per_second, mid = h / 2;
    for (let x = 0; x < w; x += 2) {
      const i0 = Math.floor((a + x / S.pps) * per), i1 = Math.max(i0 + 1, Math.floor((a + (x + 2) / S.pps) * per));
      let v = 0;
      for (let i = i0; i < i1 && i < P.peaks.length; i++) v = Math.max(v, P.peaks[i]);
      const bh = Math.max(1, v * (h - 6));
      g.fillRect(x, mid - bh / 2, 1.4, bh);
    }
  });
}

function placePlayhead() {
  const ph = $("#playhead");
  if (ph) ph.style.left = `${S.t * S.pps}px`;
  const time = $("#time");
  if (time) time.textContent = `${tc(S.t)} / ${tc(S.st?.length)}`;
}

function xToT(e) {
  const r = $("#tlInner").getBoundingClientRect();
  return clamp((e.clientX - r.left) / S.pps, 0, S.st.length);
}

function bindTimeline() {
  const tl = $("#timeline");
  tl.addEventListener("mousedown", (e) => {
    if (e.button === 1) return dragPan(e);
    if (e.button !== 0) return;
    const capEdge = e.target.closest("[data-cap-edge]");
    const bound = e.target.closest(".bound");
    const sec = e.target.closest(".sec");
    const cap = e.target.closest(".capline");
    if (capEdge) return dragCaption(e, +cap.dataset.line, capEdge.dataset.capEdge);
    if (bound) return dragBound(e, bound);
    if (sec) return clickSection(sec.dataset.uid, e.shiftKey);
    if (cap) { seek(S.st.lines[+cap.dataset.line].start); return; }
    if (e.target.closest("#ruler")) return dragSeek(e);
    if (e.target.closest("#track")) return dragGap(e);
  });
  tl.addEventListener("dblclick", (e) => {
    const cap = e.target.closest(".capline");
    if (cap && !e.target.closest("[data-cap-edge]")) confirmLines([+cap.dataset.line]);
  });
  // The wheel zooms; Shift+wheel (or a sideways trackpad swipe) scrolls along the song.
  tl.addEventListener("wheel", (e) => {
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    e.preventDefault();
    const dy = e.deltaMode === 1 ? e.deltaY * 40 : e.deltaY;   // lines → pixels
    zoomAt(Math.exp(clamp(-dy, -120, 120) * 0.002));
  }, { passive: false });
}

function trackDrag(onMove, onUp) {
  const move = (e) => onMove(e);
  const up = (e) => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); S.drag = null; onUp(e); };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

// Hold the middle button and drag to slide along the song.
function dragPan(e) {
  e.preventDefault();   // no autoscroll widget or Linux middle-click paste
  const tl = $("#timeline"), x0 = e.clientX, left0 = tl.scrollLeft;
  S.drag = "pan";
  tl.classList.add("panning");
  trackDrag((ev) => { tl.scrollLeft = left0 - (ev.clientX - x0); }, () => tl.classList.remove("panning"));
}

function dragSeek(e) {
  seek(xToT(e));
  S.drag = "seek";
  trackDrag((ev) => seek(xToT(ev)), () => {});
}

function dragCaption(e, i, edge) {
  e.stopPropagation();
  const el = e.target.closest(".capline"), line = S.st.lines[i];
  S.drag = "caption";
  let t = line[edge];
  trackDrag((ev) => {
    t = xToT(ev);
    const a = edge === "start" ? t : line.start, b = edge === "end" ? t : line.end;
    el.style.left = `${a * S.pps}px`;
    el.style.width = `${Math.max(4, (b - a) * S.pps)}px`;
  }, () => act(async () => setState(await api("PATCH", `songs/${S.slug}/lines/${i}`, { [edge]: t, confirmed: true }))).then(() => !S.st && renderTimeline()));
}

function dragBound(e, el) {
  const id = el.dataset.bUid, edge = el.dataset.bEdge;
  const s = secById(id);
  if (S.st.meta.timing_locked) return toast("Timing is locked. Untick Lock timing in the toolbar to drag section edges.", true);
  if (s.locked) return toast("That section is locked. Unlock it to retime.", true);
  S.drag = "bound";
  el.classList.add("drag");
  let t = s[edge];
  trackDrag((ev) => { t = xToT(ev); el.style.left = `${t * S.pps}px`; }, () => {
    el.classList.remove("drag");
    if (Math.abs(t - s[edge]) < 0.02) return renderTimeline();
    act(async () => setState(await api("POST", `songs/${S.slug}/retime`, { uid: id, edge, t }))).then(renderTimeline);
  });
}

function dragGap(e) {
  const t0 = xToT(e);
  S.drag = "gap";
  const band = document.createElement("div");
  band.className = "rubber";
  $("#track").append(band);
  let t1 = t0;
  trackDrag((ev) => {
    t1 = xToT(ev);
    band.style.left = `${Math.min(t0, t1) * S.pps}px`;
    band.style.width = `${Math.abs(t1 - t0) * S.pps}px`;
  }, () => {
    band.remove();
    if (Math.abs(t1 - t0) * S.pps < 6) { seek(t0); S.sel = []; return update(); }
    act(async () => {
      const r = await api("POST", `songs/${S.slug}/gap`, { start: Math.min(t0, t1), end: Math.max(t0, t1) });
      S.sel = [r.selected];
      setState(r);
      toast("Gap section added.");
    }).then(renderTimeline);
  });
}

function clickSection(id, shift) {
  if (shift && S.anchor && secById(S.anchor)) {
    const ids = plan().map((s) => s.uid);
    const [a, b] = [ids.indexOf(S.anchor), ids.indexOf(id)].sort((x, y) => x - y);
    S.sel = ids.slice(a, b + 1);
  } else {
    S.sel = [id];
    S.anchor = id;
    if (!S.playing) seek(secById(id).start + 0.01);
  }
  S.proposal = null;
  update();
}

function step(dir) {
  const p = plan();
  if (!p.length) return;
  const cur = S.sel.length ? p.findIndex((s) => s.uid === S.sel[S.sel.length - 1]) : -1;
  const next = p[clamp(cur + dir, 0, p.length - 1)];
  clickSection(next.uid, false);
  scrollIntoView(next.start, next.end);
}

function scrollIntoView(a, b) {
  const tl = $("#timeline");
  if (!tl) return;
  const x0 = a * S.pps, x1 = b * S.pps;
  if (x0 < tl.scrollLeft || x1 > tl.scrollLeft + tl.clientWidth) tl.scrollLeft = x0 - 60;
}

// Zoom around the playhead: it stays where it is on screen, or is brought
// to the middle if it was scrolled out of view.
function zoomAt(factor) {
  const tl = $("#timeline");
  const pps = clamp(S.pps * factor, 2, 300);
  if (pps === S.pps) return;
  let x = S.t * S.pps - tl.scrollLeft;
  if (x < 0 || x > tl.clientWidth) x = tl.clientWidth / 2;
  S.pps = pps;
  renderTimeline();
  tl.scrollLeft = S.t * S.pps - x;
}

// ============================================================== playback
function seek(t) {
  S.t = clamp(t, 0, S.st?.length || 0);
  audio.currentTime = S.t;
  placePlayhead();
  updateScreen();
}

function togglePlay() {
  if (!S.st?.has_audio) return;
  if (S.playing) {
    audio.pause();
    S.playing = false;
  } else {
    audio.currentTime = S.t;
    audio.play();
    S.playing = true;
    requestAnimationFrame(tick);
  }
  $("#btnPlay").textContent = S.playing ? "❚❚" : "▶";
  $("#btnPlay").setAttribute("aria-label", S.playing ? "Pause" : "Play");
  updateScreen(true);
}
audio.addEventListener("ended", () => { if (S.playing) togglePlay(); });

function tick() {
  if (!S.playing) return;
  S.t = audio.currentTime;
  placePlayhead();
  const tl = $("#timeline"), x = S.t * S.pps;
  if (tl && (x > tl.scrollLeft + tl.clientWidth - 40 || x < tl.scrollLeft)) tl.scrollLeft = x - 80;
  updateScreen();
  requestAnimationFrame(tick);
}

let shown = { key: null };
function updateScreen(force) {
  const img = $("#scrImg"), vid = $("#scrVid"), empty = $("#scrEmpty");
  if (!img) return;
  const s = sectionAt(S.t);
  const line = (S.st.lines || []).find((l) => l.start <= S.t && S.t < l.end);
  $("#scrCap").textContent = line ? line.text : "";
  const clip = s && S.previewMode === "clips" && !s.static && s.clip !== null && !s.stale ? clipOf(s) : null;
  const still = s && stillOf(s);
  const key = clip ? "c:" + clip.file : still ? "s:" + still.file : "e:" + (s ? s.uid : "none");
  if (key !== shown.key || force) {
    shown.key = key;
    img.hidden = vid.hidden = empty.hidden = true;
    if (clip) {
      if (!vid.src.endsWith(clip.file)) vid.src = fileUrl("raw/" + clip.file);
      vid.hidden = false;
      $("#scrSrc").textContent = `Clip ${clip.n}`;
    } else if (still) {
      img.src = fileUrl("stills/" + still.file);
      img.hidden = false;
      $("#scrSrc").textContent = `Still ${still.n}${s.static ? ", static" : ""}`;
    } else {
      empty.hidden = false;
      empty.textContent = !S.st.has_audio ? "Upload the song in Setup to start." : !s ? "Nothing here. Drag across this stretch on the timeline to add a gap section." : s.image_prompt ? "No still yet. Generate one from the inspector." : "No still yet. Write the storyboard first.";
      $("#scrSrc").textContent = "";
    }
  }
  if (clip) {
    const want = S.t - s.start;
    if (!vid.duration || want <= vid.duration) {
      if (Math.abs(vid.currentTime - want) > 0.3 || !S.playing) { try { vid.currentTime = want; } catch (_) {} }
    }
    if (S.playing && vid.paused) vid.play().catch(() => {});
    if (!S.playing && !vid.paused) vid.pause();
  }
}

// ============================================================= toolbar
function renderToolbar() {
  const el = $("#toolbar");
  const sel = S.sel.map(secById).filter(Boolean);
  const one = sel.length === 1 ? sel[0] : null;
  el.innerHTML = `
    <button class="btn small" data-z="out" aria-label="Zoom out">−</button>
    <button class="btn small" data-z="in" aria-label="Zoom in">+</button>
    <button class="btn small" data-z="fit">Fit song</button>
    <label class="tlock" title="Stops section edges from being dragged. Delete, split, merge and adding gaps still work."><input type="checkbox" id="tbLock" ${S.st.meta.timing_locked ? "checked" : ""}> Lock timing</label>
    <span class="sep"></span>
    <span class="selinfo">${sel.length ? `${sel.length} selected` : "Nothing selected"}</span>
    <button class="btn small" id="tbSplit" ${one && !one.locked ? "" : "disabled"}>Split into shots…</button>
    <button class="btn small" id="tbMerge" ${sel.length > 1 ? "" : "disabled"}>Merge</button>
    <button class="btn small" id="tbApprove" ${sel.length ? "" : "disabled"}>Approve</button>
    <button class="btn small" id="tbRegen" ${sel.length ? "" : "disabled"}>Regenerate stills</button>
    <button class="btn small" id="tbAnimate" ${sel.length ? "" : "disabled"}>Animate selection…</button>
    <button class="btn small danger" id="tbDelete" ${one && !one.locked ? "" : "disabled"}>Delete</button>
    <span class="legend">${["approved", "draft", "check", "split", "stale", "failed"].map((k) => `<span><i class="dot ${k}"></i>${STATUS_TEXT[k].split(":")[0]}</span>`).join("")}</span>`;
  $$("[data-z]").forEach((b) => (b.onclick = () => b.dataset.z === "fit" ? (S.pps = fitPps(), renderTimeline()) : zoomAt(b.dataset.z === "in" ? 1.5 : 1 / 1.5)));
  $("#tbLock").onchange = (e) => act(async () => { setState(await api("PUT", `songs/${S.slug}/setup`, { timing_locked: e.target.checked })); renderTimeline(); });
  $("#tbSplit").onclick = () => openSplit(one.uid);
  $("#tbMerge").onclick = () => act(async () => { const r = await api("POST", `songs/${S.slug}/merge`, { uids: S.sel }); S.sel = [r.selected]; setState(r); toast("Merged."); });
  $("#tbApprove").onclick = () => approve(S.sel);
  $("#tbRegen").onclick = () => generateStills(S.sel);
  $("#tbAnimate").onclick = () => openAnimate(S.sel);
  $("#tbDelete").onclick = () => {
    if (!confirm("Delete this section? Its stills and clips stay on disk, and the stretch becomes empty.")) return;
    act(async () => { const id = one.uid; setState(await api("DELETE", `songs/${S.slug}/sections/${id}`)); });
  };
}

async function approve(uids, approved = true) {
  return act(async () => {
    const r = await api("POST", `songs/${S.slug}/approve`, { uids, approved });
    setState(r);
    if (r.skipped.length) toast(`Approved ${r.done}. Skipped: ${r.skipped.join("; ")}`, true);
    else if (uids.length > 1) toast(`Approved ${r.done} sections.`);
  });
}

async function generateStills(uids, model) {
  return act(async () => {
    const since = Date.now() / 1000 - 2;
    const r = await api("POST", `songs/${S.slug}/stills`, model ? { uids, model } : { uids });
    setState(r);
    if (r.started > 1) openBusy("still", since, r.started);
    if (r.skipped.length) toast(`Skipped: ${r.skipped.join("; ")}`, true);
  });
}

function generateMissing() {
  const p = plan();
  const why = !p.length ? "Transcribe the song first. Stills are made per section, and sections come from the timed lyrics."
    : running("storyboard") ? "The storyboard is still being written. Stills can be made once it's done."
    : !p.some((s) => s.image_prompt) ? "Write the storyboard first. Each still is made from its section's image prompt, and the storyboard writes them."
    : null;
  if (why) return toast(why, true);
  const open = p.filter((s) => s.still === null && !s.locked && !running("still", s.uid));
  if (!open.length) return toast(p.some((s) => s.still === null) ? "The sections without a still are locked or already generating." : "Every section already has a still. Select sections and use Regenerate stills to make new versions.");
  const ready = open.filter((s) => s.image_prompt);
  if (!ready.length) return toast(`${open.length} section${open.length > 1 ? "s have" : " has"} no image prompt yet. Rewrite the storyboard, or write a prompt in the inspector.`, true);
  generateStills(null);
}

// "Regenerate with…": make these stills with another image model, without
// changing the song's Stills model. Each still remembers the model it came from.
async function openRegenWith(uids) {
  if (!S.models) S.models = await act(() => api("GET", "models"));
  if (!S.models) return;
  const M = S.models, songModel = S.st.meta.image_model;
  const list = [...M.image];
  if (!list.some((o) => o.id === songModel)) list.unshift({ id: songModel, name: songModel });
  let pick = S.regenModel && list.some((o) => o.id === S.regenModel) ? S.regenModel : (list.find((o) => o.id !== songModel) || list[0]).id;
  const price = (o) => o.local ? (M.sidecar ? "Free, on your GPU" : "Free, but the local model isn't running")
    : M.still_costs?.[o.id] != null ? `about ${money(M.still_costs[o.id])} a still` : "price shows after its first still";
  const n = uids.length, dlg = $("#dlg");
  dlg.innerHTML = `
    <div class="dh"><h2>${n > 1 ? `Regenerate ${n} stills with…` : "Regenerate with…"}</h2>
      <p class="muted small" style="margin:4px 0 0">Same prompt and references, another image model. The song's Stills model stays ${esc(list.find((o) => o.id === songModel)?.name || songModel)}; the new version is added alongside the others.</p></div>
    <div class="db"><div class="regen-list">${list.map((o) => `
      <label class="regen-row"><input type="radio" name="rw" value="${esc(o.id)}" ${o.id === pick ? "checked" : ""}>
        <span class="nm">${esc(o.name)}${o.id === songModel ? ` <span class="muted small">(song's model)</span>` : ""}</span>
        <span class="muted small">${price(o)}</span></label>`).join("")}</div></div>
    <div class="df"><button class="btn quiet" id="rwCancel">Cancel</button><button class="btn primary" id="rwGo"></button></div>`;
  const label = () => {
    const o = list.find((x) => x.id === pick);
    $("#rwGo").textContent = `Generate ${n > 1 ? `${n} stills` : "still"} with ${o?.name || pick}`;
  };
  $$("[name=rw]", dlg).forEach((r) => (r.onchange = () => { pick = r.value; label(); }));
  $("#rwCancel").onclick = () => dlg.close();
  $("#rwGo").onclick = () => { dlg.close(); S.regenModel = pick; generateStills(uids, pick); };
  label();
  dlg.showModal();
}

function writeStoryboard() {
  const redo = plan().some((s) => s.image_prompt);
  if (redo && !confirm("Rewrite the storyboard for every unlocked section? Beats and prompts you edited by hand are kept, and no stills are generated.")) return;
  act(async () => { const since = Date.now() / 1000 - 2; setState(await api("POST", `songs/${S.slug}/storyboard`)); openBusy("storyboard", since); });
}

// =========================================================== inspector
function renderInspector() {
  const el = $("#inspector");
  if (!el) return;
  const sel = S.sel.map(secById).filter(Boolean);
  const s = sel.length === 1 ? sel[0] : null;
  const key = JSON.stringify([S.sel, s, S.proposal, S.est[s?.uid], s && running("still", s.uid), S.st.meta.refs, sel.length > 1 && sel.map((x) => x.status)]);
  if (key === S.inspKey) return;
  S.inspKey = key;
  const focus = document.activeElement?.dataset?.field && el.contains(document.activeElement)
    ? { f: document.activeElement.dataset.field, a: document.activeElement.selectionStart, b: document.activeElement.selectionEnd, top: el.scrollTop } : null;

  if (!sel.length) {
    el.innerHTML = `<h3>No section selected</h3><p class="muted">Click a section on the timeline to see its story beat, prompts, references and versions. Shift-click selects a range. Drag across an empty stretch to make a gap section.</p>
      ${S.st.meta.rule ? `<div class="block"><div class="bh"><b>Visual rule</b></div><p>${esc(S.st.meta.rule)}</p></div>` : ""}`;
    return;
  }
  if (!s) return renderBulk(el, sel);
  if (!(s.uid in S.est)) loadEstimate(s.uid);

  const d = S.drafts[s.uid] || {};
  const val = (f) => (f in d ? d[f] : s[f]);
  const dirty = Object.keys(d).some((f) => d[f] !== s[f]);
  const busy = running("still", s.uid);
  const lyric = captionText(s.start, s.end);
  const lock = s.locked ? "disabled" : "";
  const est = S.est[s.uid];
  const clipReady = est && est.status === "ready";

  el.innerHTML = `
  <div class="insp-head">
    <div><h3>${tc(s.start)} – ${tc(s.end)}</h3></div>
    <div class="nav"><button class="btn small quiet" data-step="-1" aria-label="Previous section">◀</button><button class="btn small quiet" data-step="1" aria-label="Next section">▶</button></div>
  </div>
  <div class="insp-sub">${len(s).toFixed(2)} s · ${s.kind === "gap" ? "Gap" : "Lyric"} · <span class="dot ${s.status}"></span> ${STATUS_TEXT[s.status]}</div>
  ${lyric ? `<p class="lyric-q">${esc(lyric)}</p>` : ""}
  ${notices(s)}

  <div class="block">
    <div class="bh"><b>Still</b><span class="muted small">${s.stills.length ? `${s.stills.length} version${s.stills.length > 1 ? "s" : ""}` : "none yet"}</span>
      <span class="r"><label class="check small"><input type="checkbox" id="iApproved" ${s.approved ? "checked" : ""} ${s.still === null ? "disabled" : ""}> Approved</label></span></div>
    ${s.stills.length ? `<div class="versions">${[...s.stills].reverse().map((v) => `
      <button data-still="${v.n}" aria-pressed="${v.n === s.still}" title="${esc(v.model)}${v.cost ? " · " + money(v.cost) : ""}" ${lock}><img src="${fileUrl("stills/" + v.file)}" alt="Still ${v.n}"><span class="n">${v.n}</span></button>`).join("")}</div>` : ""}
    <div class="actions">
      <button class="btn primary" id="iRegen" ${busy || s.locked ? "disabled" : ""}>${busy ? "Generating…" : s.stills.length ? "Regenerate still" : "Generate still"}</button>
      <button class="btn" id="iRegenWith" ${busy || s.locked ? "disabled" : ""}>${s.stills.length ? "Regenerate" : "Generate"} with…</button>
      ${dirty ? `<span class="muted small" style="align-self:center">Uses the saved prompt. Save first to use your edits.</span>` : ""}
    </div>
  </div>

  ${s.static ? "" : `<div class="block">
    <div class="bh"><b>Clip</b><span class="muted small">${s.clips.length ? `${s.clips.length} made` : "none yet"}</span></div>
    ${s.clips.length ? `<div class="clips-list">${s.clips.map((c) => `<button class="btn small" data-clip="${c.n}" aria-pressed="${c.n === s.clip}" style="${c.n === s.clip ? "border-color:var(--amber)" : ""}" ${lock}>Clip ${c.n}, from still ${c.still ?? "?"}</button>`).join("")}</div>` : ""}
    <div class="actions">
      <button class="btn" id="iAnimate" ${clipReady ? "" : "disabled"}>${s.clips.length ? "Re-roll clip" : "Animate"}${est?.cost != null && clipReady ? ` · ${money(est.cost)}` : ""}</button>
      <span class="muted small" style="align-self:center">${est ? clipHint(est, s) : "Checking cost…"}</span>
    </div>
  </div>`}

  <div class="block">
    <div class="bh"><b>Story</b><span class="r">${dirty ? `<span class="edited">Unsaved</span>` : ""}</span></div>
    <label class="field"><span>Story beat: what the viewer should see or feel ${s.edited.beat ? `<span class="edited">edited by hand</span>` : ""}</span>
      <textarea data-field="beat" rows="2" ${lock}>${esc(val("beat"))}</textarea></label>
    <label class="field"><span>Image prompt ${s.edited.image_prompt ? `<span class="edited">edited by hand</span>` : ""}</span>
      <textarea data-field="image_prompt" rows="6" ${lock}>${esc(val("image_prompt"))}</textarea></label>
    ${s.static ? "" : `<label class="field"><span>Motion prompt ${s.edited.video_prompt ? `<span class="edited">edited by hand</span>` : ""}</span>
      <textarea data-field="video_prompt" rows="3" ${lock}>${esc(val("video_prompt"))}</textarea></label>`}
    <label class="field"><span>Extra instructions for this shot only</span>
      <textarea data-field="extra" rows="2" placeholder="e.g. hands in frame, no tiger in this one" ${lock}>${esc(val("extra"))}</textarea></label>
    <div class="actions">
      <button class="btn" id="iSave" ${dirty ? "" : "disabled"}>Save</button>
      <button class="btn" id="iAsk" ${s.locked ? "disabled" : ""}>Ask for a change…</button>
      <button class="btn" id="iPropose" ${s.locked ? "disabled" : ""}>Rewrite prompts from beat</button>
      ${dirty ? `<button class="btn quiet" id="iDiscard">Discard edits</button>` : ""}
    </div>
    ${proposalView(s)}
    ${s.asks?.length ? `<details class="asks"><summary class="muted small">Changes asked for (${s.asks.length})</summary>
      <ul>${s.asks.map((a) => `<li>${esc(a.text)}</li>`).join("")}</ul></details>` : ""}
  </div>

  <div class="block">
    <div class="bh"><b>References</b></div>
    ${S.st.meta.refs.length ? S.st.meta.refs.map((r) => {
      const off = s.refs.off.includes(r.id);
      return `<div class="ref-row ${off ? "off" : ""}"><img src="${fileUrl("refs/" + r.file)}" alt=""><span class="lbl">${esc(r.label)}</span>
        <label class="check small"><input type="checkbox" data-ref-on="${r.id}" ${off ? "" : "checked"} ${lock}> Use</label></div>`;
    }).join("") : `<p class="muted small">No global references. Add them in Setup.</p>`}
    ${s.refs.add.map((r) => `<div class="ref-row"><img src="${fileUrl("refs/" + r.file)}" alt=""><span class="lbl">${esc(r.label)} <span class="muted small">this section only</span></span>
      <button class="btn small quiet" data-ref-rm="${r.id}" ${lock}>Remove</button></div>`).join("")}
    <div class="actions"><button class="btn small" id="iAddRef" ${lock}>Add a reference for this section</button><input type="file" id="iRefFile" accept="image/png,image/jpeg,image/webp" hidden></div>
  </div>

  <div class="block">
    <div class="flags">
      <label class="check"><input type="checkbox" id="iStatic" ${s.static ? "checked" : ""} ${lock}> Static (stays a still with a slow push-in)</label>
      <label class="check"><input type="checkbox" id="iLocked" ${s.locked ? "checked" : ""}> Locked</label>
    </div>
    <div class="actions">
      <button class="btn small" id="iSplit" ${lock}>Split into shots…</button>
      <button class="btn small danger" id="iDelete" ${lock}>Delete section</button>
    </div>
  </div>`;

  bindInspector(s);
  if (focus) {
    const t = el.querySelector(`[data-field="${focus.f}"]`);
    if (t) { t.focus(); try { t.setSelectionRange(focus.a, focus.b); } catch (_) {} }
    el.scrollTop = focus.top;
  }
}

function clipHint(est, s) {
  return {
    ready: s.clips.length ? "The current clip stays as a version." : `${est.seconds} s clip`,
    draft: "Approve the still first.", "no still": "Generate a still first.",
    "split needed": `Longer than the ${S.st.limit} s clip limit. Split it first.`,
    rendering: "A clip is rendering.", unknown: "A job is in an unknown state.", static: "",
  }[est.status] ?? est.status;
}

function notices(s) {
  const out = [];
  if (s.failed) out.push(`<div class="notice nt-failed"><b>${s.failed.what === "still" ? "Still" : "Clip"} failed.</b> ${esc(s.failed.reason)}
    <div class="actions"><button class="btn small" data-retry="${s.failed.what}">Retry</button></div></div>`);
  if (s.job?.state === "unknown") out.push(`<div class="notice nt-failed"><b>Clip job unknown.</b> ${esc(s.job.error || "")} The request may or may not have reached the provider, so it isn't resent automatically.
    <div class="actions"><button class="btn small" id="iMarkFailed">Mark failed</button></div></div>`);
  else if (s.job) out.push(`<div class="notice">Clip rendering (${s.job.seconds} s). This usually takes a minute or two.</div>`);
  if (s.stale) out.push(`<div class="notice nt-stale"><b>Clip out of date.</b> The still changed after this clip was made, or the section is now longer than the clip. Re-roll it, or export will use the still.</div>`);
  if (s.low) {
    const idx = S.st.lines.map((l, i) => (l.low && !l.confirmed && l.start < s.end && l.end > s.start ? i : -1)).filter((i) => i >= 0);
    out.push(`<div class="notice nt-check"><b>Caption timing is a guess here.</b> Few of these words were heard clearly. Drag the caption's edges on the caption track, or confirm it's right.
      <div class="actions"><button class="btn small" data-confirm-lines="${idx.join(",")}">Timing is right</button></div></div>`);
  }
  if (s.split_needed) out.push(`<div class="notice nt-split"><b>Split needed.</b> ${len(s).toFixed(1)} s is longer than the ${S.st.limit} s clip limit. Split it into shots that fit, or make it static.
    <div class="actions"><button class="btn small" id="iSplit2" ${s.locked ? "disabled" : ""}>Split into shots…</button></div></div>`);
  return out.join("");
}

function proposalView(s) {
  const p = S.proposal;
  if (!p || p.uid !== s.uid) return "";
  if (p.loading) return `<div class="proposal"><p class="muted">Writing new prompts from the beat…</p></div>`;
  const row = (f, label) => `<label class="check small"><input type="checkbox" data-accept="${f}" ${p.accept[f] ? "checked" : ""}> Use new ${label}${s.edited[f] ? ` (replaces your hand edit)` : ""}</label>
      <p>${diff(s[f], p[f])}</p>`;
  return `<div class="proposal">
    <p class="muted small">Proposed prompts. Changed words are highlighted. Nothing is generated until you choose.</p>
    ${p.beat !== s.beat ? row("beat", "beat") : ""}
    ${row("image_prompt", "image prompt")}
    ${s.static ? "" : row("video_prompt", "motion prompt")}
    <div class="actions">
      <button class="btn" id="pApply">Apply</button>
      <button class="btn primary" id="pApplyRegen">Apply and regenerate</button>
      <button class="btn quiet" id="pCancel">Cancel</button>
    </div></div>`;
}

function diff(a, b) {
  const A = (a || "").split(/(\s+)/), B = (b || "").split(/(\s+)/);
  const n = A.length, m = B.length;
  if (n * m > 400000) return esc(b);
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  let i = 0, j = 0, out = "";
  while (i < n || j < m) {
    if (i < n && j < m && A[i] === B[j]) { out += esc(B[j]); i++; j++; }
    else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { out += /^\s+$/.test(B[j]) ? B[j] : `<ins>${esc(B[j])}</ins>`; j++; }
    else { if (!/^\s+$/.test(A[i])) out += `<del>${esc(A[i])}</del>`; i++; }
  }
  return out;
}

function loadEstimate(id) {
  S.est[id] = null;
  api("GET", `songs/${S.slug}/estimate?uids=${id}`).then((r) => { S.est[id] = r.rows[0] || null; renderInspector(); }).catch(() => {});
}

function patchSection(id, body) {
  return act(async () => { S.est = {}; setState(await api("PATCH", `songs/${S.slug}/sections/${id}`, body)); });
}

function bindInspector(s) {
  const el = $("#inspector"), id = s.uid;
  $$("[data-step]", el).forEach((b) => (b.onclick = () => step(+b.dataset.step)));
  $$("[data-field]", el).forEach((t) => (t.oninput = () => {
    (S.drafts[id] ||= {})[t.dataset.field] = t.value;
    const dirty = Object.keys(S.drafts[id]).some((f) => S.drafts[id][f] !== s[f]);
    $("#iSave").disabled = !dirty;
  }));
  $$("[data-still]", el).forEach((b) => (b.onclick = () => patchSection(id, { still: +b.dataset.still })));
  $$("[data-clip]", el).forEach((b) => (b.onclick = () => patchSection(id, { clip: +b.dataset.clip })));
  $$("[data-ref-on]", el).forEach((c) => (c.onchange = () => patchSection(id, { ref_off: { id: c.dataset.refOn, off: !c.checked } })));
  $$("[data-ref-rm]", el).forEach((b) => (b.onclick = () => act(async () => setState(await api("DELETE", `songs/${S.slug}/sections/${id}/refs/${b.dataset.refRm}`)))));
  $$("[data-retry]", el).forEach((b) => (b.onclick = async () => {
    await patchRetry(id);
    if (b.dataset.retry === "still") generateStills([id]);
    else animateNow([id]);
  }));
  $$("[data-confirm-lines]", el).forEach((b) => (b.onclick = () => confirmLines(b.dataset.confirmLines.split(",").map(Number))));
  $("#iApproved").onchange = (e) => approve([id], e.target.checked);
  $("#iRegenWith").onclick = () => (s.image_prompt.trim() ? openRegenWith([id]) : $("#iRegen").click());
  $("#iRegen").onclick = () => {
    if (!s.image_prompt.trim()) return toast(plan().some((x) => x.image_prompt)
      ? "This section has no image prompt yet. Write one under Story and save it, or use Ask for a change."
      : "Write the storyboard first. Each still is made from its section's image prompt, and the storyboard writes them.", true);
    generateStills([id]);
  };
  $("#iAnimate") && ($("#iAnimate").onclick = () => animateNow([id]));
  $("#iMarkFailed") && ($("#iMarkFailed").onclick = () => {
    if (!confirm("Mark this job failed? If the provider did receive it, animating again means you may pay twice.")) return;
    act(async () => { setState(await api("POST", `songs/${S.slug}/sections/${id}/mark-failed`)); S.est = {}; renderInspector(); });
  });
  $("#iSave").onclick = () => saveDrafts(id);
  $("#iDiscard") && ($("#iDiscard").onclick = () => { delete S.drafts[id]; S.inspKey = ""; renderInspector(); });
  $("#iPropose").onclick = () => propose(id);
  $("#iAsk").onclick = () => openChange([id]);
  $("#iAddRef").onclick = () => $("#iRefFile").click();
  $("#iRefFile").onchange = (e) => act(async () => {
    const f = e.target.files[0];
    if (!f) return;
    const label = prompt("Name this reference (the storyboard and image model will use this name):", f.name.replace(/\.[^.]+$/, ""));
    if (label === null) return;
    setState(await api("POST", `songs/${S.slug}/refs?section=${id}`, f, { "X-Filename": f.name, "X-Label": label }));
  });
  $("#iStatic").onchange = (e) => patchSection(id, { static: e.target.checked });
  $("#iLocked").onchange = (e) => patchSection(id, { locked: e.target.checked });
  $("#iSplit").onclick = () => openSplit(id);
  $("#iSplit2") && ($("#iSplit2").onclick = () => openSplit(id));
  $("#iDelete").onclick = () => $("#tbDelete").click();
  $$("[data-accept]", el).forEach((c) => (c.onchange = () => (S.proposal.accept[c.dataset.accept] = c.checked)));
  $("#pApply") && ($("#pApply").onclick = () => applyProposal(id, false));
  $("#pApplyRegen") && ($("#pApplyRegen").onclick = () => applyProposal(id, true));
  $("#pCancel") && ($("#pCancel").onclick = () => { S.proposal = null; renderInspector(); });
}

async function patchRetry(id) {
  await act(async () => setState(await api("POST", `songs/${S.slug}/sections/${id}/retry`)));
}

async function saveDrafts(id) {
  const d = S.drafts[id];
  if (!d) return;
  await act(async () => {
    setState(await api("PATCH", `songs/${S.slug}/sections/${id}`, d));
    delete S.drafts[id];
    S.inspKey = "";
    renderInspector();
    toast("Saved. Nothing was generated.");
  });
}

async function propose(id) {
  const d = S.drafts[id];
  if (d && Object.keys(d).length) await saveDrafts(id);
  S.proposal = { uid: id, loading: true };
  renderInspector();
  const r = await act(() => api("POST", `songs/${S.slug}/sections/${id}/propose`, {}));
  if (!r) { S.proposal = null; return renderInspector(); }
  const s = secById(id);
  S.proposal = { uid: id, ...r, accept: { beat: !s.edited.beat, image_prompt: !s.edited.image_prompt, video_prompt: !s.edited.video_prompt } };
  renderInspector();
}

async function applyProposal(id, regen) {
  const p = S.proposal, body = { hand: false };
  for (const f of ["beat", "image_prompt", "video_prompt"]) if (p.accept[f] && p[f]) body[f] = p[f];
  S.proposal = null;
  await patchSection(id, body);
  if (regen) generateStills([id]);
}

// ===================================================== ask for a change
// Type what you want ("the fence should be red"); the storyboard model merges it
// into the existing prompts with the smallest edit, and you accept the diff.
// Polish is the separate, opt-in rewrite. Nothing here generates a still.
async function openChange(uids) {
  for (const id of uids) if (S.drafts[id] && Object.keys(S.drafts[id]).length) await saveDrafts(id);
  const secs = uids.map(secById).filter(Boolean);
  const open = secs.filter((s) => !s.locked);
  const locked = secs.length - open.length;
  if (!open.length) return toast("Those sections are locked. Unlock them to change their prompts.", true);
  const dlg = $("#dlg");
  const scope = open.length === 1
    ? `${tc(open[0].start)} · ${esc(captionText(open[0].start, open[0].end) || "Instrumental")}`
    : `${open.length} sections, ${tc(open[0].start)} – ${tc(open[open.length - 1].end)}`;
  const C = { text: "", polish: false, result: null, accept: {} };

  const compose = () => {
    dlg.innerHTML = `
      <div class="dh"><h2>Ask for a change</h2><p class="muted small" style="margin:4px 0 0">${scope}${locked ? ` · ${locked} locked section${locked > 1 ? "s" : ""} skipped` : ""}</p></div>
      <div class="db">
        <textarea id="chText" rows="3" class="ask-box" placeholder="e.g. the fence should be red · make it night · the dragon wears the apron">${esc(C.text)}</textarea>
        <p class="muted small">Only what you ask for changes; the rest of the prompt is kept word for word. <b>Polish wording</b> rewrites the prompts more freely without changing what's in them, and also applies anything you type here.</p>
      </div>
      <div class="df"><span class="note">Nothing is generated. You'll see the changes before anything is saved.</span>
        <button class="btn quiet" id="chCancel">Cancel</button>
        <button class="btn" id="chPolish">Polish wording</button>
        <button class="btn primary" id="chGo">Propose change</button></div>`;
    const box = $("#chText");
    const go = (polish) => {
      C.text = box.value.trim();
      if (!polish && !C.text) { box.focus(); return toast("Type what you want changed, or use Polish wording.", true); }
      C.polish = polish;
      ask();
    };
    box.oninput = () => (C.text = box.value);
    box.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) go(false); };
    $("#chCancel").onclick = () => dlg.close();
    $("#chPolish").onclick = () => go(true);
    $("#chGo").onclick = () => go(false);
    setTimeout(() => box.focus(), 0);
  };

  const ask = async () => {
    dlg.innerHTML = `<div class="db ask-wait"><div class="clock" aria-hidden="true"><i class="hand-h"></i><i class="hand-m"></i></div>
      <p>${C.polish ? "Polishing" : "Working your change into"} ${open.length === 1 ? "the prompts" : `${open.length} sections`}…</p></div>`;
    const r = await act(() => api("POST", `songs/${S.slug}/change`, { uids: open.map((s) => s.uid), text: C.text, polish: C.polish }));
    if (!dlg.open) return;
    if (!r) return compose();
    C.result = r;
    C.accept = {};
    for (const row of r.rows) {
      const s = secById(row.uid);
      C.accept[row.uid] = { image_prompt: row.image_prompt !== s.image_prompt, video_prompt: row.video_prompt !== s.video_prompt };
    }
    review();
  };

  const review = () => {
    const r = C.result;
    const changed = r.rows.filter((row) => { const s = secById(row.uid); return row.image_prompt !== s.image_prompt || row.video_prompt !== s.video_prompt || row.fold_extra; });
    const same = r.rows.length - changed.length;
    dlg.innerHTML = `
      <div class="dh"><h2>${C.polish ? "Polished prompts" : "Proposed change"}</h2>
        <p class="muted small" style="margin:4px 0 0">${C.text ? `“${esc(C.text)}” · ` : ""}changed words are highlighted${same ? ` · ${same} section${same > 1 ? "s" : ""} needed no change` : ""}</p></div>
      <div class="db">
        ${r.warning ? `<div class="notice nt-check"><b>Heads up.</b> ${esc(r.warning)}</div>` : ""}
        ${changed.length ? changed.map((row) => {
          const s = secById(row.uid), a = C.accept[row.uid];
          const part = (f, label) => row[f] === s[f] ? "" : `<label class="check small"><input type="checkbox" data-ch="${row.uid}" data-f="${f}" ${a[f] ? "checked" : ""}> Use new ${label}${s.edited[f] ? " (replaces your hand edit)" : ""}</label><p>${diff(s[f], row[f])}</p>`;
          return `<div class="proposal">
            <div class="ch-head"><b>${tc(s.start)}</b> <span class="muted">${esc(captionText(s.start, s.end) || "Instrumental")}</span>${row.note ? `<span class="ch-note">${esc(row.note)}</span>` : ""}</div>
            ${part("image_prompt", "image prompt")}${s.static ? "" : part("video_prompt", "motion prompt")}
            ${row.fold_extra ? `<p class="muted small">Its extra instructions (“${esc(s.extra)}”) are worked into the prompt and cleared when you accept.</p>` : ""}
          </div>`;
        }).join("") : `<p class="muted">The model found nothing to change for that request.</p>`}
      </div>
      <div class="df"><span class="note">Cost ${money(r.cost || 0)}. Accepting saves the prompts; stills only regenerate if you choose.</span>
        <button class="btn quiet" id="chBack">Back</button>
        <button class="btn" id="chAccept">Accept</button>
        <button class="btn primary" id="chAcceptRegen">Accept and regenerate</button></div>`;
    $$("[data-ch]", dlg).forEach((c) => (c.onchange = () => { C.accept[c.dataset.ch][c.dataset.f] = c.checked; count(); }));
    $("#chBack").onclick = compose;
    $("#chAccept").onclick = () => accept(false);
    $("#chAcceptRegen").onclick = () => accept(true);
    const count = () => {
      const n = picked().length;
      $("#chAccept").disabled = $("#chAcceptRegen").disabled = !n;
      $("#chAcceptRegen").textContent = `Accept and regenerate ${n > 1 ? `${n} stills` : "still"}`;
    };
    count();
  };

  const picked = () => C.result.rows.filter((row) => {
    const a = C.accept[row.uid];
    return a && (a.image_prompt || a.video_prompt);
  });

  const accept = async (regen) => {
    const rows = picked();
    const note = C.polish ? (C.text ? `Polish: ${C.text}` : "Polish wording") : C.text;
    dlg.close();
    const ok = await act(async () => {
      let st;
      for (const row of rows) {
        const a = C.accept[row.uid], body = { ask: note };
        if (a.image_prompt) body.image_prompt = row.image_prompt;
        if (a.video_prompt) body.video_prompt = row.video_prompt;
        if (row.fold_extra && a.image_prompt) body.extra = "";
        st = await api("PATCH", `songs/${S.slug}/sections/${row.uid}`, body);
      }
      if (st) setState(st);
      S.est = {};
      S.inspKey = "";
      renderInspector();
      return true;
    });
    if (!ok) return;
    const ids = rows.map((row) => row.uid);
    if (regen) generateStills(ids);
    else toast(`Updated ${ids.length} section${ids.length > 1 ? "s" : ""}. Nothing was generated.`);
  };

  compose();
  dlg.showModal();
}

function confirmLines(idx) {
  act(async () => {
    let st;
    for (const i of idx) st = await api("PATCH", `songs/${S.slug}/lines/${i}`, { confirmed: true });
    if (st) setState(st);
    toast("Caption timing confirmed.");
  });
}

function renderBulk(el, sel) {
  const adjacent = (() => { const ids = plan().map((s) => s.uid); const k = sel.map((s) => ids.indexOf(s.uid)); return k.every((v, i) => !i || v === k[i - 1] + 1); })();
  el.innerHTML = `<div class="bulk">
    <h3>${sel.length} sections</h3>
    <p class="muted small">${tc(sel[0].start)} – ${tc(sel[sel.length - 1].end)}. Actions apply to this selection only; locked sections are skipped.</p>
    <div class="actions">
      <button class="btn primary" id="bAsk">Ask for a change…</button>
      <button class="btn" id="bRegen">Regenerate ${sel.length} stills</button>
      <button class="btn" id="bRegenWith">Regenerate with…</button>
      <button class="btn" id="bApprove">Approve selection</button>
      <button class="btn" id="bAnimate">Animate selection…</button>
      <button class="btn" id="bMerge" ${adjacent ? "" : "disabled"}>Merge into one section</button>
    </div>
    <div class="block">${sel.map((s) => `<div class="ref-row"><span class="dot ${s.status}"></span><span class="lbl">${tc(s.start)} ${esc(captionText(s.start, s.end) || "Instrumental")}</span></div>`).join("")}</div>
  </div>`;
  $("#bAsk").onclick = () => openChange(S.sel);
  $("#bRegen").onclick = () => generateStills(S.sel);
  $("#bRegenWith").onclick = () => openRegenWith(S.sel);
  $("#bApprove").onclick = () => approve(S.sel);
  $("#bAnimate").onclick = () => openAnimate(S.sel);
  $("#bMerge").onclick = () => $("#tbMerge").click();
}

// ========================================================= split overlay
function openSplit(id) {
  const s = secById(id);
  if (!s || s.locked) return;
  const limit = S.st.limit;
  const words = S.st.lines.flatMap((l) => l.words || []).filter((w) => w.start > s.start + 0.2 && w.start < s.end - 0.2);
  const suggest = () => {
    const n = Math.max(2, Math.ceil(len(s) / limit));
    const cuts = [];
    for (let k = 1; k < n; k++) {
      const target = s.start + (len(s) * k) / n;
      const near = words.length ? words.reduce((a, w) => (Math.abs(w.start - target) < Math.abs(a - target) ? w.start : a), words[0].start) : target;
      cuts.push(Math.abs(near - target) < len(s) / n / 2 ? near : target);
    }
    return [...new Set(cuts.map((c) => +c.toFixed(2)))].sort((a, b) => a - b);
  };
  const atPlayhead = S.t > s.start + 0.3 && S.t < s.end - 0.3 && !s.split_needed;
  let cuts = atPlayhead ? [+S.t.toFixed(2)] : suggest();
  const dlg = $("#dlg");
  dlg.innerHTML = `
    <div class="dh"><h2>Split into shots</h2><p class="muted small" style="margin:4px 0 0">${tc(s.start)} – ${tc(s.end)}. Click to add a cut, drag to move one. Parts keep this section's beat, prompts, references and still; each then gets its own versions and approval. Captions don't change.</p></div>
    <div class="db">
      <div class="parts" id="spParts"></div>
      <div class="split-view" id="spView"><canvas id="spWave"></canvas></div>
      <div class="actions"><button class="btn small" id="spSuggest">Suggest cuts</button>${S.t > s.start && S.t < s.end ? `<button class="btn small" id="spPlayhead">Cut at playhead</button>` : ""}<button class="btn small quiet" id="spClear">Remove all cuts</button></div>
    </div>
    <div class="df"><span class="note" id="spNote"></span><button class="btn quiet" id="spCancel">Cancel</button><button class="btn primary" id="spOk">Split</button></div>`;
  dlg.showModal();
  const view = $("#spView");
  const W = () => view.clientWidth;
  const toX = (t) => ((t - s.start) / len(s)) * W();
  const toT = (x) => clamp(s.start + (x / W()) * len(s), s.start + 0.25, s.end - 0.25);

  const draw = () => {
    const c = $("#spWave"), dpr = devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
    c.width = w * dpr; c.height = h * dpr;
    const g = c.getContext("2d"); g.scale(dpr, dpr); g.fillStyle = "#8ea8d2";
    const P = S.peaks;
    if (P) for (let x = 0; x < w; x += 2) {
      const a = Math.floor((s.start + (x / w) * len(s)) * P.per_second), b = Math.max(a + 1, Math.floor((s.start + ((x + 2) / w) * len(s)) * P.per_second));
      let v = 0; for (let i = a; i < b && i < P.peaks.length; i++) v = Math.max(v, P.peaks[i]);
      const bh = Math.max(1, v * (h - 40)); g.fillRect(x, (h - 24) / 2 - bh / 2, 1.4, bh);
    }
  };
  const render = () => {
    $$(".cut,.word,.wtick", view).forEach((n) => n.remove());
    for (const w of words) view.insertAdjacentHTML("beforeend", `<div class="wtick" style="left:${toX(w.start)}px"></div><div class="word" style="left:${toX(w.start)}px">${esc(w.w)}</div>`);
    cuts.forEach((c, k) => view.insertAdjacentHTML("beforeend", `<div class="cut" data-k="${k}" style="left:${toX(c)}px"><button data-rm="${k}" aria-label="Remove cut">×</button></div>`));
    const bounds = [s.start, ...cuts, s.end];
    const parts = bounds.slice(1).map((b, k) => b - bounds[k]);
    const over = parts.filter((p) => !s.static && p > limit + 0.05).length;
    $("#spParts").innerHTML = parts.map((p) => `<div class="${!s.static && p > limit + 0.05 ? "over" : ""}" style="width:${(p / len(s)) * 100}%">${p.toFixed(1)} s</div>`).join("");
    $("#spOk").disabled = !cuts.length || over > 0;
    $("#spOk").textContent = cuts.length ? `Split into ${parts.length} shots` : "Split";
    $("#spNote").textContent = over ? `${over} part${over > 1 ? "s are" : " is"} longer than the ${limit} s clip limit. Add or move cuts.` : "";
  };
  view.onmousedown = (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) { cuts.splice(+rm.dataset.rm, 1); return render(); }
    const cut = e.target.closest(".cut");
    const r = view.getBoundingClientRect();
    let k;
    if (cut) k = +cut.dataset.k;
    else { cuts.push(+toT(e.clientX - r.left).toFixed(2)); cuts.sort((a, b) => a - b); render(); return; }
    trackDrag((ev) => { cuts[k] = +toT(ev.clientX - r.left).toFixed(2); const el = view.querySelector(`.cut[data-k="${k}"]`); el.style.left = `${toX(cuts[k])}px`; }, () => { cuts.sort((a, b) => a - b); render(); });
  };
  $("#spSuggest").onclick = () => { cuts = suggest(); render(); };
  $("#spPlayhead") && ($("#spPlayhead").onclick = () => { cuts = [...new Set([...cuts, +S.t.toFixed(2)])].sort((a, b) => a - b); render(); });
  $("#spClear").onclick = () => { cuts = []; render(); };
  $("#spCancel").onclick = () => dlg.close();
  $("#spOk").onclick = () => act(async () => {
    setState(await api("POST", `songs/${S.slug}/split`, { uid: id, cuts }));
    dlg.close();
    S.sel = plan().filter((x) => x.start >= s.start - 0.01 && x.end <= s.end + 0.01).map((x) => x.uid);
    update();
    toast(`Split into ${cuts.length + 1} shots.`);
  });
  requestAnimationFrame(() => { draw(); render(); });
}

// ======================================================= animate dialog
async function animateNow(uids) {
  return act(async () => {
    const r = await api("POST", `songs/${S.slug}/animate`, { uids });
    S.est = {};
    setState(r);
    if (r.started) toast(`Sent ${r.started} clip${r.started > 1 ? "s" : ""} to ${S.st.meta.video_model}.`);
    if (r.skipped.length) toast(r.skipped.join(" "), true);
  });
}

async function openAnimate(uids) {
  const est = await act(() => api("GET", `songs/${S.slug}/estimate${uids?.length ? "?uids=" + uids.join(",") : ""}`));
  if (!est) return;
  const ready = est.rows.filter((r) => r.status === "ready");
  const blocked = est.rows.filter((r) => r.status !== "ready" && r.status !== "static");
  const statics = est.rows.filter((r) => r.status === "static").length;
  const chosen = new Set(ready.filter((r) => !r.has_current_clip).map((r) => r.uid));
  const dlg = $("#dlg");
  const why = { draft: "Not approved", "no still": "No still", "split needed": `Longer than ${est.limit} s, needs splitting`, rendering: "Already rendering", unknown: "Job unknown" };
  dlg.innerHTML = `
    <div class="dh"><h2>Cost check</h2><p class="muted small" style="margin:4px 0 0">${esc(est.model)} at ${esc(est.resolution)}. Only approved, non-static sections can animate. Clips render four at a time, and existing clips stay as versions.</p></div>
    <div class="db">
      ${ready.length ? `<table class="rows"><thead><tr><th></th><th>Section</th><th>Lyric</th><th>Clip</th><th class="num">Cost</th></tr></thead><tbody>
        ${ready.map((r) => { const s = secById(r.uid); return `<tr><td><input type="checkbox" data-pick="${r.uid}" ${chosen.has(r.uid) ? "checked" : ""} aria-label="Animate ${r.id}"></td>
          <td>${tc(s.start)}</td><td>${esc(captionText(s.start, s.end) || "Instrumental")}${r.has_current_clip ? ` <span class="muted small">(has a current clip; this re-rolls)</span>` : s.stale ? ` <span class="muted small" style="color:var(--stale)">(clip out of date)</span>` : ""}</td>
          <td>${r.seconds} s</td><td class="num">${money(r.cost)}</td></tr>`; }).join("")}</tbody></table>` : `<p class="muted">Nothing is ready to animate. Approve sections with stills first.</p>`}
      ${blocked.length ? `<h3 style="font-size:14px;margin:18px 0 6px">Not included</h3><table class="rows"><tbody>${blocked.map((r) => { const s = secById(r.uid); return `<tr><td>${tc(s.start)}</td><td>${esc(captionText(s.start, s.end) || "Instrumental")}</td><td>${why[r.status] || r.status}</td>
        <td>${r.status === "split needed" ? `<button class="btn small" data-split="${r.uid}">Split…</button>` : `<button class="btn small quiet" data-go="${r.uid}">Go to section</button>`}</td></tr>`; }).join("")}</tbody></table>` : ""}
      ${statics ? `<p class="muted small">${statics} static section${statics > 1 ? "s stay stills" : " stays a still"} and cost nothing.</p>` : ""}
    </div>
    <div class="df"><span class="note">Spent on this song so far: ${money(S.st.spent)}</span><span class="total" id="anTotal"></span>
      <button class="btn quiet" id="anCancel">Cancel</button><button class="btn primary" id="anOk"></button></div>`;
  const sum = () => {
    const rows = ready.filter((r) => chosen.has(r.uid));
    const total = rows.reduce((a, r) => a + (r.cost || 0), 0);
    $("#anTotal").textContent = money(total);
    $("#anOk").textContent = rows.length ? `Animate ${rows.length} clip${rows.length > 1 ? "s" : ""}` : "Animate";
    $("#anOk").disabled = !rows.length;
  };
  $$("[data-pick]", dlg).forEach((c) => (c.onchange = () => { c.checked ? chosen.add(c.dataset.pick) : chosen.delete(c.dataset.pick); sum(); }));
  $$("[data-go]", dlg).forEach((b) => (b.onclick = () => { dlg.close(); goTo(b.dataset.go); }));
  $$("[data-split]", dlg).forEach((b) => (b.onclick = () => { dlg.close(); goTo(b.dataset.split); openSplit(b.dataset.split); }));
  $("#anCancel").onclick = () => dlg.close();
  $("#anOk").onclick = () => { dlg.close(); animateNow([...chosen]); };
  sum();
  dlg.showModal();
}

function goTo(id) {
  if (S.tab !== "timeline") location.hash = `#/song/${S.slug}/timeline`;
  const s = secById(id);
  S.sel = [id];
  S.anchor = id;
  seek(s.start + 0.01);
  update();
  scrollIntoView(s.start, s.end);
}

// ======================================================== export dialog
async function openExport() {
  const r = await act(() => api("GET", `songs/${S.slug}/export-plan`));
  if (!r) return;
  const fall = r.rows.filter((x) => x.use !== "clip" && !(x.use === "still" && !x.reason));
  const statics = r.rows.filter((x) => x.use === "still" && !x.reason).length;
  const useStale = new Set();
  const dlg = $("#dlg");
  dlg.innerHTML = `
    <div class="dh"><h2>Export</h2><p class="muted small" style="margin:4px 0 0">Burns the captions in, trims every clip to its section, writes lyrics.srt${r.blender ? " and music_video.blend" : ""}. Sections without a usable clip show their still with a slow push-in, so check this list first.</p></div>
    <div class="db">
      ${fall.length ? `<table class="rows"><thead><tr><th>Section</th><th>Lyric</th><th>Shown as</th><th>Why</th><th></th></tr></thead><tbody>
      ${fall.map((x) => { const s = secById(x.uid); return `<tr><td>${tc(s.start)}</td><td>${esc(captionText(s.start, s.end) || "Instrumental")}</td>
        <td>${x.use === "black" ? "Black" : "Still"}</td><td>${esc(x.reason)}</td>
        <td><button class="btn small quiet" data-go="${x.uid}">Go to section</button>
        ${x.reason === "clip out of date" ? `<label class="check small"><input type="checkbox" data-stale="${x.uid}"> Use the old clip anyway</label>` : ""}</td></tr>`; }).join("")}</tbody></table>`
      : `<p>Every animated section has a current, approved clip.</p>`}
      ${statics ? `<p class="muted small">${statics} static section${statics > 1 ? "s" : ""} will show as stills, as intended.</p>` : ""}
      ${r.blender ? "" : `<p class="notice">No Blender set, so this export makes the MP4 and lyrics.srt but no music_video.blend. <a href="#/settings" id="exSettings">Set Blender in Settings</a> if you want it.</p>`}
    </div>
    <div class="df"><button class="btn quiet" id="exCancel">Cancel</button><button class="btn primary" id="exOk"></button></div>`;
  const label = () => {
    const n = fall.filter((x) => !useStale.has(x.uid)).length;
    $("#exOk").textContent = n ? `Export with ${n} section${n > 1 ? "s" : ""} as stills` : "Export";
  };
  $$("[data-stale]", dlg).forEach((c) => (c.onchange = () => { c.checked ? useStale.add(c.dataset.stale) : useStale.delete(c.dataset.stale); label(); }));
  $$("[data-go]", dlg).forEach((b) => (b.onclick = () => { dlg.close(); goTo(b.dataset.go); }));
  $("#exCancel").onclick = () => dlg.close();
  $("#exSettings") && ($("#exSettings").onclick = () => dlg.close());
  $("#exOk").onclick = () => act(async () => { const since = Date.now() / 1000 - 2; setState(await api("POST", `songs/${S.slug}/export`, { use_stale: [...useStale] })); dlg.close(); openBusy("export", since); });
  label();
  dlg.showModal();
}

function openResult() {
  const ex = S.st.export, dlg = $("#dlg");
  if (dlg.open) dlg.close();
  const v = Date.now();
  dlg.innerHTML = `
    <div class="dh"><h2>Your music video</h2></div>
    <div class="db result">
      ${ex["music_video.mp4"] ? `<video controls src="${fileUrl("export/music_video.mp4")}?v=${v}"></video>` : `<p>The video wasn't written. Check the error message.</p>`}
      <p class="small">Files are in <code>songs/${esc(S.slug)}/export/</code>:
        ${ex["music_video.mp4"] ? `<a href="${fileUrl("export/music_video.mp4")}" download>music_video.mp4</a>` : ""}
        ${ex["lyrics.srt"] ? ` · <a href="${fileUrl("export/lyrics.srt")}" download>lyrics.srt</a>` : ""}
        ${ex["music_video.blend"] ? ` · music_video.blend (opens in Blender's Video Editing workspace)` : ""}</p>
    </div>
    <div class="df"><button class="btn primary" id="rClose">Close</button></div>`;
  $("#rClose").onclick = () => dlg.close();
  dlg.showModal();
}

// ============================================================ keyboard
document.addEventListener("keydown", (e) => {
  if (S.view !== "song" || S.tab !== "timeline" || $("#dlg")?.open) return;
  if (e.target.closest("input, textarea, select")) return;
  // Alt+A: select every section. e.code, because Alt+A types "å" on a Mac.
  if (e.altKey && !e.ctrlKey && !e.metaKey && e.code === "KeyA") { e.preventDefault(); S.sel = plan().map((x) => x.uid); S.anchor = S.sel[0] ?? null; update(); }
  else if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  else if (e.key === "+" || e.key === "=") zoomAt(1.5);
  else if (e.key === "-") zoomAt(1 / 1.5);
  else if (e.key === "Escape") { S.sel = []; update(); }
});
window.addEventListener("resize", () => S.view === "song" && S.tab === "timeline" && drawWaves());

route();
