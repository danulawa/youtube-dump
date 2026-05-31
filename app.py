#!/usr/bin/env python3
"""
ytclip — a minimal Flask app that streams a YouTube video (full or trimmed)
straight to the browser without ever writing a file to disk.

How "no disk" works:
    yt-dlp resolves the chosen video + audio stream URLs (and the HTTP headers
    YouTube requires). ffmpeg reads those streams, trims if requested, merges
    them, and writes a FRAGMENTED MP4 to its stdout. Flask streams that stdout
    straight into the HTTP response, so the bytes flow client-ward as they are
    produced -- nothing is buffered to a file on the server.

    Trimmed clips are re-encoded (libx264/aac) for frame-accurate cut points.
    Full videos are stream-copied (fast, exact, original codec).

Run:
    pip install -U flask yt-dlp     # and have ffmpeg available
    python app.py
    open http://127.0.0.1:5000

Notes:
    - Set FFMPEG_PATH to an explicit binary if ffmpeg isn't on PATH
      (e.g. on a host where you bundle a static build).
"""

import os
import re
import subprocess
from urllib.parse import quote

from flask import Flask, Response, render_template, request, jsonify
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError

app = Flask(__name__)

# ffmpeg binary: PATH by default, or an explicit path via env var.
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

# Common, browser-friendly heights we expose if YouTube offers them.
KNOWN_HEIGHTS = [144, 240, 360, 480, 720, 1080, 1440, 2160]

# The friendly message shown when a link doesn't point to a real video.
NO_VIDEO_MSG = "Error 404: No Video File Found"

# Substrings that indicate "this link isn't a video" (vs. a network/bot error).
# Kept deliberately narrow so genuine failures keep their real message.
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_no_video_error(exc: Exception) -> bool:
    """True if the error means 'no video at this link' rather than a transient
    network / bot-detection / geo problem (which should keep its real message)."""
    if isinstance(exc, UnsupportedError):
        return True
    msg = str(exc).lower()
    return any(sig in msg for sig in NO_VIDEO_SIGNALS)


def parse_hms(value: str) -> int:
    """'HH:MM:SS' / 'MM:SS' / 'SS' -> seconds. Empty -> 0."""
    value = (value or "").strip()
    if not value:
        return 0
    parts = value.split(":")
    if not all(p.isdigit() for p in parts):
        raise ValueError(f"bad time: {value!r}")
    nums = [int(p) for p in parts]
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def safe_filename(name: str) -> str:
    """Tidy a title for use as a filename base (may still contain Unicode)."""
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80] or "clip"


def content_disposition(base: str) -> str:
    """Build an HTTP-safe Content-Disposition for a possibly-Unicode filename.

    HTTP headers must be Latin-1, so we send a stripped ASCII `filename` as a
    fallback plus an RFC 5987 `filename*` carrying the full UTF-8 name.
    """
    ascii_base = base.encode("ascii", "ignore").decode("ascii").strip()
    ascii_base = re.sub(r"\s+", "_", ascii_base) or "clip"
    fname_ascii = f"{ascii_base}.mp4"
    fname_utf8 = quote(f"{base}.mp4")
    return f"attachment; filename=\"{fname_ascii}\"; filename*=UTF-8''{fname_utf8}"


def headers_to_ffmpeg(http_headers: dict) -> str:
    """Turn yt-dlp's header dict into ffmpeg's CRLF-joined -headers string."""
    return "".join(f"{k}: {v}\r\n" for k, v in (http_headers or {}).items())


