"""Video downloader web app for YouTube videos/Shorts and Instagram Reels."""

import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

# Only these hosts may be fetched. Prevents the download endpoint from being
# used as an open proxy / SSRF vector against arbitrary URLs.
ALLOWED_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "instagram.com",
    "instagr.am",
    "ddinstagram.com",
}

# Optional Netscape-format cookie file, for login-walled Instagram content.
COOKIE_FILE = os.environ.get("DOWNLOADER_COOKIES") or None

# Easier alternative: pull cookies straight from an installed browser profile,
# e.g. DOWNLOADER_BROWSER=chrome (or firefox / edge / brave).
# Instagram now returns empty responses to anonymous requests for most posts,
# so one of these two is effectively required for Instagram links.
COOKIE_BROWSER = (os.environ.get("DOWNLOADER_BROWSER") or "").strip().lower() or None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_ffmpeg():
    """Locate ffmpeg, preferring a system install, falling back to the copy
    that ships with the imageio-ffmpeg wheel.

    YouTube no longer offers pre-muxed (video+audio) streams for most videos,
    so ffmpeg is required to produce a playable file - not merely a nicety.
    yt-dlp identifies the binary by filename, so the bundled build (which has a
    versioned name) is copied to bin/ffmpeg.exe once.
    """
    system = shutil.which("ffmpeg")
    if system:
        return os.path.dirname(system)

    try:
        import imageio_ffmpeg
    except ImportError:
        return None

    try:
        source = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - binary missing for this platform
        return None

    bin_dir = os.path.join(BASE_DIR, "bin")
    target = os.path.join(bin_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.isfile(target):
        os.makedirs(bin_dir, exist_ok=True)
        shutil.copy2(source, target)
        if os.name != "nt":
            os.chmod(target, 0o755)
    return bin_dir


FFMPEG_DIR = resolve_ffmpeg()
HAS_FFMPEG = FFMPEG_DIR is not None

MAX_TITLE_LEN = 120


def _base_domain(host: str) -> str:
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def classify_url(url: str):
    """Return (platform, normalized_url) or raise ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Please paste a video link.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) links are supported.")

    host = _base_domain(parsed.netloc)
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            "Unsupported site. This tool handles YouTube videos, "
            "YouTube Shorts and Instagram Reels/posts."
        )

    if "instagram" in host:
        platform = "instagram"
    else:
        platform = "youtube"
    return platform, url


def ydl_options(**extra):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": False,
        "retries": 3,
        "socket_timeout": 30,
        # YouTube serves DASH formats as many small fragments. Fetching them
        # one at a time badly underuses the connection; 16 in parallel is a
        # large speedup on anything but the slowest links.
        "concurrent_fragment_downloads": 16,
    }
    if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    elif COOKIE_BROWSER:
        # (browser, profile, keyring, container) - only the name is needed.
        opts["cookiesfrombrowser"] = (COOKIE_BROWSER, None, None, None)
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    opts.update(extra)
    return opts


def human_size(num):
    if not num:
        return None
    units = ["B", "KB", "MB", "GB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None


def format_duration(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_rank(fmt):
    """Rank competing streams of the same resolution, best last.

    H.264 is preferred over VP9/AV1: it is more widely playable on phones,
    smart TVs and older desktop players, which matters more for a download
    than the modest file-size win from newer codecs.
    """
    vcodec = (fmt.get("vcodec") or "").lower()
    if vcodec.startswith(("avc", "h264")):
        codec_score = 3
    elif vcodec.startswith(("vp9", "vp09")):
        codec_score = 2
    else:  # av01 and anything unrecognised
        codec_score = 1
    return (codec_score, 1 if fmt.get("ext") == "mp4" else 0, fmt.get("tbr") or 0)


def build_format_list(info):
    """Pick a sensible, de-duplicated set of user-facing download choices."""
    formats = info.get("formats") or []
    progressive = {}   # height -> format (video+audio already muxed)
    video_only = {}    # height -> format (needs merging with audio)
    best_audio = None

    for fmt in formats:
        if fmt.get("protocol") in ("mhtml",) or fmt.get("format_note") == "storyboard":
            continue
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        size = fmt.get("filesize") or fmt.get("filesize_approx")

        if vcodec == "none" and acodec != "none":
            # Audio-only: keep the highest bitrate one.
            if best_audio is None or (fmt.get("abr") or 0) > (best_audio.get("abr") or 0):
                best_audio = fmt
            continue

        height = fmt.get("height")
        if vcodec == "none" or not height:
            continue

        bucket = progressive if acodec != "none" else video_only
        current = bucket.get(height)
        fmt = dict(fmt)
        fmt["_size"] = size
        if current is None or _format_rank(fmt) > _format_rank(current):
            bucket[height] = fmt

    choices = []
    for height in sorted(progressive, reverse=True):
        fmt = progressive[height]
        choices.append(
            {
                "format_id": fmt["format_id"],
                "label": f"{height}p",
                "ext": fmt.get("ext") or "mp4",
                "size": human_size(fmt.get("_size")),
                "kind": "video",
                "note": "video + audio",
            }
        )

    if HAS_FFMPEG:
        for height in sorted(video_only, reverse=True):
            if height in progressive:
                continue
            fmt = video_only[height]
            audio_size = None
            if best_audio:
                audio_size = best_audio.get("filesize") or best_audio.get("filesize_approx")
            total = None
            if fmt.get("_size") and audio_size:
                total = fmt["_size"] + audio_size
            choices.append(
                {
                    # yt-dlp merges these two streams for us. Prefer m4a/AAC
                    # audio so the resulting MP4 plays everywhere - Opus in an
                    # MP4 container is rejected by several common players.
                    "format_id": f"{fmt['format_id']}+bestaudio[ext=m4a]/bestaudio",
                    "label": f"{height}p",
                    "ext": "mp4",
                    "size": human_size(total),
                    "kind": "video",
                    "note": "merged (ffmpeg)",
                }
            )

    if best_audio:
        choices.append(
            {
                "format_id": best_audio["format_id"],
                "label": "Audio only",
                "ext": best_audio.get("ext") or "m4a",
                "size": human_size(
                    best_audio.get("filesize") or best_audio.get("filesize_approx")
                ),
                "kind": "audio",
                "note": f"{int(best_audio['abr'])} kbps" if best_audio.get("abr") else "audio track",
            }
        )

    if not choices:
        choices.append(
            {
                "format_id": "best",
                "label": "Best available",
                "ext": info.get("ext") or "mp4",
                "size": None,
                "kind": "video",
                "note": "automatic",
            }
        )
    return choices


def friendly_error(exc):
    text = str(exc)
    lowered = text.lower()
    if "could not copy" in lowered and "cookie database" in lowered:
        return (
            "Couldn't read the browser's cookies because the browser is "
            "running and holds the file open. Fully close it (check the tray) "
            "and retry, or export a cookies.txt and use DOWNLOADER_COOKIES."
        )
    if (
        "login" in lowered
        or "cookies" in lowered
        or "rate-limit" in lowered
        or "empty media response" in lowered
    ):
        return (
            "Instagram won't serve this post anonymously. Restart the server "
            "with DOWNLOADER_BROWSER=chrome (or firefox/edge) to reuse the "
            "cookies from a browser you're logged into - see the README."
        )
    if "private video" in lowered:
        return "That video is private."
    if "unavailable" in lowered or "does not exist" in lowered:
        return "That video is unavailable or the link is wrong."
    if "age" in lowered and "confirm" in lowered:
        return "Age-restricted video — a cookies.txt file is required."
    # Strip yt-dlp's ANSI/prefix noise for display.
    text = re.sub(r"^ERROR:\s*", "", text).strip()
    return text[:300] or "Could not read that link."


@app.route("/")
def index():
    return render_template("index.html", has_ffmpeg=HAS_FFMPEG)


@app.route("/api/info", methods=["POST"])
def api_info():
    payload = request.get_json(silent=True) or {}
    try:
        platform, url = classify_url(payload.get("url"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        with YoutubeDL(ydl_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        return jsonify({"error": friendly_error(exc)}), 502
    except Exception as exc:  # noqa: BLE001 - surface anything else cleanly
        return jsonify({"error": friendly_error(exc)}), 502

    # A link may resolve to a playlist/carousel; take the first entry.
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    is_short = platform == "youtube" and (info.get("duration") or 0) <= 60
    return jsonify(
        {
            "platform": platform,
            "kind": (
                "Instagram Reel"
                if platform == "instagram"
                else ("YouTube Short" if is_short else "YouTube Video")
            ),
            "url": url,
            "title": (info.get("title") or "Untitled")[:MAX_TITLE_LEN],
            "uploader": info.get("uploader") or info.get("channel") or "Unknown",
            "thumbnail": info.get("thumbnail"),
            "duration": format_duration(info.get("duration")),
            "formats": build_format_list(info),
        }
    )


# --------------------------------------------------------------------------
# Download jobs
#
# Fetching happens in a worker thread so the browser can poll for progress.
# Previously the request blocked until the whole file was fetched and merged,
# which left the page looking frozen for the entire download.
# --------------------------------------------------------------------------

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 30 * 60  # seconds to keep a finished job (and its file) around
SERVE_CLEANUP_DELAY = 120  # grace period after handing a file to the browser
TMP_PREFIX = "dl_"


def _set_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _reap_jobs():
    """Drop jobs (and their temp files) that nobody collected."""
    cutoff = time.time() - JOB_TTL
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items() if j["updated"] < cutoff]
        for jid in stale:
            job = JOBS.pop(jid)
            shutil.rmtree(job["tmpdir"], ignore_errors=True)


def _reaper_loop():
    """Periodic sweep, so abandoned downloads can't fill the temp directory."""
    while True:
        time.sleep(60)
        try:
            _reap_jobs()
        except Exception:  # noqa: BLE001 - a housekeeping thread must not die
            pass


def sweep_orphaned_tmpdirs(max_age=6 * 3600):
    """Remove download temp dirs left behind by a previous run that crashed.

    Only long-untouched directories are touched, so a second instance of the
    app running concurrently won't have its in-progress work deleted.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(TMP_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _run_job(job_id, url, format_id):
    tmpdir = JOBS[job_id]["tmpdir"]
    # A merged selector ('137+bestaudio') downloads two streams in sequence,
    # each reporting its own 0-100%. Accumulate bytes across them so the bar
    # reflects the whole job instead of restarting for the audio track.
    total_streams = format_id.split("/")[0].count("+") + 1
    state = {"done": 0, "bytes_done": 0}

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            overall = None
            if total:
                # While earlier streams are outstanding the grand total is only
                # known approximately - the audio size isn't visible until its
                # download starts - so the video phase is capped below 100.
                grand = state["bytes_done"] + total
                overall = (state["bytes_done"] + done) / grand * 100
                if state["done"] < total_streams - 1:
                    overall = min(overall, 95.0)
            _set_job(
                job_id,
                state="downloading",
                percent=round(overall, 1) if overall is not None else None,
                stream=min(state["done"] + 1, total_streams),
                streams=total_streams,
                downloaded=state["bytes_done"] + done,
                total=(state["bytes_done"] + total) if total else None,
                speed=d.get("speed"),
                eta=d.get("eta"),
                updated=time.time(),
            )
        elif status == "finished":
            state["done"] += 1
            state["bytes_done"] += d.get("downloaded_bytes") or 0
            if state["done"] >= total_streams:
                # Everything fetched; ffmpeg may still need to mux.
                _set_job(job_id, state="processing", percent=100, updated=time.time())

    def postprocessor_hook(d):
        if d.get("status") == "started":
            _set_job(job_id, state="merging", updated=time.time())

    opts = ydl_options(
        format=format_id,
        outtmpl=os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        restrictfilenames=True,
        merge_output_format="mp4" if "+" in format_id else None,
        progress_hooks=[progress_hook],
        postprocessor_hooks=[postprocessor_hook],
    )
    opts = {k: v for k, v in opts.items() if v is not None}

    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [
            os.path.join(tmpdir, name)
            for name in os.listdir(tmpdir)
            if os.path.isfile(os.path.join(tmpdir, name))
        ]
        if not files:
            raise RuntimeError("Download produced no file.")
        path = max(files, key=os.path.getsize)
        _set_job(
            job_id,
            state="ready",
            percent=100,
            path=path,
            filename=os.path.basename(path),
            size=os.path.getsize(path),
            updated=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 - report anything back to the UI
        _set_job(job_id, state="error", error=friendly_error(exc), updated=time.time())


@app.route("/api/download/start", methods=["POST"])
def api_download_start():
    payload = request.get_json(silent=True) or {}
    try:
        _, url = classify_url(payload.get("url"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    format_id = (payload.get("format_id") or "best").strip()
    # Only ids, '+' merges, '/' fallbacks and simple [key=value] filters are
    # allowed - this is a yt-dlp selector string, never a shell argument.
    if not re.fullmatch(r"[A-Za-z0-9_\-\.+/\[\]=]{1,150}", format_id):
        return jsonify({"error": "Invalid format selection."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "starting",
            "percent": None,
            "downloaded": 0,
            "total": None,
            "speed": None,
            "eta": None,
            "stream": 1,
            "streams": format_id.split("/")[0].count("+") + 1,
            "path": None,
            "filename": None,
            "size": None,
            "error": None,
            "tmpdir": tempfile.mkdtemp(prefix="dl_"),
            "updated": time.time(),
        }

    threading.Thread(
        target=_run_job, args=(job_id, url, format_id), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/download/progress/<job_id>")
def api_download_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown or expired download."}), 404
        snapshot = {
            k: job[k]
            for k in (
                "state",
                "percent",
                "speed",
                "eta",
                "error",
                "size",
                "filename",
                "downloaded",
                "total",
                "stream",
                "streams",
            )
        }
    return jsonify(snapshot)


@app.route("/api/download/file/<job_id>")
def api_download_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown or expired download."}), 404
        if job["state"] != "ready":
            return jsonify({"error": "This download isn't finished yet."}), 409
        path, filename, tmpdir = job["path"], job["filename"], job["tmpdir"]

    response = send_file(path, as_attachment=True, download_name=filename)

    # Flask's response.call_on_close does not fire reliably for send_file
    # responses under the dev server's file wrapper, so cleanup is scheduled
    # on a timer instead. The delay lets the transfer finish and releases the
    # file handle, which Windows requires before the directory can be removed.
    def _cleanup():
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        shutil.rmtree(tmpdir, ignore_errors=True)

    threading.Timer(SERVE_CLEANUP_DELAY, _cleanup).start()
    return response


if __name__ == "__main__":
    if HAS_FFMPEG:
        print(f"[ok] ffmpeg: {FFMPEG_DIR}")
    else:
        print(
            "[warn] ffmpeg not found - video downloads will be unavailable.\n"
            "       Run: pip install imageio-ffmpeg   (or install ffmpeg on PATH)"
        )

    orphans = sweep_orphaned_tmpdirs()
    if orphans:
        print(f"[ok] cleaned {orphans} leftover temp folder(s) from earlier runs")
    threading.Thread(target=_reaper_loop, daemon=True).start()

    print("Serving on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
