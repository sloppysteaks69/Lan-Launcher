// LAN Party Launcher UI: polls /api/state and redraws the active tab.
"use strict";
let lastNote = 0;
let S = null, tab = "library", drafts = {}, pre = null, net = null, def = null, loadedVersion = null, loadedPid = null;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gb = (n) => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(0) + " MB";
const sz = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e8 ? Math.round(n / 1e6) + " MB" : n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.round(n / 1e3) + " KB";
const spd = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB/s" : Math.round(n / 1e3) + " KB/s";
const eta = (s) => !isFinite(s) || s <= 0 ? "–" : s < 60 ? Math.ceil(s) + "s" : s < 3600 ? Math.floor(s / 60) + "m " + Math.round(s % 60) + "s"
  : Math.floor(s / 3600) + "h " + Math.round((s % 3600) / 60) + "m";

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-LanParty": "1" }, body: JSON.stringify(body) };
  const r = await fetch("/api/" + path, opt);
  return r.json();
}
// Only touch the DOM when the markup changed, so a poll refresh never swallows a click mid-press.
function put(sel, h) { const el = $(sel); if (el._h !== h) { el.innerHTML = h; el._h = h; } }
// The toast for a button that needs admin: the launcher started with run_admin.bat gets no prompt at all.
function adminMsg(doing) {
  return (S && S.vpn && S.vpn.admin) ? doing + " (already running as admin, so no prompt)"
    : doing + " Accept the admin prompt; if it doesn't show, click the flashing shield on the taskbar.";
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 4000); }
async function act(path, body, okMsg) {
  const r = await api(path, body);
  if (r.ok === false) toast(r.error || "Failed"); else if (r.warning) toast(r.warning); else if (okMsg) toast(okMsg);
  await refresh();
  return r;
}

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === name));
  tab = name; pre = null; net = null; render(true);
}
document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));

// A new poll (from the Party Master) switches this screen to the Vote tab, once per poll. The poll ids already
// seen are kept per browser (localStorage, wrapped: it can be missing); the first load only records what's there.
let seenPolls = null;
function saveSeenPolls() { try { localStorage.setItem("lp_seenpolls", JSON.stringify([...seenPolls].slice(-300))); } catch (e) {} }
function checkNewPolls() {
  const ids = S.polls.map((p) => p.id);
  if (seenPolls === null) {
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem("lp_seenpolls") || "null"); } catch (e) {}
    if (!Array.isArray(stored)) { seenPolls = new Set(ids); saveSeenPolls(); return; }
    seenPolls = new Set(stored);
  }
  const fresh = S.polls.filter((p) => !seenPolls.has(p.id));
  if (!fresh.length || heartBusy) return;     // mid-drag on the hearts: switch on the next refresh
  fresh.forEach((p) => seenPolls.add(p.id));
  saveSeenPolls();
  const open = fresh.filter((p) => !p.closed);
  if (!open.length) return;
  closeGear();
  if (tab !== "vote") showTab("vote");
  if (open[0].created_by !== S.me.id) toast(`🗳️ New poll from ${open[0].creator_name}: ${open[0].title}`);
}

async function refresh() {
  try { S = await api("state"); } catch (e) { $("#who").textContent = "launcher not running (restarting?)"; return; }
  // after an update the launcher restarts: reload so the page matches the new version
  if (loadedPid === null) { loadedVersion = S.version; loadedPid = S.pid; }
  else if (S.pid !== loadedPid && S.version !== loadedVersion) { location.reload(); return; }
  // results of background jobs (firewall rules added, update finished, …) pop up once
  const fresh = (S.notes || []).filter((n) => n.t > lastNote);
  if (lastNote && fresh.length) toast(fresh[fresh.length - 1].msg);
  if (S.notes && S.notes.length) lastNote = Math.max(lastNote || 0, ...S.notes.map((n) => n.t)); else if (!lastNote) lastNote = 1;
  checkNewPolls();
  render(false);
}
setInterval(refresh, 1500);
refresh();

function render(force) {
  if (!S) return;
  const vpnBadge = S.vpn && S.vpn.state !== "off" ? ` · 🌐 VPN ${esc(S.vpn.state)}` : "";
  const pms = S.peers.filter((p) => p.party_master).map((p) => esc(p.name));
  const pmBadge = pms.length ? ` · <span title="Party Master: starts the polls">👑 ${pms.join(", ")}</span>` : "";
  const th = S.throttle || {};
  const thBadge = th.limited ? ` · <span title="Game copies are held to this speed while a game runs">⏸ copies ${spd(th.limit_bps)} (playing ${esc(th.title)})</span>` : "";
  put("#who", `${S.me.party_master ? `<span class="crown" title="You are the Party Master">👑</span> ` : ""}<b>${esc(S.me.name)}</b> · v${esc(S.version)} · ${esc(S.me.ips.join(", ") || "no LAN IP")} · ${S.peers.length} player(s) online${pmBadge}${vpnBadge}${thBadge}`);
  renderParty();
  renderUpdate();
  if (tab === "settings" && !force && document.activeElement && document.activeElement.tagName === "INPUT") return;
  ({ library: vLibrary, vote: vVote, prereqs: vPrereqs, network: vNetwork, internet: vInternet, settings: vSettings })[tab]();
}

function renderUpdate() {
  const u = S.update || {}, st = u.status || {}, av = u.available || {};
  let h = "";
  if (st.state === "downloading" || st.state === "applying" || st.state === "restarting") h = `<div>⬆️ ${esc(st.msg)}</div>`;
  else if (st.state === "restart_needed") h = `<div>✅ ${esc(st.msg)}</div><button class="b" onclick="if(confirm('Restart the launcher now? This leaves the internet party.'))act('app/restart',{},'Restarting…')">Restart now</button>`;
  else if (av.version) h = `<div>⬆️ Launcher <b>v${esc(av.version)}</b> is available from <b>${esc(av.name)}</b> <span class="dim">(you have v${esc(S.version)})</span></div>
     <button class="b" onclick="act('app/update',{peer_id:'${esc(av.peer_id)}'},'Updating…')">Update</button>`
     + (st.state === "error" ? `<span class="dim">${esc(st.msg)}</span>` : "");
  else if (st.state === "error") h = `<div>${esc(st.msg)}</div>`;
  put("#update", h);
}

function renderParty() {
  const p = S.party, el = $("#party");
  if (!p) { put("#party", ""); return; }
  const g = S.library.find((x) => x.id === p.game_id);
  const mine = p.me;
  let btn;
  if (mine) btn = `<button class="b alt" onclick="act('party/clear',{})">End party</button>`;
  else if (g && g.have) btn = `<button class="b" onclick="act('launch',{id:'${esc(p.game_id)}',role:'join',host_ip:'${esc(p.host_ip)}'},'Joining…')">Join game</button>
     <button class="b alt" onclick="act('party/clear',{})">Dismiss</button>`;
  else btn = `<button class="b purple" onclick="act('download',{id:'${esc(p.game_id)}'},'Download started')">Download first</button>
     <button class="b alt" onclick="act('party/clear',{})">Dismiss</button>`;
  put("#party", `<div>🎮 <b>${esc(p.host_name)}</b> is hosting <b>${esc(p.title)}</b> <span class="dim">(${esc(p.host_ip)})</span></div>${btn}`);
}