def extract(url: str, fmt: str | None = None) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if fmt:
        opts["format"] = fmt
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def api_info():
    """Return title, duration, and available qualities (with full-size bytes)."""
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Please paste a YouTube link."), 400
    try:
        info = extract(url)
    except (DownloadError, ExtractorError, UnsupportedError) as e:
        if is_no_video_error(e):
            return jsonify(error=NO_VIDEO_MSG), 404
        return jsonify(error=f"Couldn't read that video: {e}"), 400
    except Exception as e:
        return jsonify(error=f"Couldn't read that video: {e}"), 400

    # A playlist/channel or anything with no real video -> not a video link.
    formats = info.get("formats", [])
    if info.get("entries") is not None or not formats:
        return jsonify(error=NO_VIDEO_MSG), 404

    # Best audio size (prefer m4a/aac, fall back to anything audio-only).
    audio_sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    audio_size = max(audio_sizes) if audio_sizes else 0

    # For each available height, the best video stream's size (+ audio).
    qualities = []
    for h in KNOWN_HEIGHTS:
        vids = [f for f in formats if f.get("height") == h and f.get("vcodec") not in (None, "none")]
        if not vids:
            continue
        # Prefer an H.264 (avc1) stream -> enables fast stream-copy at <=1080p.
        h264 = [f for f in vids if (f.get("vcodec") or "").startswith("avc1")]
        chosen = (h264 or vids)
        vid_size = max((f.get("filesize") or f.get("filesize_approx") or 0) for f in chosen)
        qualities.append({
            "height": h,
            "label": f"{h}p",
            "filesize_full": (vid_size + audio_size) or None,
            "h264": bool(h264),
        })

    if not qualities:
        return jsonify(error=NO_VIDEO_MSG), 404

    return jsonify(
        title=info.get("title", "video"),
        duration=info.get("duration") or 0,
        qualities=qualities,
    )


@app.route("/download", methods=["POST"])
def download():
    """Stream the (optionally trimmed) clip as a fragmented MP4. No disk write."""
    url = request.form.get("url", "").strip()
    height = request.form.get("height", "").strip()
    mode = request.form.get("mode", "full")
    try:
        start = parse_hms(request.form.get("start", ""))
        end = parse_hms(request.form.get("end", ""))
        height_n = int(height)
    except (ValueError, TypeError):
        return "Invalid parameters.", 400

    trimming = mode == "trim"
    if trimming and end <= start:
        return "End time must be after start time.", 400

    # Resolve fresh stream URLs (they expire, so never reuse from /api/info).
    fmt = (
        f"bestvideo[height={height_n}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height={height_n}]+bestaudio/"
        f"bestvideo[height<={height_n}]+bestaudio/best[height<={height_n}]/best"
    )
    try:
        info = extract(url, fmt)
    except (DownloadError, ExtractorError, UnsupportedError) as e:
        if is_no_video_error(e):
            return NO_VIDEO_MSG, 404
        return f"Couldn't resolve streams: {e}", 400
    except Exception as e:
        return f"Couldn't resolve streams: {e}", 400

    if info.get("entries") is not None or not info.get("formats"):
        return NO_VIDEO_MSG, 404

    duration = info.get("duration") or 0
    if trimming and duration and start >= duration:
        return "Start time is past the end of the video.", 400
    if trimming and duration:
        end = min(end, duration)

    title = safe_filename(info.get("title", "clip"))

    # Gather the input streams (separate video+audio, or one combined stream).
    if info.get("requested_formats"):
        inputs = info["requested_formats"]          # [video, audio]
    else:
        inputs = [info]                             # single combined stream

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    for f in inputs:
        hdr = headers_to_ffmpeg(f.get("http_headers"))
        if hdr:
            cmd += ["-headers", hdr]
        if trimming:
            cmd += ["-ss", str(start)]
        cmd += ["-i", f["url"]]

    if len(inputs) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]

    if trimming:
        cmd += ["-t", str(end - start)]
        # Re-encode for frame-accurate cut points.
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
    else:
        # Full video: copy through, fast and exact.
        cmd += ["-c", "copy"]

    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]

    def generate():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()

    base = f"{title}_{start}-{end}" if trimming else title
    return Response(
        generate(),
        mimetype="video/mp4",
        headers={"Content-Disposition": content_disposition(base)},
    )


if __name__ == "__main__":
    # threaded=True so info-fetch and a streaming download don't block each other.
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)