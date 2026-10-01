const test = require("node:test");
const assert = require("node:assert");
const { extractUrls, detectLocal } = require("../frontend/detect.js");

const SHORT = "https://on.soundcloud.com/7Zj8Us1gsTJhqOWXvl";

test("extracts URL from share text, quotes, punctuation and whitespace", () => {
  assert.deepStrictEqual(extractUrls(`Listen to playlist:\n${SHORT}\nthanks`), [SHORT]);
  assert.deepStrictEqual(extractUrls(`abc ${SHORT} xyz`), [SHORT]);
  assert.deepStrictEqual(extractUrls(`   ${SHORT}   `), [SHORT]);
  assert.deepStrictEqual(extractUrls(`"${SHORT}".`), [SHORT]);
  assert.deepStrictEqual(extractUrls(`(${SHORT}),`), [SHORT]);
  assert.deepStrictEqual(extractUrls("see https://en.wikipedia.org/wiki/Foo_(bar)"), ["https://en.wikipedia.org/wiki/Foo_(bar)"]);
  assert.deepStrictEqual(extractUrls("no url here"), []);
  assert.deepStrictEqual(extractUrls("javascript:alert(1) ftp://x.y"), []);
});

test("multiple URLs keep order", () => {
  assert.deepStrictEqual(extractUrls(`${SHORT} and https://youtu.be/abc`), [SHORT, "https://youtu.be/abc"]);
});

test("keeps YouTube list= parameter", () => {
  assert.deepStrictEqual(extractUrls("https://www.youtube.com/watch?v=a&list=PL123."), ["https://www.youtube.com/watch?v=a&list=PL123"]);
});

test("detects platform and type", () => {
  const cases = [
    ["https://www.youtube.com/watch?v=a", "youtube", "video"],
    ["https://youtu.be/a", "youtube", "video"],
    ["https://youtube.com/shorts/a", "youtube", "shorts"],
    ["https://www.youtube.com/playlist?list=PL1", "youtube", "playlist"],
    ["https://www.youtube.com/watch?v=a&list=PL1", "youtube", "playlist"],
    ["https://m.youtube.com/@chan", "youtube", "channel"],
    ["https://soundcloud.com/a/sets/b", "soundcloud", "playlist"],
    ["https://m.soundcloud.com/a/b", "soundcloud", "track"],
    ["https://soundcloud.com/a", "soundcloud", "profile"],
    [SHORT, "soundcloud", null],
    ["https://open.spotify.com/track/x", "spotify", "track"],
    ["https://open.spotify.com/intl-vi/album/x", "spotify", "album"],
    ["https://open.spotify.com/playlist/x", "spotify", "playlist"],
  ];
  for (const [url, platform, type] of cases) {
    const d = detectLocal(url);
    assert.strictEqual(d.platform, platform, url);
    assert.strictEqual(d.type, type, url);
  }
  assert.strictEqual(detectLocal(SHORT).shortLink, true);
  assert.strictEqual(detectLocal("https://example.com/x"), null);
});