// ---------- Hearts: your interest in a game, 0-10 (half-heart steps, up to 5 hearts) ----------
// Pixel-art heart: X = outline (Launch blue), inside = fill (Host purple). Drawn as SVG rects, so it stays crisp.
const HEART = [".XX...XX.", "X..X.X..X", "X...X...X", "X.......X", ".X.....X.", "..X...X..", "...X.X...", "....X...."];
const HEART_PX = 2, HEART_W = 9 * HEART_PX, HEART_STEP = HEART_W + 2;    // heart width + gap, in CSS px
const HEART_IN = (() => {      // inside cells = not reachable from the border through empty cells
  const H = HEART.length, W = HEART[0].length, out = new Set(), q = [];
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++)
    if ((y === 0 || x === 0 || y === H - 1 || x === W - 1) && HEART[y][x] === ".") { out.add(y * W + x); q.push([x, y]); }
  while (q.length) {
    const [x, y] = q.pop();
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = x + dx, ny = y + dy, k = ny * W + nx;
      if (nx >= 0 && ny >= 0 && nx < W && ny < H && HEART[ny][nx] === "." && !out.has(k)) { out.add(k); q.push([nx, ny]); }
    }
  }
  const inside = [];
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (HEART[y][x] === "." && !out.has(y * W + x)) inside.push([x, y]);
  return inside;
})();
function heartSVG(fill) {     // fill: 0 empty, 1 left half, 2 full
  let r = "";
  HEART.forEach((row, y) => [...row].forEach((c, x) => { if (c === "X") r += `<rect x="${x}" y="${y}" width="1" height="1" class="ho"/>`; }));
  if (fill) for (const [x, y] of HEART_IN) if (fill === 2 || x <= 4) r += `<rect x="${x}" y="${y}" width="1" height="1" class="hf"/>`;
  return `<svg width="${HEART_W}" height="${HEART.length * HEART_PX}" viewBox="0 0 ${HEART[0].length} ${HEART.length}" shape-rendering="crispEdges">${r}</svg>`;
}
function heartsInner(score, dragging) {
  // resting: the hearts you've filled (one empty heart at 0); while dragging: one more, so there's room to grow
  const n = Math.min(5, dragging ? Math.floor(score / 2) + 1 : Math.max(1, Math.ceil(score / 2)));
  let h = "";
  for (let i = 0; i < n; i++) h += heartSVG(Math.max(0, Math.min(2, score - 2 * i)));
  return h;
}
function heartsHTML(g) {
  const l = g.like || {}, s = l.mine || 0;
  const votes = `${l.n} vote${l.n === 1 ? "" : "s"}`;
  // the Party Master sees the party's score out of 10 (players who gave it no hearts don't count)
  const avg = S.me.party_master
    ? `<span class="likeavg pm" title="${esc(l.n ? "Party score from " + (l.voters || []).join(", ") : "Nobody has given this game hearts yet")}">👑 ${l.n ? `<b>${(+l.avg).toFixed(1)} / 10</b> · ${votes}` : "no score yet"}</span>`
    : l.n ? `<span class="likeavg" title="${esc((l.voters || []).join(", "))}">avg ${l.avg} · ${votes}</span>` : "";
  return `<div class="hearts" role="slider" tabindex="0" aria-label="How much you want to play ${esc(g.title)} (0-10)"
      aria-valuemin="0" aria-valuemax="10" aria-valuenow="${s}" title="Drag right to fill hearts: how much you want to play this"
      data-id="${esc(g.id)}" data-score="${s}" onpointerdown="heartDown(event,this)" onkeydown="heartKey(event,this)">${heartsInner(s, false)}</div>${avg}`;
}
let heartBusy = false, heartTimer = null;           // while dragging, refreshes don't redraw the grid
function heartScoreAt(el, clientX) {
  // measured from where the hearts started when the drag began, so a reflow mid-drag can't make the score jump
  const left = el._dragLeft !== undefined ? el._dragLeft : el.getBoundingClientRect().left + 2;
  const x = clientX - left;
  return x <= 1 ? 0 : Math.max(0, Math.min(10, Math.ceil(x / (HEART_STEP / 2))));
}
function heartShow(el, s, dragging) { el.dataset.score = s; el.setAttribute("aria-valuenow", s); el.innerHTML = heartsInner(s, dragging); }
async function heartSave(el) {
  heartBusy = false;
  await api("like", { id: el.dataset.id, score: +el.dataset.score });
  await refresh();
}
function heartDown(e, el) {
  if (e.button !== undefined && e.button !== 0) return;
  e.preventDefault();
  heartBusy = true;
  el.focus();
  el._dragLeft = el.getBoundingClientRect().left + 2;      // + the hearts' padding
  heartShow(el, heartScoreAt(el, e.clientX), true);
  // Listen on the window, so the drag keeps working past the last heart (capture is a bonus, not required)
  try { el.setPointerCapture(e.pointerId); } catch (err) { /* no active pointer: window listeners still work */ }
  const move = (ev) => heartShow(el, heartScoreAt(el, ev.clientX), true);
  const up = () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    el._dragLeft = undefined;
    heartShow(el, +el.dataset.score, false);
    heartSave(el);
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); window.addEventListener("pointercancel", up);
}
function heartKey(e, el) {
  const s = +el.dataset.score;
  const n = { ArrowRight: s + 1, ArrowUp: s + 1, ArrowLeft: s - 1, ArrowDown: s - 1, Home: 0, End: 10 }[e.key];
  if (n === undefined) return;
  e.preventDefault();
  heartBusy = true;
  heartShow(el, Math.max(0, Math.min(10, n)), false);
  clearTimeout(heartTimer);
  heartTimer = setTimeout(() => heartSave(el), 500);
}

// ---------- Library ----------
// Filters (genre, players, release sort, year range, search) are kept per browser and only change what's shown.
const LF_DEFAULT = { genre: "", players: "", sort: "title", years: "", q: "" };
let LF = (() => { try { return Object.assign({}, LF_DEFAULT, JSON.parse(localStorage.getItem("lp_libfilter") || "{}")); }
                  catch (e) { return Object.assign({}, LF_DEFAULT); } })();
