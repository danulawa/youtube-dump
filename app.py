import os
import re
import uuid
import shutil
import tempfile
import threading
import subprocess
from urllib.parse import quote

from flask import Flask, Response, render_template, request, jsonify
from yt_dlp import YoutubeDL
from yt_dlp.utils import UnsupportedError, download_range_func

app = Flask(__name__)

# ffmpeg binary, overridable via env var
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

# trim cuts: False = fast keyframe-aligned copy; True = frame-exact (re-encodes)
FRAME_EXACT_TRIM = True

# use the (throttled) section download only when a clip is under this fraction
# of the video; bigger slices download the whole thing fast and trim locally
SECTION_MAX_FRACTION = 0.30

NO_VIDEO_MSG = "Error 404: No Video File Found"

# in-progress / finished jobs, keyed by id
JOBS = {}

# error text that means the link isn't a video
NO_VIDEO_SIGNALS = (
    "unsupported url",
    "is not a valid url",
    "not a valid url",
    "video unavailable",
    "this video is unavailable",
    "this video is not available",
    "private video",
    "video has been removed",
    "removed by the uploader",
    "does not exist",
    "incomplete youtube id",
    "no video formats found",
    "requested format is not available",
)


def is_no_video_error(exc):
    # true when the link has no real video
    if isinstance(exc, UnsupportedError):
        return True
    msg = str(exc).lower()
    return any(sig in msg for sig in NO_VIDEO_SIGNALS)


def parse_hms(value):
    # "HH:MM:SS" / "MM:SS" / "SS" -> seconds
    value = (value or "").strip()
    if not value:
        return 0
    parts = value.split(":")
    if not all(p.isdigit() for p in parts):
        raise ValueError(f"bad time: {value!r}")
    secs = 0
    for n in (int(p) for p in parts):
        secs = secs * 60 + n
    return secs


def safe_filename(name):
    # clean a title into a filename base
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80] or "clip"


def content_disposition(base):
    # HTTP-safe filename header (ASCII + RFC 5987)
    ascii_base = base.encode("ascii", "ignore").decode("ascii").strip()
    ascii_base = re.sub(r"\s+", "_", ascii_base) or "clip"
    return (
        f'attachment; filename="{ascii_base}.mp4"; '
        f"filename*=UTF-8''{quote(f'{base}.mp4')}"
    )


def build_qualities(formats):
    # group video streams by actual height (works for any aspect ratio)
    audio_sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    audio_size = max(audio_sizes) if audio_sizes else 0

    by_height = {}
    for f in formats:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        by_height.setdefault(f["height"], []).append(f)

    qualities = []
    for h in sorted(by_height):
        vids = by_height[h]
        h264 = [f for f in vids if (f.get("vcodec") or "").startswith("avc1")]
        chosen = h264 or vids
        vid_size = max((f.get("filesize") or f.get("filesize_approx") or 0) for f in chosen)
        label = next(
            (f"{m.group(1)}p" for f in vids if (m := re.search(r"(\d+)p", f.get("format_note") or ""))),
            f"{h}p",
        )
        qualities.append({
            "height": h,
            "label": label,
            "filesize_full": (vid_size + audio_size) or None,
            "h264": bool(h264),
        })
    return qualities


def ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "remote_components": ["ejs:github"],  # fetch the n-challenge solver
    }
    if extra:
        opts.update(extra)
    return opts


def extract(url, fmt=None):
    extra = {"skip_download": True}
    if fmt:
        extra["format"] = fmt
    with YoutubeDL(ydl_opts(extra)) as ydl:
        return ydl.extract_info(url, download=False)


def output_path(info, workdir):
    # the file yt-dlp actually produced
    rd = info.get("requested_downloads")
    if rd and rd[0].get("filepath"):
        return rd[0]["filepath"]
    files = [os.path.join(workdir, f) for f in os.listdir(workdir) if f.endswith(".mp4")]
    return max(files, key=os.path.getmtime) if files else None


def local_trim(src, clip, start, dur):
    # cut the already-downloaded file (disk read, no throttle -> fast)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", src, "-t", str(dur)]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
            if FRAME_EXACT_TRIM else ["-c", "copy"])
    cmd += ["-movflags", "+faststart", clip]
    proc = subprocess.run(cmd, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:400] or "ffmpeg trim failed")


