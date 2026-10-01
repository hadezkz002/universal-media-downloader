"use strict";
// URL extraction and platform/type guessing for the input box. UX only: the backend decides.

const LEADING = "\"'<([{“‘«";
const TRAILING = "\"'>]}”’».,;:!?…";

/** Return every http(s) URL in pasted text, cleaned of quotes and trailing punctuation. */
function extractUrls(text) {
  const urls = [];
  for (let token of (text || "").trim().split(/\s+/)) {
    while (token && LEADING.includes(token[0])) token = token.slice(1);
    if (!/^https?:\/\//i.test(token)) continue;
    while (token && (TRAILING.includes(token.at(-1))
      || (token.at(-1) === ")" && token.split(")").length > token.split("(").length))) token = token.slice(0, -1);
    try {
      const u = new URL(token);
      if (u.protocol === "http:" || u.protocol === "https:") urls.push(token);
    } catch { /* not a URL */ }
  }
  return urls;
}

/** Best-effort platform/type guess from the URL alone. type === null means "ask the server". */
function detectLocal(url) {
  const u = new URL(url);
  const host = u.hostname.toLowerCase().replace(/^(www|m|music)\./, "");
  const path = u.pathname;
  if (host === "youtu.be") return { platform: "youtube", type: u.searchParams.has("list") ? "playlist" : "video" };
  if (host === "youtube.com") {
    if (path.startsWith("/shorts/")) return { platform: "youtube", type: "shorts" };
    if (path === "/playlist" || u.searchParams.has("list")) return { platform: "youtube", type: "playlist" };
    if (path === "/watch") return { platform: "youtube", type: "video" };
    if (/^\/(@|channel\/|c\/|user\/)/.test(path)) return { platform: "youtube", type: "channel" };
    return { platform: "youtube", type: null };
  }
  if (host === "on.soundcloud.com") return { platform: "soundcloud", type: null, shortLink: true };
  if (host === "soundcloud.com") {
    const parts = path.split("/").filter(Boolean);
    if (parts[1] === "sets") return { platform: "soundcloud", type: "playlist" };
    if (parts.length === 1 || ["tracks", "albums", "sets", "reposts", "likes", "popular-tracks"].includes(parts[1])) {
      return { platform: "soundcloud", type: "profile" };
    }
    return { platform: "soundcloud", type: parts.length >= 2 ? "track" : null };
  }
  if (host === "open.spotify.com") {
    const m = path.match(/^\/(?:intl-[a-z-]+\/)?(track|album|playlist)\//);
    return { platform: "spotify", type: m ? m[1] : null };
  }
  return null;
}

if (typeof module !== "undefined") module.exports = { extractUrls, detectLocal };