const PLAYER_RANGES = { "1": [1, 1], "2-4": [2, 4], "5-8": [5, 8], "9+": [9, 9999] };
function saveLF() { try { localStorage.setItem("lp_libfilter", JSON.stringify(LF)); } catch (e) {} }
function setLF(k, v) {
  LF[k] = v; saveLF();
  if (k !== "q") renderLibBar(true);          // the search box keeps its focus while typing
  renderLibGrid();
}
function resetLF() { LF = Object.assign({}, LF_DEFAULT); saveLF(); renderLibBar(true); renderLibGrid(); }
const gPlayers = (g) => { const p = g.players && g.players.max ? g.players : (g.lan || {}).players; return p && p.max ? p : null; };
const decadeOf = (y) => y ? Math.floor(y / 10) * 10 : 0;
function libFiltered() {
  const q = (LF.q || "").trim().toLowerCase();
  let list = S.library.filter((g) => {
    if (LF.genre && (LF.genre === "?" ? g.genre : g.genre !== LF.genre)) return false;
    if (LF.players) {
      const p = gPlayers(g), r = PLAYER_RANGES[LF.players];
      if (!p || !r || p.max < r[0] || p.min > r[1]) return false;
    }
    if (LF.years && (LF.years === "?" ? g.year : decadeOf(g.year) !== +LF.years)) return false;
    if (q && !(g.title || "").toLowerCase().includes(q)) return false;
    return true;
  });
  const byTitle = (a, b) => (a.title || "").localeCompare(b.title || "", undefined, { sensitivity: "base" });
  if (LF.sort === "liked") {
    const L = (g) => g.like || {};
    list = list.slice().sort((a, b) => (L(b).avg || 0) - (L(a).avg || 0) || (L(b).n || 0) - (L(a).n || 0) || byTitle(a, b));
  } else if (LF.sort === "new" || LF.sort === "old") {
    const dir = LF.sort === "new" ? -1 : 1;
    list = list.slice().sort((a, b) => (!a.year - !b.year) || dir * ((a.year || 0) - (b.year || 0)) || byTitle(a, b));
  }
  return list;
}
function renderLibBar(force) {
  const bar = document.getElementById("libbar");
  if (!bar) return;
  const search = document.getElementById("libq");
  if (!force && search && document.activeElement === search) return;
  const genres = [...new Set(S.library.map((g) => g.genre).filter(Boolean))].sort();
  const decades = [...new Set(S.library.map((g) => decadeOf(g.year)).filter(Boolean))].sort();
  const anyNoGenre = S.library.some((g) => !g.genre), anyNoYear = S.library.some((g) => !g.year);
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(label)}</option>`;
  const chip = (v, label) => `<button class="chip ${LF.players === v ? "on" : ""}" onclick="setLF('players', LF.players === '${v}' ? '' : '${v}')">${label}</button>`;
  const h = `<div class="filters">
      <div class="fgroup"><div class="flabel">Genre</div>
        <select onchange="setLF('genre', this.value)">${opt("", "All genres", LF.genre)}${genres.map((x) => opt(x, x, LF.genre)).join("")}${anyNoGenre ? opt("?", "Unknown", LF.genre) : ""}</select></div>
      <div class="fgroup"><div class="flabel">Players</div><div class="row">
        <select onchange="setLF('players', this.value)">${opt("", "Any players", LF.players)}${opt("1", "1 player", LF.players)}${opt("2-4", "2–4 players", LF.players)}${opt("5-8", "5–8 players", LF.players)}${opt("9+", "9+ players", LF.players)}</select>
        ${chip("1", "1 Player")}${chip("2-4", "2–4 Players")}${chip("5-8", "5–8 Players")}${chip("9+", "9+ Players")}</div></div>
      <div class="fgroup"><div class="flabel">Sort by release</div><div class="row">
        <select onchange="setLF('sort', this.value)">${opt("title", "Title A–Z", LF.sort)}${opt("new", "Newest first", LF.sort)}${opt("old", "Oldest first", LF.sort)}${opt("liked", "Most liked", LF.sort)}</select>
        <select onchange="setLF('years', this.value)">${opt("", "Any year", LF.years)}${decades.map((d) => opt(d, d + "s", LF.years)).join("")}${anyNoYear ? opt("?", "Unknown year", LF.years) : ""}</select></div></div>
      <div class="fgroup grow"><div class="flabel">&nbsp;</div>
        <input type="search" id="libq" placeholder="Search games…" value="${esc(LF.q)}" oninput="setLF('q', this.value)"></div>
    </div>`;
  if (bar._h !== h) { bar.innerHTML = h; bar._h = h; }
}
// Library sub-tabs: Games (the default) and the shared media folders (media.py). Kept per browser.
const LIB_CATS = [["games", "🎮", "Games"], ["movies", "🎬", "Movies"], ["tv", "📺", "TV"], ["music", "🎵", "Music"],
                  ["books", "📚", "Books"], ["misc", "📦", "Misc"]];
let libCat = (() => { try { const c = localStorage.getItem("lp_libcat"); return LIB_CATS.some((x) => x[0] === c) ? c : "games"; } catch (e) { return "games"; } })();
function setLibCat(c) {
  libCat = c;
  try { localStorage.setItem("lp_libcat", c); } catch (e) {}
  const v = $("#view"); v.innerHTML = ""; v._h = null;     // rebuild the boxes for the new category
  vLibrary();
}
function renderLibCats() {
  const media = S.media || [];
  const h = `<div class="subtabs" role="tablist">` + LIB_CATS.map(([c, icon, label]) => {
    const n = c === "games" ? S.library.length : media.filter((m) => m.cat === c).length;
    return `<button role="tab" aria-selected="${libCat === c}" class="${libCat === c ? "on" : ""}" onclick="setLibCat('${c}')">${icon} ${label}${n ? ` <span class="cnt">${n}</span>` : ""}</button>`;
  }).join("") + `</div>`;
  put("#libcats", h);
}
function vLibrary() {
  // the bar and the grid live in their own boxes, so a refresh never redraws the search box under the cursor
  if (!document.getElementById("libbar")) {
    const v = $("#view");
    v.innerHTML = `<div id="libcats"></div><div id="libhead"></div><div id="libbar"></div><div id="libgrid"></div>`;
    v._h = null;
  }
  renderLibCats();
  if (libCat !== "games") { renderMediaBar(false); renderMediaGrid(); return; }
  renderLibBar(false);
  renderLibGrid();
}

// ---------- Library > Movies / TV / Music / Books / Misc ----------
// Each category is one folder on this PC (chosen here or in Settings). Everything at the top of it, a file or a
// sub-folder, is one item that other players can copy, through the same copier as the games.
const MF_DEFAULT = { q: "", sort: "title", show: "" };
let MF = (() => { try { return Object.assign({}, MF_DEFAULT, JSON.parse(localStorage.getItem("lp_mediafilter") || "{}")); }
                  catch (e) { return Object.assign({}, MF_DEFAULT); } })();
function setMF(k, v) {
  MF[k] = v;
  try { localStorage.setItem("lp_mediafilter", JSON.stringify(MF)); } catch (e) {}
  if (k !== "q") renderMediaBar(true);
  renderMediaGrid();
}
const catLabel = (c) => (LIB_CATS.find((x) => x[0] === c) || ["", "", c])[2];
async function pickMediaFolder(cat) {
  toast("Choose the folder in the window that opens (it may be behind the browser)");
  return act("media/folder", { cat, pick: true }, `${catLabel(cat)} folder set`);
}
async function getMedia(id, cat) {
  // no folder for this category yet: ask for one first, then start the copy
  if (!(S.settings.media_folders || {})[cat]) {
    const r = await pickMediaFolder(cat);
    if (!r || r.ok === false) return;
  }
  act("download", { id }, "Download started");
}
function renderMediaBar(force) {
  const bar = document.getElementById("libbar");
  if (!bar) return;
  const search = document.getElementById("mediaq");
  if (!force && search && document.activeElement === search) return;
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(label)}</option>`;
  const h = `<div class="filters">
      <div class="fgroup"><div class="flabel">Show</div>
        <select onchange="setMF('show', this.value)">${opt("", "Everything in the party", MF.show)}${opt("mine", "Only mine", MF.show)}${opt("missing", "Only what I don't have", MF.show)}</select></div>
      <div class="fgroup"><div class="flabel">Sort</div>
        <select onchange="setMF('sort', this.value)">${opt("title", "Title A–Z", MF.sort)}${opt("new", "Newest added", MF.sort)}${opt("big", "Biggest first", MF.sort)}${opt("owners", "Most shared", MF.sort)}</select></div>
      <div class="fgroup grow"><div class="flabel">&nbsp;</div>
        <input type="search" id="mediaq" placeholder="Search ${esc(catLabel(libCat))}…" value="${esc(MF.q)}" oninput="setMF('q', this.value)"></div>
    </div>`;
  if (bar._h !== h) { bar.innerHTML = h; bar._h = h; }
}
function renderMediaGrid() {
  if (!document.getElementById("libgrid")) return;
  const cat = libCat, label = catLabel(cat);
  const folder = (S.settings.media_folders || {})[cat] || "";
  const all = (S.media || []).filter((m) => m.cat === cat);
  const dl = Object.fromEntries(S.downloads.map((d) => [d.id, d]));
  const ul = Object.fromEntries((S.uploads || []).map((u) => [u.id, u]));
  const mine = all.filter((m) => m.have);
  put("#libhead", `<div class="row"><h2 style="margin:0">${esc(label)} (${mine.length} yours, ${all.length} in the party)</h2>
    ${folder ? `<button class="mini" onclick="act('rescan',{},'Rescanning')">${S.media_scanning ? "Scanning…" : "Rescan"}</button>` : ""}</div>
    ${folder ? `<div class="row mfolder"><span class="dim">Folder:</span> <code>${esc(folder)}</code>
      <button class="mini" onclick="pickMediaFolder('${cat}')">Change…</button></div>` : ""}`);
  const q = (MF.q || "").trim().toLowerCase();
  let shown = all.filter((m) => (!q || (m.title || "").toLowerCase().includes(q) || (m.name || "").toLowerCase().includes(q))
    && (MF.show !== "mine" || m.have) && (MF.show !== "missing" || !m.have));
  const byTitle = (a, b) => (a.title || "").localeCompare(b.title || "", undefined, { sensitivity: "base", numeric: true });
  if (MF.sort === "new") shown = shown.slice().sort((a, b) => (b.added || 0) - (a.added || 0) || byTitle(a, b));
  else if (MF.sort === "big") shown = shown.slice().sort((a, b) => (b.size || 0) - (a.size || 0) || byTitle(a, b));
  else if (MF.sort === "owners") shown = shown.slice().sort((a, b) => b.owners.length - a.owners.length || byTitle(a, b));
  else shown = shown.slice().sort(byTitle);
  let h = "";
  if (!folder) h += `<div class="card mpick"><h3>Choose a folder for your ${esc(label)}</h3>
      <p class="dim">Pick the folder on this PC where your ${esc(label.toLowerCase())} live. Everything in it is shared with the party
      (each file or sub-folder is one item), and what you copy from other players goes there too. You can change it any time here or in Settings.</p>
      <button class="b big" onclick="pickMediaFolder('${cat}')">📁 Choose folder…</button></div><br>`;
  if (!all.length && folder) h += `<div class="card dim">Nothing here yet. Put files or folders in <code>${esc(folder)}</code> and click Rescan,
      or wait for other players to share theirs.</div>`;
  else if (all.length && !shown.length) h += `<div class="card dim">Nothing matches.</div>`;
  h += `<div class="grid">`;
  for (const m of shown) {
    const d = dl[m.id];
    const busy = d && (d.state === "downloading" || d.state === "starting");
    if (busy || ul[m.id]) {
      h += `<div class="card gcard xfer ${busy ? "down" : "up"}"><h3>${esc(m.title)}</h3>${xferHTML(busy ? d : null, ul[m.id])}</div>`;
      continue;
    }
    const others = (m.owner_names || []).filter((n) => !n.endsWith("(you)"));
    let actions;
    if (m.have) actions = `<button class="b alt" onclick="act('media/show',{id:'${esc(m.id)}'})" title="Open the folder in Explorer">📂 Show in folder</button>`;
    else actions = `<button class="b alt" onclick="getMedia('${esc(m.id)}','${cat}')" ${others.length ? "" : "disabled"}>Get from ${esc(others[0] || "nobody online")}${others.length > 1 ? ` +${others.length - 1}` : ""}</button>`;
    const tags = (m.types || []).map((t) => `<span class="tag">${esc(t.replace(/^\./, "").toUpperCase())}</span>`);
    if (!m.single) tags.unshift(`<span class="tag">folder</span>`);
    const metaBits = [sz(m.size || 0), m.single ? "" : `${m.files || 0} files`, `on ${esc((m.owner_names || []).join(", "))}`].filter(Boolean);
    let prog = "";
    if (d && d.state === "error") prog = `<div class="meta"><span class="no">${esc(d.error)}</span></div>`;
    h += `<div class="card gcard ${m.have ? "" : "missing"}"><h3 title="${esc(m.name)}">${esc(m.title)}</h3>
      <div class="meta">${metaBits.join(" · ")}</div>
      ${tags.length ? `<div class="tags">${tags.join("")}</div>` : ""}
      <div class="row acts">${actions}</div>${prog}</div>`;
  }
  put("#libgrid", h + `</div>`);
}
// The transfer card: download progress, speed, time left, LZ4 saving, hash checks, every source and every
// player copying from us. d = this PC's download job (or null), u = our uploads of this game (or undefined).
function xferHTML(d, u) {
  let h = "";
  const th = S.throttle || {};
  if (d) {
    const pct = d.total ? Math.min(100, 100 * d.done / d.total) : 0;
    const left = d.rate > 0 ? (d.total - d.done) / d.rate : Infinity;
    const saved = d.wire > 0 && d.done > 0 ? Math.round(100 * (1 - d.wire / Math.max(1, d.done - (d.resumed || 0)))) : 0;
    h += `<div class="xhead"><span class="xdir">⬇ ${d.state === "starting" ? "Starting…" : "Downloading"}</span>
      <span class="xpct">${pct.toFixed(pct < 10 ? 1 : 0)}%</span></div>
      <div class="bar xbar"><div style="width:${pct}%"></div></div>
      <div class="xstats">
        <div><b>${spd(d.rate)}</b><span>speed</span></div>
        <div><b>${sz(d.done)}</b><span>of ${sz(d.total)}</span></div>
        <div><b>${d.state === "starting" ? "–" : eta(left)}</b><span>left</span></div>
        <div><b>${d.files_done}/${d.files}</b><span>files</span></div>
      </div>`;
    const notes = [];
    if (d.wire_rate > 0 && d.wire_rate < d.rate * 0.97) notes.push(`on the wire ${spd(d.wire_rate)}`);
    if (saved >= 3) notes.push(`<span class="xgood">compression saved ${saved}%</span>`);
    if (d.files) notes.push(d.checked >= d.files ? `<span class="xgood">✓ every file checked by SHA-256</span>`
                                                 : `${d.checked} of ${d.files} files checked by SHA-256`);
    if (notes.length) h += `<div class="xnote">${notes.join(" · ")}</div>`;
    const srcs = (d.source_stats || []).slice().sort((a, b) => b.rate - a.rate || b.bytes - a.bytes);
    if (srcs.length) {
      const top = Math.max(1, ...srcs.map((x) => x.rate));
      h += `<div class="xsub">From</div><div class="xrows">` + srcs.map((x) => `<div class="xrow ${x.state !== "ok" ? "off" : ""}">
          <span class="xname">${esc(x.name)}</span>
          <span class="xtag ${x.kind === "swarm" ? "sw" : ""}" title="${x.kind === "swarm" ? "Still downloading it too: shares the parts it has" : "Has the whole game"}">${x.kind === "swarm" ? "swarm" : "full"}</span>
          <span class="xmini"><i style="width:${Math.round(100 * x.rate / top)}%"></i></span>
          <span class="xnum">${x.state !== "ok" ? "dropped" : spd(x.rate)}</span><span class="xnum dim">${sz(x.bytes)}</span></div>`).join("") + `</div>`;
    }
  }
  if (u) {
    h += `<div class="xsub">${d ? "Also sharing with" : `<span class="xdir">⬆ Sharing</span> · ${spd(u.rate)} · ${sz(u.bytes)} sent`}</div>
      <div class="xrows">` + u.peers.slice().sort((a, b) => b.rate - a.rate).map((p) => `<div class="xrow">
        <span class="xname">${esc(p.name)}</span><span class="xtag up" title="${p.level ? `zstd level ${p.level}, adjusted to the link` : ""}">${p.level ? `zstd ${p.level}` : "copying"}</span>
        <span class="xmini"><i style="width:${Math.round(100 * p.rate / Math.max(1, ...u.peers.map((q) => q.rate)))}%"></i></span>
        <span class="xnum">${spd(p.rate)}</span><span class="xnum dim">${sz(p.bytes)}</span></div>`).join("") + `</div>`;
    if (u.wire > 0 && u.wire < u.bytes * 0.97) h += `<div class="xnote"><span class="xgood">compression saved ${Math.round(100 * (1 - u.wire / u.bytes))}%</span> of what we sent</div>`;
  }
  if (th.limited) h += `<div class="xnote xwarn">⏸ Held to ${spd(th.limit_bps)} each way while you play ${esc(th.title)} (Settings)</div>`;
  return h;
}
function renderLibGrid() {
  if (!document.getElementById("libgrid")) return;
  const dl = Object.fromEntries(S.downloads.map((d) => [d.id, d]));
  const ul = Object.fromEntries((S.uploads || []).map((u) => [u.id, u]));
  const have = S.library.filter((g) => g.have).length;
  const shown = libFiltered();
  const filtering = JSON.stringify(LF) !== JSON.stringify(LF_DEFAULT);
  put("#libhead", `<div class="row"><h2 style="margin:0">Games (${have} yours, ${S.library.length} in the party)</h2>
    <button class="mini" onclick="act('rescan',{},'Rescanning')">${S.scanning ? "Scanning…" : "Rescan"}</button></div>`);
  let h = `<div class="row between fsum"><span class="dim">${filtering ? `Showing ${shown.length} of ${S.library.length} games` : ""}</span>
    ${filtering ? `<button class="link" onclick="resetLF()">↺ Reset filters</button>` : ""}</div>`;
  if (!S.settings.folders.length) h += `<div class="card">Add your games folder in <b>Settings</b> to get started.</div><br>`;
  if (filtering && !shown.length) h += `<div class="card dim">No games match these filters.</div>`;
  h += `<div class="grid">`;
  for (const g of shown) {
    const d = dl[g.id];
    const others = g.owner_names.filter((n) => !n.endsWith("(you)"));
    let actions;
    if (g.have && g.setup_needed) actions = `<button class="b" onclick="act('setup',{id:'${esc(g.id)}'},'Setup started – the game unlocks when it finishes')">Run one-time setup</button>
       <span class="meta">${esc(g.lan.setup)}</span>`;
    else if (g.have) actions = `<button class="b big" onclick="act('launch',{id:'${esc(g.id)}',role:'solo'},'Launching')" title="Play on this PC">Launch</button>
       <button class="b big purple" onclick="hostGame('${esc(g.id)}')" title="Host it and invite everyone to join">Host</button>`;
    else if (d && (d.state === "downloading" || d.state === "starting")) actions = `<span class="dim">Downloading…</span>`;
    else actions = `<button class="b alt" onclick="act('download',{id:'${esc(g.id)}'},'Download started')" ${others.length ? "" : "disabled"}>Get from ${esc(others[0] || "nobody online")}${others.length > 1 ? ` +${others.length - 1}` : ""}</button>`;
    let prog = "";
    if (d && d.state !== "done") {
      const pct = d.total ? Math.floor(100 * d.done / d.total) : 0;
      prog = `<div class="meta">${d.state === "error" ? `<span class="no">${esc(d.error)}</span>` :
        `${pct}% · ${gb(d.done)} of ${gb(d.total)} · ${(d.rate / 1e6).toFixed(1)} MB/s · ${d.files_done}/${d.files} files from ${
          (d.source_stats && d.source_stats.length)
            ? d.source_stats.map(s => `${esc(s.name)} ${gb(s.bytes)}${s.state !== "ok" ? " (dropped)" : ""}`).join(", ")
            : esc(d.sources.join(", "))}`}</div>
        <div class="bar"><div style="width:${pct}%"></div></div>`;
    }
    // while the game is copying to or from this PC, the whole card becomes its transfer stats
    const busy = d && (d.state === "downloading" || d.state === "starting");
    if (busy || ul[g.id]) {
      const gear = g.have ? `<button class="gear" title="Settings for ${esc(g.title)}" aria-label="Settings for ${esc(g.title)}"
        onclick="openGear('${esc(g.id)}')">⚙&#xFE0E;</button>` : "";
      h += `<div class="card gcard xfer ${busy ? "down" : "up"}">${gear}<h3>${esc(g.title)}</h3>${xferHTML(busy ? d : null, ul[g.id])}</div>`;
      continue;
    }
    // card layout: title, one meta line, a few tags, Launch / Host / hearts
    const tags = [];
    if (g.arch) tags.push(`<span class="tag">${esc(g.arch)}</span>`);
    if (g.genre) tags.push(`<span class="tag">${esc(g.genre)}</span>`);
    if (g.network === "ipx") tags.push(`<span class="tag ipx">IPX${g.ipx_detected ? "?" : ""}</span>`);
    if (g.kind === "dos") tags.push(`<span class="tag dos">DOS</span>`);
    const lan = g.lan || {};
    if (lan.error) tags.push(`<span class="tag need">${esc(lan.error)}</span>`);
    const pl = gPlayers(g);
    const metaBits = [gb(g.size || 0)];
    if (pl) metaBits.push(`Max players: ${pl.max}`);
    if (g.year) metaBits.push(`Released: ${esc(g.year)}`);
    metaBits.push(`on ${esc(g.owner_names.join(", "))}`);
    const detail = [`${g.files || 0} files`, g.network && g.network !== "ipx" ? `network: ${g.network}` : "",
                    (g.prereqs || []).length ? `needs: ${g.prereqs.join(", ")}` : "", lan.join || lan.host ? "has lan.json" : ""]
                   .filter(Boolean).join(" · ");
    // per-game settings (exe, arguments, ForceBindIP, admin, folder, setup, registry, firewall): the gear
    const gear = g.have ? `<button class="gear" title="Settings for ${esc(g.title)}" aria-label="Settings for ${esc(g.title)}"
      onclick="openGear('${esc(g.id)}')">⚙&#xFE0E;</button>` : "";
    const fbRow = "";
    h += `<div class="card gcard ${g.have ? "" : "missing"}">${gear}<h3>${esc(g.title)}</h3>
      <div class="meta" title="${esc(detail)}">${metaBits.join(" · ")}</div>
      ${tags.length ? `<div class="tags">${tags.join("")}</div>` : ""}
      <div class="row acts">${actions}${heartsHTML(g)}</div>${fbRow}${prog}</div>`;
  }
  if (heartBusy) return;            // don't redraw under a drag or while arrow keys are still being pressed
  const ae = document.activeElement;
  const focusedHeart = ae && ae.classList && ae.classList.contains("hearts") ? ae.dataset.id : "";
  put("#libgrid", h + `</div>`);
  // a redraw replaces the focused hearts: put the keyboard focus back on the same game's hearts
  if (focusedHeart && !ae.isConnected) {
    const el = document.querySelector(`#libgrid .hearts[data-id="${CSS.escape(focusedHeart)}"]`);
    if (el) el.focus();
  }
}

