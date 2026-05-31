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
from yt_dlp.utils import UnsupportedError

app = Flask(__name__)

# ffmpeg binary, overridable via env var
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

# trim cuts: False = fast keyframe-aligned copy; True = frame-exact (re-encodes)
FRAME_EXACT_TRIM = True

# clip >= this fraction of the video -> full fast download + local trim;
# smaller clips stream just the section (less data, but YouTube-throttled)
SECTION_MAX_FRACTION = 0.70

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


def out_time_to_seconds(t):
    # ffmpeg -progress "HH:MM:SS.micro" -> seconds
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None


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


def headers_to_ffmpeg(http_headers):
    # yt-dlp header dict -> ffmpeg -headers string
    return "".join(f"{k}: {v}\r\n" for k, v in (http_headers or {}).items())


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


def run_ffmpeg(cmd, total_dur, job):
    # run an ffmpeg cmd (with -progress pipe:1) and feed time-based percent into the job
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time=") and total_dur > 0:
            secs = out_time_to_seconds(line.split("=", 1)[1])
            if secs is not None:
                job["percent"] = min(99.0, secs / total_dur * 100)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.read() or "")[:400] or "ffmpeg failed")


def section_download(inputs, start, dur, out, job):
    # download ONLY the section via ffmpeg, with smooth time-based progress
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats"]
    for f in inputs:
        hdr = headers_to_ffmpeg(f.get("http_headers"))
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-ss", str(start), "-i", f["url"]]
    if len(inputs) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-t", str(dur)]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
            if FRAME_EXACT_TRIM else ["-c", "copy"])
    cmd += ["-progress", "pipe:1", out]
    run_ffmpeg(cmd, dur, job)


def local_trim(src, clip, start, dur, job):
    # cut the downloaded file (disk read -> fast); no faststart yet
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
           "-ss", str(start), "-i", src, "-t", str(dur)]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
            if FRAME_EXACT_TRIM else ["-c", "copy"])
    cmd += ["-progress", "pipe:1", clip]
    run_ffmpeg(cmd, dur, job)


def finalize(src, dst, dur, job):
    # quick remux to put the index up front (streamable / instantly seekable)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
           "-i", src, "-c", "copy", "-movflags", "+faststart",
           "-progress", "pipe:1", dst]
    run_ffmpeg(cmd, dur, job)


def run_job(job_id, url, fmt, trimming, start, end, duration):
    job = JOBS[job_id]
    workdir = tempfile.mkdtemp(prefix="ytdump_")
    job["workdir"] = workdir
    try:
        clip_dur = end - start if trimming else 0
        # small clip -> stream the section; clip >= 70% -> full download + local trim
        use_section = trimming and (not duration or clip_dur < SECTION_MAX_FRACTION * duration)

        if use_section:
            info = extract(url, fmt)  # resolve stream URLs + headers
            if info.get("entries") is not None or not info.get("formats"):
                raise RuntimeError(NO_VIDEO_MSG)
            title = safe_filename(info.get("title", "clip"))
            inputs = info.get("requested_formats") or [info]

            job["stage"] = "downloading"; job["percent"] = 0.0
            raw = os.path.join(workdir, "raw.mp4")
            section_download(inputs, start, clip_dur, raw, job)

            job["stage"] = "finalizing"; job["percent"] = 0.0
            final_path = os.path.join(workdir, "final.mp4")
            finalize(raw, final_path, clip_dur, job)
            base = f"{title}_{start}-{end}"
        else:
            def hook(d):
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if total:
                        job["stage"] = "downloading"
                        job["percent"] = min(99.0, (d.get("downloaded_bytes") or 0) / total * 100)

            dl = {
                "format": fmt,
                "outtmpl": os.path.join(workdir, "out.%(ext)s"),
                "merge_output_format": "mp4",
                "concurrent_fragment_downloads": 5,
                "progress_hooks": [hook],
            }
            job["stage"] = "downloading"; job["percent"] = 0.0
            with YoutubeDL(ydl_opts(dl)) as ydl:
                info = ydl.extract_info(url, download=True)

            src = output_path(info, workdir)
            if not src or not os.path.exists(src):
                raise RuntimeError(NO_VIDEO_MSG)
            title = safe_filename(info.get("title", "clip"))

            if not trimming:
                final_path = src
                base = title
            else:
                job["stage"] = "trimming"; job["percent"] = 0.0
                clip = os.path.join(workdir, "clip.mp4")
                local_trim(src, clip, start, clip_dur, job)
                job["stage"] = "finalizing"; job["percent"] = 0.0
                final_path = os.path.join(workdir, "final.mp4")
                finalize(clip, final_path, clip_dur, job)
                base = f"{title}_{start}-{end}"

        job["base"] = base
        job["path"] = final_path
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