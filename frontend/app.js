"use strict";

const API = (window.UMD_CONFIG && window.UMD_CONFIG.apiBase || "").replace(/\/$/, "");
const $ = (id) => document.getElementById(id);
const STAGES = { queued: "Queued", downloading: "Downloading", processing: "Processing", merging: "Merging",
  zipping: "Zipping", ready: "Ready", failed: "Failed" };
const AUDIO = [["mp3", "MP3"], ["m4a", "M4A"], ["opus", "Opus"], ["original", "Original (no conversion)"],
  ["flac", "FLAC (container only, not higher quality)"]];
const VIDEO = [["mp4", "MP4"], ["webm", "WebM"]];
const QUALITY = [["best", "Best available"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"], ["360", "360p"]];

let current = null;   // last analyze result
let limits = null;

// ---------------------------------------------------------------- helpers
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(API + path, { ...options, headers: { "Content-Type": "application/json" } });
  } catch {
    throw new Error("Cannot reach the server. The free server may be waking up — please try again in a minute.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Server error (${res.status}). Please try again.`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

function fmtTime(s) {
  if (!s && s !== 0) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
function fmtSize(b) {
  if (!b) return "";
  return b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.ceil(b / 1e3) + " KB";
}
function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c != null));
  return node;
}
function setOptions(select, options, value) {
  select.replaceChildren(...options.map(([v, label, disabled]) => el("option", { value: v, textContent: label, disabled: !!disabled })));
  if (value && options.some(([v, , d]) => v === value && !d)) select.value = value;
}
function showError(msg) { $("error").textContent = msg; $("error").hidden = !msg; }
const safeImg = (url) => (typeof url === "string" && url.startsWith("https://") ? url : "");
const platformName = { youtube: "YouTube", soundcloud: "SoundCloud", spotify: "Spotify" };

// ---------------------------------------------------------------- theme
(function theme() {
  const saved = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme").onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* storage unavailable */ }
  };
})();

// ---------------------------------------------------------------- server status (cold start)
async function checkServer() {
  const banner = $("server");
  const slow = setTimeout(() => {
    banner.textContent = "Waking up the free server… this can take up to a minute on the first visit.";
    banner.hidden = false;
  }, 2500);
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      const h = await api("/api/health");
      limits = h.limits;
      $("limits").textContent = `Server limits: up to ${limits.max_playlist_items} items per job, `
        + `${Math.round(limits.max_video_duration / 60)} min per item, ${limits.max_download_size_mb} MB per file; `
        + `files are kept for ${Math.round(limits.file_ttl / 60)} minutes.`;
      clearTimeout(slow);
      banner.hidden = true;
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 8000));
    }
  }
  clearTimeout(slow);
  banner.textContent = "The server is not responding right now. Please try again later.";
  banner.hidden = false;
}

// ---------------------------------------------------------------- analyze
$("analyze-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("analyze");
  showError("");
  $("result").hidden = true;
  btn.disabled = true;
  btn.textContent = "Analyzing…";
  try {
    current = await post("/api/analyze", { url: $("url").value.trim() });
    limits = current.limits || limits;
    renderResult();
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Analyze";
  }
});

function renderResult() {
  const r = current;
  $("thumb").src = safeImg(r.thumbnail);
  $("thumb").hidden = !safeImg(r.thumbnail);
  $("title").textContent = r.title || "Untitled";
  $("platform").textContent = platformName[r.platform] || r.platform;
  $("duration").textContent = r.kind === "single" ? fmtTime(r.duration) : `${r.count} item${r.count === 1 ? "" : "s"}`;
  $("uploader").textContent = r.uploader || "";
  $("notice").textContent = r.notice || "";
  $("notice").hidden = !r.notice;

  const isSpotify = r.kind === "spotify";
  const isPlaylist = r.kind === "playlist";
  const canVideo = r.platform === "youtube" && (r.kind === "playlist" || r.has_video);
  setOptions($("mode"), [["audio", "Audio"], ["video", "Video", !canVideo]], $("mode").value);
  if (!canVideo) $("mode").value = "audio";
  updateFormats();

  $("playlist").hidden = !(isPlaylist || isSpotify);
  $("select-all").parentElement.hidden = isSpotify;
  $("download").hidden = false;
  $("download").textContent = isPlaylist ? "Download selected" : isSpotify ? "" : "Download";
  if (isSpotify) $("download").hidden = true;
  $("entries").replaceChildren(...(r.entries || []).map((it) => (isSpotify ? spotifyRow(it) : playlistRow(it))));
  if (r.truncated) {
    $("entries").append(el("li", { className: "dim", textContent: `Only the first ${r.entries.length} of ${r.count} items are listed.` }));
  }
  $("select-all").checked = true;
  updateSelection();
  if (r.kind === "single" && r.too_long) {
    showError(`This media is longer than the server limit (${Math.round(limits.max_video_duration / 60)} minutes).`);
  }
  $("result").hidden = false;
}