// ---------- Game settings dialog (the gear) ----------
// Built once when opened, outside the grid, so the 1.5 s refresh never resets what you're editing.
function openGear(id) {
  const g = S.library.find((x) => x.id === id);
  if (!g) return;
  closeGear();
  const fb = g.force_bind || {};
  const kb = (n) => n >= 1024 ? (n / 1024).toFixed(1) + " MB" : n + " KB";
  const cur = g.exe_source === "this PC" ? g.exe : "";
  const exeOpts = [`<option value="" ${cur ? "" : "selected"}>Default: ${esc(g.default_exe || "none found")} (${esc(g.default_source || "automatic")})</option>`]
    .concat((g.exes || []).map((e) => `<option value="${esc(e.rel)}" ${cur && cur.toLowerCase() === e.rel.toLowerCase() ? "selected" : ""}>${esc(e.rel)} · ${kb(e.kb)}${e.rel === g.auto_exe ? " · auto-detected" : ""}${e.hint ? " · " + esc(e.hint) : ""}</option>`)).join("");
  const argsVal = g.args_source === "this PC" ? g.args : (g.args || "");
  const lan = g.lan || {};
  const bg = document.createElement("div");
  bg.id = "gearbg";
  bg.innerHTML = `<div class="dlg" role="dialog" aria-modal="true" aria-label="${esc(g.title)} settings">
    <div class="row between"><h3 style="margin:0">⚙&#xFE0E; ${esc(g.title)}</h3><button class="mini" onclick="closeGear()">✕</button></div>
    <div class="meta" style="margin:4px 0 12px">Now starts: <b>${esc(g.exe || "nothing")}</b> (${esc(g.exe_source || "")})${g.args ? ` · arguments: <code>${esc(g.args)}</code> (${esc(g.args_source || "default")})` : ""}</div>
    <label class="fl">Executable</label>
    <select id="gexe" style="width:100%">${exeOpts}</select>
    <div class="meta">Every program in the game folder. Used for Launch, and for Host/Join when the game has no lan.json command for them.</div>
    <label class="fl">Extra command-line arguments (Launch)</label>
    <input type="text" id="gargs" value="${esc(argsVal)}" placeholder="e.g. -windowed -nointro" style="width:100%;min-width:0">
    <label class="ck"><input type="checkbox" id="gshare"> Share these with other players (saved in the game's lan.json, which travels with the game; the old file is backed up)</label>
    <hr>
    ${fb.applies ? (fb.lan_off ? `<div class="meta">ForceBindIP is off in this game's lan.json.</div>`
      : `<label class="ck"><input type="checkbox" id="gfb" ${fb.on ? "checked" : ""}> Use the VPN address in internet parties (ForceBindIP). Untick if the game needs the internet or misbehaves.</label>`) : ""}
    <label class="ck"><input type="checkbox" id="gadmin" ${g.run_as_admin ? "checked" : ""}> Run as administrator (normally games start as your normal user)</label>
    <div class="row" style="margin-top:12px">
      <button class="b alt" onclick="act('game/open_folder',{id:'${esc(g.id)}'})">Open game folder</button>
      ${lan.setup ? `<button class="b alt" onclick="act('setup',{id:'${esc(g.id)}'},'Setup started')">Run one-time setup again</button>` : ""}
      ${g.has_reg ? `<button class="b alt" onclick="act('game/reg_import',{id:'${esc(g.id)}'},adminMsg('Importing the registry file…'))">Re-import registry (.reg)</button>` : ""}
      <button class="b alt" onclick="act('game/firewall',{id:'${esc(g.id)}'},adminMsg('Opening firewall ports for this game…'))">Open firewall ports for this game</button>
    </div>
    <div class="row" style="margin-top:16px;justify-content:flex-end">
      <button class="b alt" onclick="closeGear()">Cancel</button>
      <button class="b" onclick="saveGear('${esc(g.id)}')">Save</button></div></div>`;
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) closeGear(); });
  document.body.appendChild(bg);
  bg.querySelector("#gexe").focus();
}
function closeGear() { const bg = document.getElementById("gearbg"); if (bg) bg.remove(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeGear(); });
async function saveGear(id) {
  const v = (sel) => document.getElementById(sel);
  const body = { id, exe: v("gexe").value, args: v("gargs").value, share: v("gshare").checked, run_as_admin: v("gadmin").checked };
  if (v("gfb")) body.force_bind = v("gfb").checked;
  const r = await api("game/settings", body);
  if (r.ok === false) { toast(r.error || "Not saved"); return; }
  closeGear();
  toast((r.msg || "Saved.") + " Rescanning the game…");
  await refresh();
}

async function hostGame(id) {
  await act("party/start", { id }, "Hosting for everyone");
}

// ---------- Vote ----------
function myBallot(p) { const b = p.ballots.find((x) => x.voter === S.me.id); return b ? b.ranking : []; }
function draft(p) { if (!drafts[p.id]) drafts[p.id] = myBallot(p).slice(); return drafts[p.id]; }
function moveRank(pid, i, dir) { const d = drafts[pid]; const j = i + dir; if (j < 0 || j >= d.length) return; [d[i], d[j]] = [d[j], d[i]]; render(true); }
function addRank(pid, c) { drafts[pid].push(c); render(true); }
function dropRank(pid, i) { drafts[pid].splice(i, 1); render(true); }
async function submitBallot(pid) { await act("vote/ballot", { poll: pid, ranking: drafts[pid] }, "Ballot sent to everyone"); }

function vVote() {
  // only the Party Master starts polls (the launcher refuses the others too); everyone votes
  const pms = S.peers.filter((p) => p.party_master).map((p) => p.name);
  let h = `<div class="row"><h2 style="margin:0">Ranked-choice vote</h2>`;
  if (S.me.party_master) h += `<span class="crown" title="You are the Party Master">👑</span>
    <button class="b" onclick="openPicker()">New poll…</button>
    <button class="b alt" onclick="openPicker('liked')" title="Opens the game list with the 8 best-scored games ticked">New poll (most liked)…</button></div>`;
  else h += `<span class="dim">${pms.length ? `Polls are started by the Party Master: 👑 <b>${esc(pms.join(", "))}</b>.`
    : "Polls are started by the Party Master. Nobody is Party Master yet: one player ticks it in Settings."}</span></div>`;
  if (S.me.party_master) h += scoresHTML();
  if (!S.polls.length) h += `<p class="dim">No polls yet.</p>`;
  for (const p of S.polls) {
    const T = (c) => esc(p.titles[c] || c);
    const res = p.result;
    const last = res.rounds[res.rounds.length - 1];
    h += `<div class="card" style="margin-top:14px"><div class="row"><h3 style="margin:0">${esc(p.title)}</h3>
      <span class="meta">by ${esc(p.creator_name)} · ${p.ballots.length} ballot(s) · ${p.closed ? "closed" : "open"}</span>
      ${!p.closed ? `<button class="mini" onclick="act('vote/close',{poll:'${p.id}'},'Poll closed')">Close poll</button>` : ""}</div>`;
    h += `<div class="cols" style="margin-top:10px">`;
    if (!p.closed) {
      const d = draft(p);
      // most-liked first, with the party's hearts average, so the ranking starts from what people want to play
      const likeOf = (c) => ((S.library.find((g) => g.id === c) || {}).like || {});
      const pool = p.candidates.filter((c) => !d.includes(c))
        .sort((a, b) => (likeOf(b).avg || 0) - (likeOf(a).avg || 0) || (p.titles[a] || a).localeCompare(p.titles[b] || b));
      const likeTag = (c) => likeOf(c).n ? ` <span class="meta">♥ ${likeOf(c).avg} (${likeOf(c).n})</span>` : "";
      h += `<div><b>Your ranking</b> <span class="meta">(1 = favourite; rank as many as you like)</span>
        <ol class="rank">${d.map((c, i) => `<li>${T(c)} <button class="mini" onclick="moveRank('${p.id}',${i},-1)">▲</button>
          <button class="mini" onclick="moveRank('${p.id}',${i},1)">▼</button> <button class="mini" onclick="dropRank('${p.id}',${i})">✕</button></li>`).join("")}</ol>
        <br><button class="b" onclick="submitBallot('${p.id}')">Send ballot</button>
        <h2>Add to ranking</h2><ul class="pool">${pool.map((c) => `<li><button class="mini" onclick="addRank('${p.id}','${esc(c)}')">+</button> ${T(c)}${likeTag(c)}</li>`).join("")}</ul></div>`;
    } else h += `<div>${res.winner ? `Winner:<div class="winner">${T(res.winner)}</div>` : "No winner"}
        ${res.winner ? `<br><button class="b purple" onclick="hostGame('${esc(res.winner)}')">Host the winner for everyone</button>` : ""}</div>`;
    h += `<div><b>${p.closed ? "Count" : "Live count"}</b>${res.winner ? ` · leading: <span class="ok">${T(res.winner)}</span>` : ""}
      <table><tr><th>Round</th><th>First preferences</th><th>Out</th></tr>
      ${res.rounds.map((r, i) => `<tr><td>${i + 1}</td><td>${Object.entries(r.counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${T(c)}: ${n}`).join("<br>")}</td>
        <td>${r.eliminated ? T(r.eliminated) : ""}</td></tr>`).join("")}</table>
      <div class="meta" style="margin-top:6px">Voted: ${p.ballots.map((b) => esc(b.voter_name)).join(", ") || "nobody yet"}</div></div></div></div>`;
  }
  put("#view", h);
}

