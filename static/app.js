const form = document.getElementById("form");
const urlInput = document.getElementById("url");
const goBtn = document.getElementById("go");
const statusEl = document.getElementById("status");
const result = document.getElementById("result");
const thumb = document.getElementById("thumb");
const badge = document.getElementById("badge");
const titleEl = document.getElementById("title");
const uploaderEl = document.getElementById("uploader");
const durationEl = document.getElementById("duration");
const quality = document.getElementById("quality");
const downloadBtn = document.getElementById("download");
const clearBtn = document.getElementById("clear");
const progress = document.getElementById("progress");
const fill = document.getElementById("fill");
const pstate = document.getElementById("pstate");
const pstats = document.getElementById("pstats");

let current = null;
let polling = null;
let restoring = false;

// The fetched video is kept in localStorage so a page reload doesn't throw
// away the result and force the link to be pasted again.
const STORE_KEY = "downloader:last";
const STORE_TTL = 12 * 60 * 60 * 1000; // stream URLs go stale; 12h is plenty

function saveState(data, selected) {
  try {
    localStorage.setItem(
      STORE_KEY,
      JSON.stringify({ v: 1, at: Date.now(), data, selected })
    );
  } catch (err) {
    /* private mode or quota - persistence is a nicety, never fatal */
  }
}

function loadState() {
  let raw;
  try {
    raw = localStorage.getItem(STORE_KEY);
  } catch (err) {
    return null;
  }
  if (!raw) return null;
  try {
    const saved = JSON.parse(raw);
    if (saved.v !== 1 || !saved.data) return null;
    if (Date.now() - saved.at > STORE_TTL) {
      clearState();
      return null;
    }
    return saved;
  } catch (err) {
    clearState();
    return null;
  }
}

function clearState() {
  try {
    localStorage.removeItem(STORE_KEY);
  } catch (err) {
    /* ignore */
  }
}

function setStatus(message, kind) {
  if (!message) {
    statusEl.hidden = true;
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  goBtn.disabled = true;
  goBtn.textContent = "Fetching…";
  result.hidden = true;
  setStatus("Reading the link…");

  try {
    const response = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed.");
    render(data);
    setStatus(null);
  } catch (err) {
    setStatus(err.message, "err");
  } finally {
    goBtn.disabled = false;
    goBtn.textContent = "Fetch";
  }
});

function render(data) {
  current = data;
  titleEl.textContent = data.title;
  uploaderEl.textContent = data.uploader;
  durationEl.textContent = data.duration || "";
  badge.textContent = data.kind;

  if (data.thumbnail) {
    thumb.src = data.thumbnail;
    thumb.hidden = false;
  } else {
    thumb.removeAttribute("src");
    thumb.hidden = true;
  }

  quality.innerHTML = "";
  for (const fmt of data.formats) {
    const option = document.createElement("option");
    option.value = fmt.format_id;
    const parts = [fmt.label, "." + fmt.ext];
    if (fmt.size) parts.push("· " + fmt.size);
    if (fmt.note) parts.push("· " + fmt.note);
    option.textContent = parts.join(" ");
    quality.appendChild(option);
  }

  result.hidden = false;
  // Don't re-save while restoring, or the 12h expiry would reset on every
  // page load and the entry would never age out.
  if (!restoring) saveState(data, quality.value);
}

// Remember the chosen quality too, so a reload keeps the same selection.
quality.addEventListener("change", () => {
  if (current) saveState(current, quality.value);
});

clearBtn.addEventListener("click", () => {
  if (polling) return; // don't discard a running download
  clearState();
  current = null;
  result.hidden = true;
  urlInput.value = "";
  setStatus(null);
  urlInput.focus();
});

// Restore the last fetched video on page load.
(function restore() {
  const saved = loadState();
  if (!saved) return;
  restoring = true;
  try {
    render(saved.data);
    urlInput.value = saved.data.url || "";
    if (saved.selected) {
      const match = Array.from(quality.options).find(
        (o) => o.value === saved.selected
      );
      if (match) quality.value = saved.selected;
    }
  } finally {
    restoring = false;
  }
})();

downloadBtn.addEventListener("click", async () => {
  if (!current || polling) return;

  downloadBtn.disabled = true;
  downloadBtn.textContent = "Downloading…";
  clearBtn.disabled = true;
  setStatus(null);
  showProgress(null, "Starting…", "");

  let jobId;
  try {
    const response = await fetch("/api/download/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: current.url, format_id: quality.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start download.");
    jobId = data.job_id;
  } catch (err) {
    finishDownload();
    setStatus(err.message, "err");
    return;
  }

  polling = setInterval(() => poll(jobId), 500);
  poll(jobId);
});

async function poll(jobId) {
  let data;
  try {
    const response = await fetch(`/api/download/progress/${jobId}`);
    data = await response.json();
    if (!response.ok) throw new Error(data.error || "Lost track of the download.");
  } catch (err) {
    finishDownload();
    setStatus(err.message, "err");
    return;
  }

  if (data.state === "downloading") {
    const stats = [];
    if (data.total) stats.push(`${bytes(data.downloaded)} / ${bytes(data.total)}`);
    if (data.speed) stats.push(`${bytes(data.speed)}/s`);
    if (data.eta) stats.push(`${data.eta}s left`);
    const pct = data.percent;
    // With a merged format the audio stream is fetched after the video one.
    const what =
      data.streams > 1 ? (data.stream === 1 ? "video" : "audio") : "";
    let label =
      pct !== null ? `Downloading ${pct.toFixed(1)}%` : "Downloading…";
    if (what) label += ` (${what})`;
    showProgress(pct, label, stats.join("  ·  "));
  } else if (data.state === "processing" || data.state === "merging") {
    // No meaningful percentage while ffmpeg muxes the streams together.
    showProgress(null, "Merging video and audio…", "");
  } else if (data.state === "ready") {
    finishDownload();
    setStatus(`Done — ${bytes(data.size)}. Saving to your device…`, "ok");
    window.location.href = `/api/download/file/${jobId}`;
  } else if (data.state === "error") {
    finishDownload();
    setStatus(data.error || "Download failed.", "err");
  }
}

function showProgress(percent, label, stats) {
  progress.hidden = false;
  pstate.textContent = label;
  pstats.textContent = stats || "";
  if (percent === null || percent === undefined) {
    fill.classList.add("pulse");
  } else {
    fill.classList.remove("pulse");
    fill.style.width = percent + "%";
  }
}

function finishDownload() {
  if (polling) {
    clearInterval(polling);
    polling = null;
  }
  progress.hidden = true;
  fill.classList.remove("pulse");
  fill.style.width = "0";
  downloadBtn.disabled = false;
  downloadBtn.textContent = "Download";
  clearBtn.disabled = false;
}

function bytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}
