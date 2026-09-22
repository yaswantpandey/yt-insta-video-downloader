// Round-trip test for the localStorage persistence added to static/app.js.
// Loads the real page markup and the real script in a jsdom window, fetches a
// video via a stubbed /api/info, then reloads the page and asserts the card
// comes back. Run: node test_persist.js
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs
  .readFileSync(path.join(__dirname, "templates", "index.html"), "utf8")
  // strip Jinja bits that a browser would never see
  .replace(/\{%[\s\S]*?%\}/g, "")
  .replace(/\{\{[^}]*\}\}/g, "");
const JS = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

const SAMPLE = {
  platform: "youtube",
  kind: "YouTube Video",
  url: "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
  title: "Big Buck Bunny",
  uploader: "Blender",
  thumbnail: "https://example.invalid/t.jpg",
  duration: "10:35",
  formats: [
    { format_id: "628+bestaudio", label: "2160p", ext: "mp4", size: null, kind: "video", note: "merged" },
    { format_id: "312+bestaudio", label: "1080p", ext: "mp4", size: null, kind: "video", note: "merged" },
    { format_id: "140", label: "Audio only", ext: "m4a", size: "9.8 MB", kind: "audio", note: "129 kbps" },
  ],
};

// A localStorage that survives across the two page loads, like a real browser.
function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    get size() {
      return map.size;
    },
  };
}

function loadPage(storage) {
  const dom = new JSDOM(HTML, { runScripts: "outside-only", url: "http://127.0.0.1:5000/" });
  const win = dom.window;
  Object.defineProperty(win, "localStorage", { value: storage, configurable: true });
  win.fetch = async (url, opts) => ({
    ok: true,
    json: async () => {
      if (String(url).includes("/api/info")) return SAMPLE;
      throw new Error("unexpected fetch: " + url);
    },
  });
  win.eval(JS);
  return win;
}

function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL: " + msg);
    process.exitCode = 1;
  } else {
    console.log("pass: " + msg);
  }
}

(async () => {
  const storage = makeStorage();

  // --- first load: nothing stored, card hidden
  let win = loadPage(storage);
  assert(win.document.getElementById("result").hidden === true, "card hidden on a fresh page");

  // --- fetch a video
  win.document.getElementById("url").value = SAMPLE.url;
  win.document.getElementById("form").dispatchEvent(new win.Event("submit"));
  await new Promise((r) => setTimeout(r, 50));

  const doc1 = win.document;
  assert(doc1.getElementById("result").hidden === false, "card shown after fetch");
  assert(doc1.getElementById("title").textContent === "Big Buck Bunny", "title rendered");
  assert(doc1.getElementById("quality").options.length === 3, "3 quality options");
  assert(storage.size === 1, "state written to localStorage");

  // pick a non-default quality so we can check it is remembered
  const sel = doc1.getElementById("quality");
  sel.value = "140";
  sel.dispatchEvent(new win.Event("change"));

  // --- simulate F5: brand new window, same storage
  win = loadPage(storage);
  const doc2 = win.document;
  assert(doc2.getElementById("result").hidden === false, "card RESTORED after refresh");
  assert(doc2.getElementById("title").textContent === "Big Buck Bunny", "title restored");
  assert(doc2.getElementById("uploader").textContent === "Blender", "uploader restored");
  assert(doc2.getElementById("badge").textContent === "YouTube Video", "badge restored");
  assert(doc2.getElementById("quality").options.length === 3, "quality list restored");
  assert(doc2.getElementById("quality").value === "140", "chosen quality remembered");
  assert(doc2.getElementById("url").value === SAMPLE.url, "url box repopulated");

  // --- Clear button wipes it
  doc2.getElementById("clear").dispatchEvent(new win.Event("click"));
  assert(doc2.getElementById("result").hidden === true, "card hidden after Clear");
  assert(storage.size === 0, "storage emptied by Clear");

  win = loadPage(storage);
  assert(win.document.getElementById("result").hidden === true, "stays cleared after refresh");

  // --- expired entry is discarded
  const stale = makeStorage();
  stale.setItem(
    "downloader:last",
    JSON.stringify({ v: 1, at: Date.now() - 13 * 60 * 60 * 1000, data: SAMPLE, selected: "140" })
  );
  win = loadPage(stale);
  assert(win.document.getElementById("result").hidden === true, "expired entry ignored");

  // --- corrupt entry must not break the page
  const bad = makeStorage();
  bad.setItem("downloader:last", "{not json");
  win = loadPage(bad);
  assert(win.document.getElementById("result").hidden === true, "corrupt entry ignored, page still works");
  assert(bad.size === 0, "corrupt entry removed");
})();