// ---------- Party Master: heart scores and the game picker ----------
let scoreSort = { k: "avg", dir: -1 };
function sortScores(k) { scoreSort = { k, dir: scoreSort.k === k ? -scoreSort.dir : (k === "title" ? 1 : -1) }; render(true); }
function scoresHTML() {
  const sc = S.scores;
  if (!sc) return "";
  const k = scoreSort.k, dir = scoreSort.dir;
  const rows = sc.games.slice().sort((a, b) => (k === "title" ? dir * a.title.localeCompare(b.title, undefined, { sensitivity: "base" })
    : dir * (a[k] - b[k])) || b.avg - a.avg || a.title.localeCompare(b.title));
  const arrow = (c) => k === c ? (dir < 0 ? " ▼" : " ▲") : "";
  const th = (c, label) => `<th><button class="link" onclick="sortScores('${c}')">${label}${arrow(c)}</button></th>`;
  return `<details class="card scores" style="margin-top:14px" ${scoresOpen ? "open" : ""} ontoggle="scoresOpen=this.open">
    <summary><b>👑 Party heart scores</b> <span class="meta">${sc.scored ? `${sc.scored} game${sc.scored === 1 ? "" : "s"} scored · overall <b>${sc.overall.toFixed(1)} / 10</b>` : "nobody has given hearts yet"}${sc.unscored ? ` · ${sc.unscored} with no score (not counted)` : ""}</span></summary>
    <p class="meta">Each game's hearts averaged out of 10 over the players who scored it. Only you see this, as Party Master.</p>
    ${sc.games.length ? `<table><tr>${th("title", "Game")}${th("avg", "Score")}${th("n", "Players")}<th>Who</th></tr>
      ${rows.map((g) => `<tr><td>${esc(g.title)}</td><td><b>${g.avg.toFixed(1)}</b> / 10</td><td>${g.n}</td><td class="dim">${esc(g.voters.join(", "))}</td></tr>`).join("")}</table>` : ""}</details>`;
}
let scoresOpen = true;
const likeOfG = (g) => g.like || {};
function pickerSorted() {
  return S.library.slice().sort((a, b) => (likeOfG(b).n ? likeOfG(b).avg : -1) - (likeOfG(a).n ? likeOfG(a).avg : -1)
    || (likeOfG(b).n || 0) - (likeOfG(a).n || 0) || (a.title || "").localeCompare(b.title || "", undefined, { sensitivity: "base" }));
}
function pickerPreset(which) {
  const all = pickerSorted(), n = S.peers.length + 1;
  let ids;
  if (which === "all") ids = all.map((g) => g.id);
  else if (which === "none") ids = [];
  else if (which === "owned") ids = all.filter((g) => g.owners.length >= n).map((g) => g.id);
  else ids = all.filter((g) => likeOfG(g).n).slice(0, 8).map((g) => g.id);       // "liked": the 8 best scored
  document.querySelectorAll("#gearbg input.pk").forEach((c) => c.checked = ids.includes(c.value));
  pickerCount();
}
function pickerCount() {
  const n = document.querySelectorAll("#gearbg input.pk:checked").length;
  const el = document.getElementById("pkn");
  if (el) el.textContent = `${n} game${n === 1 ? "" : "s"} ticked`;
}
// Built once when opened (on top of the page, like the gear), so the refresh never unticks anything.
function openPicker(preset) {
  closeGear();
  const games = pickerSorted();
  if (!games.length) { toast("No games in the party yet."); return; }
  const scored = games.filter((g) => likeOfG(g).n).length;
  const bg = document.createElement("div");
  bg.id = "gearbg";
  bg.innerHTML = `<div class="dlg" role="dialog" aria-modal="true" aria-label="New poll">
    <div class="row between"><h3 style="margin:0">👑 New poll</h3><button class="mini" onclick="closeGear()">✕</button></div>
    <label class="fl">Question</label>
    <input type="text" id="ptitle" placeholder="What do we play next?" style="width:100%;min-width:0">
    <label class="fl">Games in the poll <span class="meta">(party heart score out of 10; best first)</span></label>
    <div class="row" style="gap:6px">
      <button class="chip" onclick="pickerPreset('liked')" ${scored ? "" : "disabled"}>Top 8 scored</button>
      <button class="chip" onclick="pickerPreset('owned')">Everyone has</button>
      <button class="chip" onclick="pickerPreset('all')">All</button>
      <button class="chip" onclick="pickerPreset('none')">None</button>
      <span class="meta" id="pkn"></span></div>
    <div class="picklist">${games.map((g) => {
      const l = likeOfG(g);
      return `<label class="pick"><input type="checkbox" class="pk" value="${esc(g.id)}" onchange="pickerCount()">
        <span class="pt">${esc(g.title)}${g.have ? "" : ' <span class="meta">(not on this PC)</span>'}</span>
        <span class="ps ${l.n ? "" : "dim"}" title="${esc(l.n ? (l.voters || []).join(", ") : "no hearts yet")}">${l.n ? `♥ ${(+l.avg).toFixed(1)} / 10 · ${l.n}` : "no score"}</span></label>`;
    }).join("")}</div>
    <div class="meta">Everyone's launcher switches to the Vote tab when the poll opens.</div>
    <div class="row" style="margin-top:16px;justify-content:flex-end">
      <button class="b alt" onclick="closeGear()">Cancel</button>
      <button class="b" onclick="submitPicker()">Start poll</button></div></div>`;
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) closeGear(); });
  document.body.appendChild(bg);
  // preset: the top-scored games (or all of them when nobody has given hearts yet)
  pickerPreset(preset === "liked" || scored >= 2 ? "liked" : "all");
  if (preset === "liked" && scored < 2) toast("Not many games have hearts yet: fill some in on the Library tab.");
  bg.querySelector("#ptitle").focus();
}
async function submitPicker() {
  const ids = [...document.querySelectorAll("#gearbg input.pk:checked")].map((c) => c.value);
  if (ids.length < 2) { toast("Tick at least 2 games for the poll."); return; }
  const title = (document.getElementById("ptitle").value || "").trim();
  const r = await act("vote/new", { title, candidates: ids }, "Poll opened for everyone");
  if (r.ok !== false) closeGear();
}

