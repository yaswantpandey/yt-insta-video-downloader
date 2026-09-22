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
const progress = document.getElementById("progress");
const fill = document.getElementById("fill");
const pstate = document.getElementById("pstate");
const pstats = document.getElementById("pstats");

let current = null;
let polling = null;

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
}

downloadBtn.addEventListener("click", async () => {
  if (!current || polling) return;

  downloadBtn.disabled = true;
  downloadBtn.textContent = "Downloading…";
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