function playlistRow(it) {
  const box = el("input", { type: "checkbox", checked: !it.too_long, disabled: it.too_long });
  box.dataset.url = it.url;
  box.addEventListener("change", updateSelection);
  const dur = it.too_long ? "too long" : fmtTime(it.duration);
  return el("li", {}, el("label", { className: "line" }, box,
    el("span", { textContent: `${String(it.index).padStart(2, "0")}  ${it.title}` }),
    el("span", { className: "dim", textContent: dur })));
}

function spotifyRow(it) {
  const cands = el("div", { className: "cands" });
  const find = el("button", { type: "button", className: "small", textContent: "Find source" });
  find.onclick = async () => {
    find.disabled = true;
    find.textContent = "Searching…";
    try {
      const res = await post("/api/resolve", { title: it.title, artist: it.uploader || "" });
      cands.replaceChildren(...(res.candidates.length ? res.candidates.map((c) => candidateRow(c, it))
        : [el("p", { className: "dim", textContent: "No public source found." })]));
      find.textContent = "Search again";
    } catch (err) {
      cands.replaceChildren(el("p", { className: "error-text", textContent: err.message }));
      find.textContent = "Retry";
    } finally {
      find.disabled = false;
    }
  };
  return el("li", {}, el("div", { className: "line" },
    el("span", { textContent: `${String(it.index).padStart(2, "0")}  ${it.uploader ? it.uploader + " – " : ""}${it.title}` }),
    el("span", { className: "dim", textContent: fmtTime(it.duration) }), find), cands);
}

function candidateRow(c, track) {
  const use = el("button", { type: "button", className: "small", textContent: "Use this" });
  use.onclick = () => startJob({ url: c.url, mode: "audio", format: $("format").value, quality: "best" },
    `${track.title} — from ${platformName[c.source]} (matched via Spotify metadata)`);
  const link = el("a", { href: c.url, target: "_blank", rel: "noopener noreferrer", textContent: platformName[c.source] });
  return el("div", { className: "cand" }, link,
    el("span", { textContent: `${c.title}${c.uploader ? " · " + c.uploader : ""} ${c.duration ? "· " + fmtTime(c.duration) : ""}` }), use);
}

function updateFormats() {
  const video = $("mode").value === "video";
  setOptions($("format"), video ? VIDEO.map(([v, l]) => [v, l, v === "webm" && current && current.kind === "single" && !current.has_webm])
    : AUDIO, $("format").value);
  const max = current && current.max_height;
  setOptions($("quality"), QUALITY.map(([v, l]) => [v, l, v !== "best" && max && Number(v) > max]), $("quality").value);
  $("quality-wrap").hidden = !video;
  if (current && current.kind === "spotify") $("quality-wrap").hidden = true;
  updateSize();
}
$("mode").addEventListener("change", updateFormats);
$("quality").addEventListener("change", updateSize);
$("format").addEventListener("change", updateSize);

function updateSize() {
  const r = current;
  let text = "";
  if (r && r.kind === "single") {
    const size = $("mode").value === "audio" ? r.audio_size
      : (r.video_sizes || {})[$("quality").value === "best" ? String(r.max_height >= 1080 ? 1080 : r.max_height || "") : $("quality").value];
    if (size) text = `Approximate size: ${fmtSize(size)}`;
    if ($("mode").value === "audio" && $("format").value === "flac") text += (text ? " · " : "") + "FLAC stores the same lossy source losslessly; it does not improve quality.";
  }
  $("size").textContent = text;
}