// ---------- Prereqs ----------
async function vPrereqs() {
  if (!pre) { pre = await api("prereqs"); setTimeout(() => { pre = null; }, 2500); }
  const inst = pre.installer;
  const missing = pre.items.filter((i) => i.needed_by.length && !i.installed);
  let h = `<div class="row"><h2 style="margin:0">Prerequisites</h2>
    <button class="b" ${missing.length && !inst.running ? "" : "disabled"} onclick="act('prereqs/install',{},adminMsg('Installing…'))">Install all missing (${missing.length})</button>
    <button class="mini" onclick="api('prereqs?force=1').then(r=>{pre=r;render(true)})">Re-check</button></div>
    <p class="dim">Found from each game's exe imports and bundled redist folders. One admin prompt covers every install.</p>
    <table><tr><th>Component</th><th>This PC</th><th>Needed by</th><th>Source</th><th></th></tr>`;
  for (const i of pre.items) {
    if (!i.needed_by.length && i.installed) continue;
    h += `<tr><td>${esc(i.name)}</td><td>${i.installed ? '<span class="ok">installed</span>' : '<span class="no">missing</span>'}</td>
      <td>${esc(i.needed_by.join(", ")) || '<span class="dim">no game</span>'}</td><td class="dim">${esc(i.how)}</td>
      <td>${i.installed ? "" : `<button class="mini" ${inst.running ? "disabled" : ""} onclick="act('prereqs/install',{ids:['${i.id}']},'Installing')">Install</button>`}</td></tr>`;
  }
  h += `</table>`;
  if (inst.log.length) h += `<h2>Install log</h2><pre class="log">${esc(inst.log.join("\n"))}</pre>`;
  if (tab === "prereqs") put("#view", h);
}