def run_job(job_id, url, fmt, trimming, start, end, duration):
    job = JOBS[job_id]
    workdir = tempfile.mkdtemp(prefix="ytdump_")
    job["workdir"] = workdir
    try:
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job["stage"] = "downloading"
                    job["percent"] = min(99.0, (d.get("downloaded_bytes") or 0) / total * 100)

        def pp_hook(d):
            if d.get("status") == "started":
                job["stage"] = "encoding"

        clip_dur = end - start if trimming else 0
        # small clip of a long video -> fetch only the section (saves data)
        use_section = trimming and (not duration or clip_dur < SECTION_MAX_FRACTION * duration)

        dl = {
            "format": fmt,
            "outtmpl": os.path.join(workdir, "out.%(ext)s"),
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 5,
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
        }
        if use_section:
            dl["download_ranges"] = download_range_func(None, [(start, end)])
            if FRAME_EXACT_TRIM:
                dl["force_keyframes_at_cuts"] = True

        with YoutubeDL(ydl_opts(dl)) as ydl:
            info = ydl.extract_info(url, download=True)

        path = output_path(info, workdir)
        if not path or not os.path.exists(path):
            raise RuntimeError(NO_VIDEO_MSG)

        # full download of a trim -> cut locally (fast, no throttle)
        if trimming and not use_section:
            job["stage"] = "encoding"
            clip = os.path.join(workdir, "clip.mp4")
            local_trim(path, clip, start, clip_dur)
            path = clip

        title = safe_filename(info.get("title", "clip"))
        job["base"] = f"{title}_{start}-{end}" if trimming else title
        job["path"] = path
        job["percent"] = 100.0
        job["stage"] = "done"
        job["status"] = "done"
    except Exception as e:
        job["error"] = NO_VIDEO_MSG if is_no_video_error(e) else str(e)
        job["status"] = "error"
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Please paste a YouTube link."), 400

    try:
        info = extract(url)
    except Exception as e:
        if is_no_video_error(e):
            return jsonify(error=NO_VIDEO_MSG), 404
        return jsonify(error=f"Couldn't read that video: {e}"), 400

    # playlist/channel or no streams -> not a video
    formats = info.get("formats", [])
    if info.get("entries") is not None or not formats:
        return jsonify(error=NO_VIDEO_MSG), 404

    qualities = build_qualities(formats)
    if not qualities:
        return jsonify(error=NO_VIDEO_MSG), 404

    return jsonify(
        title=info.get("title", "video"),
        duration=info.get("duration") or 0,
        qualities=qualities,
    )


@app.route("/download", methods=["POST"])
def download():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode", "full")
    try:
        start = parse_hms(data.get("start", ""))
        end = parse_hms(data.get("end", ""))
        height_n = int(str(data.get("height", "")).strip())
        duration = int(float(data.get("duration") or 0))
    except (ValueError, TypeError):
        return jsonify(error="Invalid parameters."), 400

    trimming = mode == "trim"
    if trimming and end <= start:
        return jsonify(error="End time must be after start time."), 400

    fmt = (
        f"bestvideo[height={height_n}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height={height_n}]+bestaudio/"
        f"bestvideo[height<={height_n}]+bestaudio/best[height<={height_n}]/best"
    )

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "stage": "downloading", "percent": 0.0,
                    "error": None, "path": None, "base": None, "workdir": None}
    threading.Thread(
        target=run_job, args=(job_id, url, fmt, trimming, start, end, duration), daemon=True
    ).start()
    return jsonify(job_id=job_id)


@app.route("/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(status="error", error="Unknown job."), 404
    return jsonify(
        status=job["status"],
        stage=job["stage"],
        percent=round(job["percent"], 1),
        error=job["error"],
    )


@app.route("/file/<job_id>")
def file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("path"):
        return "Not ready.", 404
    path, workdir, base = job["path"], job["workdir"], job["base"]

    def generate():
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            JOBS.pop(job_id, None)

    return Response(
        generate(),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": content_disposition(base),
            "Content-Length": str(os.path.getsize(path)),
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
