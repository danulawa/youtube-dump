# The YouTube Dump

A little app that grabs a YouTube video, the whole thing or just a slice, and saves it straight to your browser. It runs on your own computer. Nothing gets uploaded anywhere.

## What you need first

Three things have to be installed before any of this works:

- **Python 3.10 or newer.** Older versions won't even start.
- **ffmpeg.** It does the actual cutting and merging of the video.
- **Deno.** yt-dlp uses it to get around YouTube's speed throttling. Skip it and your downloads crawl at about 70 KB/s. So don't skip it.

Quick way to check you've got all three:

```bash
python --version
ffmpeg -version
deno --version
```

## Set up the project

Open a terminal inside the project folder, make a clean environment, and install the Python side.

**Mac / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you're in the old Command Prompt instead of PowerShell, the activate line is `venv\Scripts\activate.bat`.

## Install ffmpeg and Deno

These aren't Python packages, so pip won't fetch them. Grab both:

**Mac**
```bash
brew install ffmpeg deno
```

**Linux (Ubuntu / Debian)**
```bash
sudo apt update && sudo apt install -y ffmpeg
curl -fsSL https://deno.land/install.sh | sh
```

**Windows (PowerShell)**
```powershell
winget install Gyan.FFmpeg
winget install DenoLand.Deno
```

Close and reopen your terminal afterward so it notices the new programs. You don't have to configure anything: yt-dlp finds Deno on its own once it's installed.

## Run it

```bash
python app.py
```

Now open http://127.0.0.1:5000 in your browser. That's the whole thing.

You can use `flask --app app run` if you'd rather, but `python app.py` is the better call. It runs threaded, so the page won't freeze while a download is going.

## Using it

Paste a link and hit Fetch. Choose the full video or trim it to a start and end time. Pick a quality. Hit Download, and your browser saves the file as it streams in.

## When something breaks

- **Downloads are painfully slow.** Nine times out of ten, Deno isn't installed or isn't on your PATH. Install it, reopen the terminal, and check `deno --version`.
- **You get an empty or tiny file.** yt-dlp is probably out of date. YouTube changes constantly. Run `pip install -U "yt-dlp[default]"` and try again.
- **Mac certificate error** (`CERTIFICATE_VERIFY_FAILED`). Run the `Install Certificates.command` that shipped with your Python, then `pip install -U certifi`.

## One note

Keep this on your own machine. Putting it on a cloud server usually gets blocked by YouTube anyway, and running it locally avoids that completely. And only download things you're actually allowed to.