function selectedUrls() {
  return [...$("entries").querySelectorAll("input[type=checkbox]:checked")].map((b) => b.dataset.url);
}
function updateSelection() {
  if (!current || current.kind !== "playlist") return;
  const n = selectedUrls().length;
  const max = limits ? limits.max_playlist_items : Infinity;
  $("selected-count").textContent = `Selected: ${n}`;
  const over = n > max;
  $("limit-warning").hidden = !over;
  $("limit-warning").textContent = over
    ? `Playlist contains ${current.count} items. Public server limit: ${max} items/job. Please select up to ${max} items.` : "";
  $("download").disabled = over || n === 0;
}
$("select-all").addEventListener("change", (e) => {
  $("entries").querySelectorAll("input[type=checkbox]:not(:disabled)").forEach((b) => { b.checked = e.target.checked; });
  updateSelection();
});

$("download").addEventListener("click", () => {
  const r = current;
  const body = { url: r.webpage_url, mode: $("mode").value, format: $("format").value, quality: $("quality").value };
  if (r.kind === "playlist") Object.assign(body, { items: selectedUrls(), playlist_title: r.title });
  startJob(body, r.kind === "playlist" ? `${r.title} (${body.items.length} items)` : r.title);
});

// ---------------------------------------------------------------- jobs
async function startJob(body, label) {
  showError("");
  const card = $("job-tpl").content.firstElementChild.cloneNode(true);
  card.querySelector(".job-title").textContent = label;
  $("jobs").prepend(card);
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  card.querySelector(".remove").onclick = () => {
    if (card.dataset.id) api(`/api/jobs/${card.dataset.id}`, { method: "DELETE" }).catch(() => {});
    card.remove();
  };
  card.querySelector(".retry").onclick = () => { card.remove(); startJob(body, label); };
  renderJob(card, { status: "queued", progress: 0, items: [] });
  try {
    const job = await post("/api/jobs", body);
    card.dataset.id = job.id;
    poll(card, job.id);
  } catch (err) {
    renderJob(card, { status: "failed", error: err.message, items: [] });
  }
}

async function poll(card, id) {
  let failures = 0;
  while (card.isConnected) {
    try {
      const job = await api(`/api/jobs/${id}`);
      failures = 0;
      renderJob(card, job);
      if (job.status === "ready" || job.status === "failed") return;
    } catch (err) {
      if (++failures >= 5) return renderJob(card, { status: "failed", error: err.message, items: [] });
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

function renderJob(card, job) {
  const pill = card.querySelector(".pill");
  pill.textContent = STAGES[job.status] || job.status;
  pill.className = `pill ${job.status}`;
  const bar = card.querySelector(".bar");
  const busy = !["ready", "failed"].includes(job.status);
  bar.classList.toggle("indeterminate", busy && !job.progress);
  bar.firstElementChild.style.width = job.status === "ready" ? "100%" : `${job.progress || 0}%`;
  bar.hidden = job.status === "failed";
  const errorBox = card.querySelector(".job-error");
  errorBox.textContent = job.error || "";
  errorBox.hidden = !job.error;
  card.querySelector(".retry").hidden = job.status !== "failed";

  const files = card.querySelector(".job-files");
  const multi = job.items.length > 1;
  files.replaceChildren(...job.items.filter((it) => it.status === "ready" || (multi && it.status === "failed")).map((it) => {
    if (it.status === "failed") return el("li", {}, el("span", { className: "error-text", textContent: `✕ ${it.title || "Item " + (it.index + 1)}: ${it.error}` }));
    const a = el("a", { href: `${API}/api/jobs/${job.id}/files/${it.index}`, className: "primary", textContent: "Save" });
    a.style.minHeight = "34px";
    return el("li", {}, el("span", { textContent: `${it.name} (${fmtSize(it.size)})` }), a);
  }));
  const zip = card.querySelector(".zip");
  zip.hidden = !job.zip;
  if (job.zip) {
    zip.href = `${API}/api/jobs/${job.id}/zip`;
    zip.textContent = `Download ZIP (${fmtSize(job.zip.size)})`;
  }
  if (job.status === "ready" && job.expires_at && !card.querySelector(".expiry")) {
    const t = new Date(job.expires_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    card.append(el("p", { className: "hint expiry", textContent: `Files are deleted from the server at about ${t}.` }));
  }
}

if (!API) showError("Configuration error: API base URL is not set.");
checkServer();