// ---------- Network ----------
async function vNetwork() {
  if (!net) { net = await api("net"); setTimeout(() => { net = null; }, 3000); }
  const fwRows = net.games.filter((g) => g.firewall.length || Object.keys(g.ports).length).map((g) =>
    `<tr><td>${esc(g.title)}</td><td>${Object.entries(g.ports).map(([k, v]) => `${k.toUpperCase()} ${esc(v)}`).join("<br>")}</td>
     <td>${g.firewall.map((f) => f.ok ? `<code>${esc(f.cmd)}</code>` : `<span class="no">skipped (not a plain netsh rule): ${esc(f.cmd)}</span>`).join("<br>")}</td></tr>`).join("");
  let h = `<h2>Players online</h2><table><tr><th>Name</th><th>Address</th><th>Version</th></tr>
    <tr><td>${esc(S.me.name)} (you)${S.me.party_master ? ' <span class="crown" title="Party Master">👑</span>' : ""}</td><td>${esc(S.me.ips.join(", "))}</td><td>${esc(S.version)}</td></tr>
    ${S.peers.map((p) => `<tr><td>${esc(p.name)}${p.party_master ? ' <span class="crown" title="Party Master">👑</span>' : ""}</td><td>${esc(p.ip)}:${p.port}</td><td>${esc(p.version)}</td></tr>`).join("")}</table>
    <h2>Transfer links</h2><p class="dim">The launcher probes every IP for each player and picks the fastest link for game transfers. A direct ethernet cable shows up as &lt;1ms.</p>
    <div id="linkinfo" class="dim">Loading…</div>
    <div class="row" style="margin-top:8px"><button class="b" onclick="probeLinks()">Probe all links now</button></div>
    <h2>Make old games see each other</h2>
    <div class="row"><button class="b" onclick="act('net/firewall',{},adminMsg('Adding firewall rules…'))">Open firewall for launcher + all games</button>
    <button class="b alt" ${net.ipxwrapper_ready ? "disabled" : ""} onclick="act('net/fetch_ipxwrapper',{},'IPXWrapper downloaded')">${net.ipxwrapper_ready ? "IPXWrapper ready" : "Download IPXWrapper"}</button>
    <span class="dim">DOSBox: ${net.dosbox ? esc(net.dosbox) : '<span class="no">bundled DOSBox-X missing (lanparty\\bin\\dosbox-x) – set a path in Settings</span>'}</span></div>
    <p class="dim">Windows IPX games get IPXWrapper (IPX over UDP on the LAN). DOS games run in DOSBox with IPX on; the party host runs the IPX server and everyone else connects to it automatically.</p>
    <table><tr><th>Game</th><th>Network</th><th>Type</th><th>IPX wrapper</th></tr>`;
  for (const g of net.games) {
    let w = "";
    if (g.kind === "dos") w = `<span class="dim">DOSBox IPX (automatic)</span>`;
    else if (g.kind === "pe" && g.arch !== "x64") w = g.wrapped
      ? `<span class="ok">installed</span> <button class="mini" onclick="act('net/wrap',{id:'${esc(g.id)}',on:false},'Removed').then(()=>net=null)">Remove</button>`
      : `<button class="mini" onclick="act('net/wrap',{id:'${esc(g.id)}',on:true},'Installed').then(()=>net=null)">Install</button>`;
    h += `<tr><td>${esc(g.title)}</td><td>${esc(g.network || "-")}</td><td>${esc(g.kind)} ${esc(g.arch || "")}</td><td>${w}</td></tr>`;
  }
  if (fwRows) h += `</table><h2>Ports and firewall rules from lan.json</h2><p class="dim">Added when you press "Open firewall" above.</p>
    <table><tr><th>Game</th><th>Ports</th><th>Extra commands</th></tr>${fwRows}`;
  h += `</table><h2>Network adapters</h2><table><tr><th>Adapter</th><th>IPv4</th><th></th></tr>
    ${net.adapters.map((a) => `<tr><td>${esc(a.name)}</td><td>${esc(a.ipv4)}</td><td>${a.virtual ? '<span class="no">virtual/VPN – old games may broadcast here instead of the LAN; disable it during the party if games can\'t see each other</span>' : '<span class="ok">ok</span>'}</td></tr>`).join("")}</table>`;
  if (net.log.length) h += `<h2>Log</h2><pre class="log">${esc(net.log.join("\n"))}</pre>`;
  if (tab === "network") put("#view", h);
  loadLinks();
}
async function loadLinks() {
  try {
    const r = await api("links");
    const el = document.getElementById("linkinfo");
    if (!el || !r.links) return;
    if (!r.links.length) { el.textContent = "No other players online."; return; }
    let h = `<table><tr><th>Player</th><th>IPs</th><th>Selected link</th></tr>`;
    for (const l of r.links) {
      const probes = Object.entries(l.probes || {}).map(([ip, ms]) =>
        `${esc(ip)}: <b>${ms < 1 ? '<span class="ok">&lt;1ms (direct)</span>' : ms < 5 ? `<span class="ok">${ms}ms</span>` : `<span class="dim">${ms}ms</span>`}</b>`).join(", ") || '<span class="dim">not probed yet</span>';
      h += `<tr><td>${esc(l.name)}</td><td>${probes}</td><td><span class="ok">${esc(l.best_ip)}</span></td></tr>`;
    }
    el.innerHTML = h + `</table>`;
  } catch(e) {}
}
async function probeLinks() {
  toast("Probing all links…");
  await act("links/probe", {}, "Links probed");
  net = null;
}

