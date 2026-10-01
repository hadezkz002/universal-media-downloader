"""yt-dlp CLI entry point with one small fix applied.

SoundCloud sets only embed full objects for their first few tracks; yt-dlp then emits bare
api-v2 URLs without titles for the rest (it batch-fetches them only for private sets). We batch-fetch
them for every set through yt-dlp's own API helper, so flat playlist listings carry title, artist,
duration, artwork, permalink and the track's access policy (one request per 50 tracks).

ponytail: relies on yt-dlp internals (_extract_set/_call_api); any failure falls back to stock behaviour.
Drop this file once upstream lists set tracks with metadata.
"""

import yt_dlp
from yt_dlp.extractor import soundcloud as sc

_original_extract_set = sc.SoundcloudPlaylistBaseIE._extract_set


def _extract_set(self, playlist, token=None):
    by_id = {}
    try:
        tracks = playlist.get("tracks") or []
        by_id = {t["id"]: t for t in tracks if t.get("id") and t.get("permalink_url")}
        missing = [t["id"] for t in tracks if t.get("id") and t["id"] not in by_id]
        for start in range(0, len(missing), 50):
            query = {"ids": ",".join(map(str, missing[start:start + 50])), "playlistId": str(playlist["id"])}
            if token:
                query["playlistSecretToken"] = token
            for t in self._call_api(self._API_V2_BASE + "tracks", str(playlist["id"]), "Downloading track metadata",
                                    query=query, headers=self._HEADERS) or []:
                if t.get("id"):
                    by_id[t["id"]] = t
        playlist = {**playlist, "tracks": [by_id.get(t.get("id"), t) for t in tracks]}
    except Exception as exc:  # never break extraction because of the enrichment
        self.report_warning(f"SoundCloud track metadata enrichment failed: {exc}")
    result = _original_extract_set(self, playlist, token)
    for entry in result.get("entries") or []:
        track = by_id.get(int(entry["id"])) if str(entry.get("id") or "").isdigit() else None
        if not track:
            continue
        # Read fields directly: _extract_info_dict would probe every artwork URL over the network.
        meta = {
            "title": track.get("title"),
            "uploader": (track.get("publisher_metadata") or {}).get("artist") or (track.get("user") or {}).get("username"),
            "duration": track["duration"] / 1000 if isinstance(track.get("duration"), (int, float)) else None,
            "thumbnail": track.get("artwork_url") or (track.get("user") or {}).get("avatar_url"),
        }
        entry.update({k: v for k, v in meta.items() if v})
        entry["sc_policy"] = track.get("policy")
    return result


sc.SoundcloudPlaylistBaseIE._extract_set = _extract_set

if __name__ == "__main__":
    yt_dlp.main()
