# The YouTube Dump

A tiny local web app that downloads a YouTube video — full or trimmed — at a
quality you choose, and **streams it straight to your browser without ever
writing a file on the server**. Built with Flask + yt-dlp + ffmpeg.

> Runs entirely on your own machine. Nothing is uploaded anywhere; downloads
> use your own internet connection.

---

## 1. System requirements

You need **all three** of these before the app will work:

| Requirement | Minimum | Why it's needed |
|-------------|---------|-----------------|
| **Python**  | **3.10 or newer** | The code uses modern type-hint syntax (`str \| None`). 3.9 and older will not start. |
| **ffmpeg**  | any recent build **with libx264** | Trims, merges video+audio, and produces the MP4. A minimal build without libx264 will fail on the *trim* path. |
| **Deno**    | any recent version | yt-dlp needs a JavaScript runtime to solve YouTube's anti-download challenge. **Without it, downloads crawl at ~70 KB/s.** |

Also: a modern web browser, and an internet connection.

Check what you already have:

```bash
python --version      # or: python3 --version   -> must be 3.10+
ffmpeg -version        # should print version info
deno --version         # should print version info
```

---

## 2. Get the project

Download or clone the folder so you have this structure:

```
youtube-dump/
├── app.py
├── requirements.txt
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── app.js
```

Open a terminal **inside the `youtube-dump` folder** before running anything below.

---

## 3. Install the Python packages

Use a virtual environment so nothing pollutes your system Python. The activate
command is the **only** step that differs by OS.

**macOS / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
```

**Windows (Command Prompt / cmd)**
```cmd
python -m venv venv
venv\Scripts\activate.bat
pip install -U pip
pip install -r requirements.txt
```

> **Tip:** YouTube changes things often, so keep yt-dlp current. Re-running
> this every so often prevents most "empty file" / "format not found" problems:
> ```
> pip install -U "yt-dlp[default]"
> ```
> The `[default]` extra also pulls in the JavaScript-solver glue yt-dlp uses
> alongside Deno.

---

## 4. Install ffmpeg and Deno  ⚠️ don't skip this

These are **system programs**, not pip packages — they are not installed by
`pip install -r requirements.txt`. Install both, then make sure they're on your
`PATH` (the version checks in Section 1 confirm this).

### ffmpeg

**macOS** (via [Homebrew](https://brew.sh))
```bash
brew install ffmpeg
```

**Linux**
```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg
# Fedora
sudo dnf install -y ffmpeg
# Arch
sudo pacman -S ffmpeg
```

**Windows**
```powershell
winget install Gyan.FFmpeg
# or, with Chocolatey:
choco install ffmpeg-full
```
If you install manually instead, download a **full** build (e.g. from
gyan.dev), unzip it, and add its `bin` folder to your `PATH`.

### Deno

**macOS**
```bash
brew install deno
```

**Linux / macOS (official installer)**
```bash
curl -fsSL https://deno.land/install.sh | sh
```
Then follow the printed instruction to add Deno to your `PATH` (usually adding
`export PATH="$HOME/.deno/bin:$PATH"` to your `~/.zshrc` or `~/.bashrc`).

**Windows (PowerShell)**
```powershell
irm https://deno.land/install.ps1 | iex
# or:  winget install DenoLand.Deno
# or:  choco install deno
```

> yt-dlp **auto-detects** Deno once it's on your `PATH` — no configuration
> needed. You don't have to tell the app about it.

### If ffmpeg isn't on your PATH

You can point the app at an explicit ffmpeg binary with the `FFMPEG_PATH`
environment variable instead of fixing your PATH:

```bash
# macOS / Linux
export FFMPEG_PATH=/full/path/to/ffmpeg
```
```powershell
# Windows PowerShell
$env:FFMPEG_PATH="C:\full\path\to\ffmpeg.exe"
```
```cmd
:: Windows cmd
set FFMPEG_PATH=C:\full\path\to\ffmpeg.exe
```

---

## 5. Run it

With your virtual environment **activated** and you in the `youtube-dump` folder:

```bash
python app.py
```
(Use `python3` on macOS/Linux if `python` points to Python 2.)

You'll see something like:
```
 * Running on http://127.0.0.1:5000
```

Open that address in your browser: **http://127.0.0.1:5000**

### Prefer `flask run`?

It works, but read this first:

```bash
# macOS / Linux
flask --app app run --debug
```
```powershell
# Windows PowerShell
flask --app app run --debug
```

⚠️ **`flask run` is single-threaded by default**, which means a streaming
download will block other requests (the page can feel frozen while a download
runs). For this app, **`python app.py` is recommended** — it enables threaded
mode automatically so the UI stays responsive during a download.

---

## 6. Using the app

1. Paste a YouTube link and click **Fetch**.
2. Pick **Full video** or **Trim** (Trim reveals editable start/end time boxes
   with ▲/▼ steppers; the dropdown shows an estimated size per quality).
3. Choose a quality.
4. Click **Download** — your browser saves the file as it streams in.

---

## 7. Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| **Downloads ~70 KB/s** | Deno isn't installed / not on PATH, so yt-dlp can't beat YouTube's throttling. Install Deno (Section 4) and confirm `deno --version`. |
| **`Error 404: No Video File Found`** | The link isn't a single video (bad URL, private/removed video, or a playlist/channel link). |
| **Empty / tiny output file, or "format not found"** | Usually an outdated yt-dlp. Run `pip install -U "yt-dlp[default]"`. |
| **`CERTIFICATE_VERIFY_FAILED` (macOS, python.org Python)** | Run `/Applications/Python\ 3.xx/Install\ Certificates.command`, then `pip install -U certifi`. |
| **Trim path crashes / `ffmpeg ... libx264` error** | Your ffmpeg build lacks libx264. Install a full build (Section 4). |
| **`flask` command not found** | Activate the virtual environment first, or just use `python app.py`. |

---

## 8. Notes

- This is a **personal, local-use** tool. It is not designed for, and will
  often be blocked when, hosted on a cloud server (datacenter IPs get
  bot-flagged by YouTube). Running it on your own machine avoids that entirely.
- Respect copyright and YouTube's Terms of Service. Download only content you
  own or are permitted to download.
MDEOF
echo "README written"
echo "--- preview (head) ---"
head -40 /mnt/user-data/outputs/youtube-dump/README.md
echo "..."
wc -l /mnt/user-data/outputs/youtube-dump/README.md
Output