// ---------- Internet Party ----------
function vInternet() {
  const v = S.vpn || {};
  const off = v.state === "off" || v.state === "error" || !v.state;
  const members = S.auth_members || [];
  let h = `<h2>Internet Party</h2>
    <p class="dim">Play and sync games with friends over the internet using an encrypted VPN (n2n).
    Broadcast-compatible: old LAN games discover each other automatically.</p>`;

  // prerequisites
  h += `<div class="card"><h3>Prerequisites</h3>
    <div class="row">
      <span>n2n: ${v.n2n_ready ? '<span class="ok">bundled</span>' : '<span class="no">missing from lanparty\\bin</span>'}</span>
      <span style="margin-left:20px">Admin: ${v.admin ? '<span class="ok">yes</span>' : '<span class="no">no, start with run_admin.bat</span>'}</span>
      <span style="margin-left:20px">TAP adapter: ${v.tap_adapter === "ok" ? '<span class="ok">LanPartyVPN ready</span>'
        : v.tap_adapter === "disabled" ? '<span class="no">LanPartyVPN disabled</span>'
        : v.tap_installed ? '<span class="no">not created yet</span>' : '<span class="no">driver not installed</span>'}</span>
      ${v.tap_adapter !== "ok" ? `<button class="b" onclick="act('vpn/install_tap',{},'Setting up the TAP adapter…')" ${v.admin ? "" : "disabled"}>Set up TAP</button>` : ""}
    </div></div>`;

  if (off) {
    // The last party (kept 24 h, cleared by Leave party): one click to get back in after a crash or a PC restart
    const lp = S.last_party || {};
    const ready = v.n2n_ready && v.tap_installed && v.admin;
    if (lp.role) {
      const when = lp.at ? new Date(lp.at * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "recently";
      h += `<div class="card" style="margin-top:16px"><h3>Last party</h3>
        <p class="dim">${lp.role === "host" ? "You hosted" : "You joined"} a party (${esc(when)}) and didn't leave it.
        ${lp.role === "host" ? "Hosting it again keeps the same party, so your friends' code still works (unless your internet or LAN address changed; then you get a new code to share)." : ""}</p>
        <div class="row">${lp.role === "host"
          ? `<button class="b purple" onclick="act('vpn/create_party',{again:true},'Hosting the party again…')" ${ready ? "" : "disabled"}>Host again</button>`
          : `<button class="b" onclick="act('vpn/join_party',{again:true},'Rejoining…')" ${ready ? "" : "disabled"}>Rejoin last party</button>`}</div></div>`;
    }
    // Create or join
    h += `<div class="cols" style="margin-top:16px">
      <div class="card"><h3>Host a party</h3>
        <p class="dim">You'll get a code to share with your friends. They enter it to join.
        You need UDP port 7654 forwarded on your router (or a friend runs the supernode).</p>
        <div class="row"><input type="text" id="vip" placeholder="Your external IP (auto-detected if empty)" style="min-width:180px">
          <input type="text" id="vport" placeholder="7654" style="width:70px">
          <button class="b purple" onclick="createParty()" ${v.n2n_ready && v.tap_installed && v.admin ? "" : "disabled"}>Create party</button></div>
      </div>
      <div class="card"><h3>Join a party</h3>
        <p class="dim">Paste the code the host gave you.</p>
        <div class="row"><input type="text" id="vjoin" placeholder="ABCD-EFGH-IJKL-…" style="min-width:280px" value="${esc(lp.code || "")}">
          <button class="b" onclick="joinParty()" ${v.n2n_ready && v.tap_installed && v.admin ? "" : "disabled"}>Join</button></div>
      </div></div>`;
  } else {
    // Active party
    const isHost = v.state === "host";
    h += `<div class="card" style="margin-top:16px"><h3>${isHost ? "🌐 Hosting" : "🌐 Connected"}</h3>
      <div class="meta">VPN IP: <b>${esc(v.my_ip)}</b> · Party ID: ${esc(v.party_id)} · Supernode: ${esc(v.host_ip)}:${v.supernode_port || 7654}</div>`;
    if (isHost && v.code) h += `<div style="margin:8px 0"><b>Party code:</b> <code id="pcode">${esc(v.code)}</code>
      <button class="mini" onclick="navigator.clipboard.writeText($('#pcode').textContent);toast('Copied!')">Copy</button></div>`;
    h += `<div class="row"><button class="b alt" onclick="act('vpn/leave_party',{},'Left the party')">Leave party</button></div></div>`;

    // Members
    if (members.length) {
      h += `<h3 style="margin-top:16px">Members (${members.length})</h3><table><tr><th>Name</th><th>Peer ID</th><th>Joined</th>${isHost ? "<th></th>" : ""}</tr>`;
      for (const m of members) {
        const t = new Date(m.joined * 1000).toLocaleTimeString();
        h += `<tr><td>${esc(m.name || m.id)}</td><td class="dim">${esc(m.id)}</td><td class="dim">${t}</td>
          ${isHost && m.id !== S.me.id ? `<td><button class="mini" onclick="act('vpn/kick',{peer_id:'${esc(m.id)}'},'Kicked')">Kick</button></td>` : (isHost ? "<td></td>" : "")}</tr>`;
      }
      h += `</table>`;
    }

    // Audit log
    h += `<details style="margin-top:16px"><summary class="dim">Security audit log</summary><div id="auditlog">Loading…</div></details>`;
    loadAudit();
  }

  // n2n log
  if (v.log && v.log.length) h += `<h3 style="margin-top:16px">VPN log</h3><pre class="log">${esc(v.log.join("\n"))}</pre>`;

  if (v.error) h += `<div class="card" style="margin-top:12px"><span class="no">Error: ${esc(v.error)}</span></div>`;
  put("#view", h);
}

let _partyCode = "";
async function createParty() {
  const ip = ($("#vip") || {}).value || "";
  const port = parseInt(($("#vport") || {}).value) || 7654;
  const r = await act("vpn/create_party", { external_ip: ip, port }, "Party created!");
  if (r.ok && r.code) { _partyCode = r.code; toast("Party code: " + r.code); }
}
async function joinParty() {
  const code = ($("#vjoin") || {}).value || "";
  await act("vpn/join_party", { code }, "Joined!");
}
async function loadAudit() {
  try {
    const r = await api("vpn/audit");
    const el = document.getElementById("auditlog");
    if (!el || !r.entries) return;
    if (!r.entries.length) { el.textContent = "No requests yet."; return; }
    let h = `<table><tr><th>Time</th><th>IP</th><th>Player</th><th>Path</th><th>Result</th></tr>`;
    for (const e of r.entries.slice(-50).reverse()) {
      const t = new Date(e.t * 1000).toLocaleTimeString();
      h += `<tr><td class="dim">${t}</td><td class="dim">${esc(e.ip)}</td><td>${esc(e.player)}</td>
        <td>${esc(e.path)}</td><td>${e.ok ? '<span class="ok">OK</span>' : `<span class="no">${esc(e.reason)}</span>`}</td></tr>`;
    }
    el.innerHTML = h + `</table>`;
  } catch (e) {}
}

// ---------- Settings ----------
function vSettings() {
  const st = S.settings;
  put("#view", `<h2>Your name</h2><div class="row"><input type="text" id="sname" value="${esc(st.name)}">
      <button class="b" onclick="act('settings',{name:$('#sname').value},'Saved')">Save</button></div>
    <h2>Games folders</h2><p class="dim">Each sub-folder inside is one game. Put a <code>lanparty.json</code> in a game folder to override its exe, args, network or prereqs.</p>
    <table>${st.folders.map((f) => `<tr><td>${esc(f)}</td><td><button class="mini" onclick='act("folders/remove",{path:${JSON.stringify(f)}})'>Remove</button></td></tr>`).join("")}</table><br>
    <div class="row"><button class="b" onclick="act('folders/add',{pick:true},'Folder added')">Browse for folder…</button>
      <input type="text" id="sfolder" placeholder="or paste a path, e.g. D:\\Games">
      <button class="b alt" onclick="act('folders/add',{path:$('#sfolder').value},'Folder added')">Add</button></div>
    <h2>Downloads go to</h2><div class="row"><input type="text" id="sdl" value="${esc(st.download_dir)}" placeholder="(first games folder)">
      <button class="b" onclick="act('settings',{download_dir:$('#sdl').value},'Saved')">Save</button></div>
    <h2>Media folders</h2><p class="dim">Library &gt; Movies, TV, Music, Books and Misc. Each file or sub-folder in a category's folder
      is shared with the party, and what you copy from other players goes into the same folder.</p>
    <table>${LIB_CATS.slice(1).map(([c, icon, label]) => { const f = (st.media_folders || {})[c] || "";
      return `<tr><td style="width:110px">${icon} ${label}</td><td>${f ? esc(f) : `<span class="dim">not chosen</span>`}</td>
        <td style="white-space:nowrap"><button class="mini" onclick="pickMediaFolder('${c}')">Browse…</button>
        ${f ? `<button class="mini" onclick="act('media/folder',{cat:'${c}',clear:true},'${label} folder cleared')">Clear</button>` : ""}</td></tr>`; }).join("")}</table>
    <label style="display:block;margin-top:8px"><input type="checkbox" ${st.share_media !== false ? "checked" : ""} onchange="act('settings',{share_media:this.checked},'Saved')">
      Let other players copy my movies, TV, music, books and misc files</label>
    <h2>DOSBox</h2><div class="row"><input type="text" id="sdb" value="${esc(st.dosbox_path)}" placeholder="Empty = the bundled DOSBox-X (set a path only to use another DOSBox)">
      <button class="b" onclick="act('settings',{dosbox_path:$('#sdb').value},'Saved')">Save</button></div>
    <h2>Extra players by address</h2><p class="dim">Only needed if your network blocks broadcast. One ip:port per line, comma-separated.</p>
    <div class="row"><input type="text" id="speers" value="${esc(st.static_peers.join(", "))}" placeholder="192.168.1.20:47801">
      <button class="b" onclick="act('settings',{static_peers:$('#speers').value.split(',')},'Saved')">Save</button></div>
    <h2>Sharing</h2><label><input type="checkbox" ${st.share_library ? "checked" : ""} onchange="act('settings',{share_library:this.checked},'Saved')">
      Let other players copy games from me</label>
    <h2>🎮 While I'm playing</h2><label><input type="checkbox" ${st.limit_while_playing !== false ? "checked" : ""} onchange="act('settings',{limit_while_playing:this.checked},'Saved')">
      Slow down game copies while a game is running on this PC</label>
    <div class="row" style="margin-top:6px"><span>Limit, each way:</span><input type="number" id="slim" min="0.1" step="0.5" style="width:90px" value="${esc(st.limit_mbps ?? 1)}"> MB/s
      <button class="b" onclick="act('settings',{limit_mbps:parseFloat($('#slim').value)},'Saved')">Save</button></div>
    <p class="dim">Copies to and from other players keep going, just slower, so they don't lag the match. A program running from one of your game folders
      (or DOSBox) counts as playing. ${(S.throttle || {}).playing ? `<b>Playing ${esc(S.throttle.title)} now.</b>` : "No game running now."}</p>
    <h2>👑 Party Master</h2><label><input type="checkbox" id="spm" ${st.party_master ? "checked" : ""} onchange="act('settings',{party_master:this.checked},this.checked ? 'You are the Party Master' : 'Party Master off')">
      I am the Party Master</label>
    <p class="dim">One player per party. The Party Master picks the games and starts the polls (everyone's screen switches to the vote),
      and sees the party's heart score for every game (out of 10, averaged over the players who gave it hearts).</p>
    ${(() => { const o = S.peers.filter((p) => p.party_master).map((p) => p.name);
      return o.length ? `<p class="${st.party_master ? "no" : "dim"}">👑 ${esc(o.join(", "))} ${o.length === 1 ? "is" : "are"} Party Master${st.party_master ? " too: only one of you should be." : "."}</p>` : ""; })()}
    <h2>Windows Defender</h2><p class="dim">Exclude your game folders from real-time scanning so old games don't stutter or get quarantined.</p>
    <div>${defenderHtml()}</div>
    <div class="row" style="margin-top:8px"><button class="b" onclick="act('defender/exclude',{},adminMsg('Adding exclusions…')).then(()=>{def=null;loadDefender()})">Add all game folders to Defender exclusions</button>
    <button class="mini" onclick="def=null;loadDefender()">Re-check</button></div>`);
  if (!def) loadDefender();
}
function defenderHtml() {
  if (!def) return '<span class="dim">Checking…</span>';
  let h = "";
  if (def.excluded.length) h += def.excluded.map(f => `<div><span class="ok">✓ excluded:</span> ${esc(f)}</div>`).join("");
  if (def.not_excluded.length) h += def.not_excluded.map(f => `<div><span class="no">✗ not excluded:</span> ${esc(f)}</div>`).join("");
  if (!def.excluded.length && !def.not_excluded.length) h = "No game folders configured.";
  return h;
}
async function loadDefender() {
  try { def = await api("defender/status"); setTimeout(() => { def = null; }, 30000); render(true); } catch(e) {}
}
