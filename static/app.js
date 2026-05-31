const $ = (id) => document.getElementById(id);
const state = { duration: 0, qualities: [] };

/* remembered trim selection (survives a Full <-> Trim round trip for 5 min) */
const TRIM_MEMORY_MS = 5 * 60 * 1000;
let trimMemory = null;
let pollTimer = null;

/* ---------- time helpers ---------- */
function hmsToSeconds(v) {
  if (!v) return NaN;
  const parts = v.trim().split(":");
  if (parts.length > 3) return NaN;
  const nums = parts.map((p) => parseInt(p, 10));
  if (nums.some(isNaN)) return NaN;
  return nums.reduce((acc, n) => acc * 60 + n, 0);
}
function secondsToHms(total) {
  total = Math.max(0, Math.floor(total));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(s)}`;
}
function fmtBytes(b) {
  if (!b) return "size n/a";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `\u2248 ${b.toFixed(b < 10 ? 1 : 0)} ${u[i]}`;
}

/* ---------- mode ---------- */
function currentMode() {
  return document.querySelector(".seg-btn.active").dataset.mode;
}
function isFull() { return currentMode() === "full"; }

function clipFraction() {
  if (isFull() || !state.duration) return 1;
  const s = hmsToSeconds($("start").value);
  const e = hmsToSeconds($("end").value);
  if (isNaN(s) || isNaN(e) || e <= s) return null;
  return Math.min(1, (e - s) / state.duration);
}

/* ---------- quality dropdown with live size ---------- */
function renderQualities() {
  const sel = $("quality");
  const frac = clipFraction();
  const prev = sel.value;
  sel.innerHTML = "";
  for (const q of state.qualities) {
    const opt = document.createElement("option");
    opt.value = q.height;
    let size = "";
    if (q.filesize_full && frac !== null) size = " \u2014 " + fmtBytes(q.filesize_full * frac);
    else if (frac === null) size = " \u2014 set valid times";
    opt.textContent = q.label + size + (q.h264 ? "" : " \u2022 re-encode");
    sel.appendChild(opt);
  }
  if (prev) sel.value = prev;
}

/* ---------- tooltips / validation ---------- */
function showTip(target, msg) {
  $(target).classList.add("invalid");
  const tip = $(`${target}-tip`);
  tip.textContent = msg;
  tip.classList.add("show");
}
function clearTip(target) {
  $(target).classList.remove("invalid");
  $(`${target}-tip`).classList.remove("show");
}
const flashTimers = {};
function flashTip(target, msg) {
  showTip(target, msg);
  clearTimeout(flashTimers[target]);
  flashTimers[target] = setTimeout(() => validateTimes(), 1400);
}
function endLimitMsg() {
  return `Can't go past the video end (${secondsToHms(state.duration)})`;
}

function validateTimes() {
  if (isFull()) {
    clearTip("start"); clearTip("end");
    $("downloadBtn").disabled = false;
    return true;
  }
  const s = hmsToSeconds($("start").value);
  const e = hmsToSeconds($("end").value);
  let ok = true;

  if (isNaN(e)) { showTip("end", "Use HH:MM:SS"); ok = false; }
  else if (state.duration && e > state.duration) { showTip("end", endLimitMsg()); ok = false; }
  else if (e <= 0) { showTip("end", "End must be greater than 0"); ok = false; }
  else clearTip("end");

  if (isNaN(s)) { showTip("start", "Use HH:MM:SS"); ok = false; }
  else if (s < 0) { showTip("start", "Start can't be negative"); ok = false; }
  else if (!isNaN(e) && s >= e) { showTip("start", "Start can't reach the end time"); ok = false; }
  else clearTip("start");

  $("downloadBtn").disabled = !ok;
  return ok;
}

/* ---------- steppers ---------- */
function step(target, dir) {
  if (isFull()) return;
  const input = $(target);
  let val = hmsToSeconds(input.value);
  if (isNaN(val)) val = target === "start" ? 0 : (state.duration || 0);

  let next = val + dir;
  let blockedMsg = null;

  if (target === "start") {
    const ceil = (hmsToSeconds($("end").value) || state.duration || 1) - 1;
    if (next > ceil) { next = ceil; blockedMsg = "Start can't reach the end time"; }
    if (next < 0) next = 0;
  } else {
    const ceil = state.duration || next;
    if (state.duration && next > ceil) { next = ceil; blockedMsg = endLimitMsg(); }
    const floor = (hmsToSeconds($("start").value) || 0) + 1;
    if (next < floor) next = floor;
  }

  input.value = secondsToHms(next);
  if (blockedMsg) flashTip(target, blockedMsg);
  validateTimes();
  renderQualities();
}

function holdRepeat(btn) {
  let timer, interval;
  const fire = () => step(btn.dataset.target, parseInt(btn.dataset.dir, 10));
  const startHold = (e) => {
    e.preventDefault();
    fire();
    timer = setTimeout(() => { interval = setInterval(fire, 80); }, 350);
  };
  const stopHold = () => { clearTimeout(timer); clearInterval(interval); };
  btn.addEventListener("mousedown", startHold);
  btn.addEventListener("touchstart", startHold, { passive: false });
  ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach((ev) =>
    btn.addEventListener(ev, stopHold)
  );
}

/* ---------- fetch info ---------- */
async function fetchInfo() {
  const url = $("url").value.trim();
  if (!url) { setStatus("Paste a link first.", true); return; }

  resetProgress();
  $("fetchBtn").disabled = true;
  $("fetchBtn").textContent = "...";
  setStatus("Reading video\u2026");

  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed.");

    state.duration = data.duration || 0;
    state.qualities = data.qualities;
    trimMemory = null;

    $("videoTitle").textContent = data.title;
    $("vidStart").textContent = "00:00:00";
    $("vidEnd").textContent = secondsToHms(state.duration);
    $("start").value = "00:00:00";
    $("end").value = secondsToHms(state.duration);

    setMode(currentMode());
    renderQualities();
    $("options").hidden = false;
    setStatus("");
  } catch (e) {
    setStatus(e.message, true);
    $("options").hidden = true;
  } finally {
    $("fetchBtn").disabled = false;
    $("fetchBtn").textContent = "Fetch";
  }
}

function setStatus(msg, isError = false) {
  const el = $("status");
  if (!msg) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

/* ---------- mode toggle (with 5-min trim memory) ---------- */
function setMode(mode) {
  const prev = currentMode();
  if (prev === "trim" && mode === "full") {
    trimMemory = { start: $("start").value, end: $("end").value, savedAt: Date.now() };
  }

  document.querySelectorAll(".seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode)
  );

  const full = mode === "full";
  if (full) {
    $("start").value = "00:00:00";
    $("end").value = secondsToHms(state.duration);
  } else if (prev === "full") {
    const fresh = trimMemory && (Date.now() - trimMemory.savedAt) < TRIM_MEMORY_MS;
    if (fresh) {
      $("start").value = trimMemory.start;
      $("end").value = trimMemory.end;
    } else {
      trimMemory = null;
      $("start").value = "00:00:00";
      $("end").value = secondsToHms(state.duration);
    }
  }

  $("start").disabled = full;
  $("end").disabled = full;
  validateTimes();
  renderQualities();
}

/* ---------- progress UI ---------- */
const STAGE_LABELS = {
  downloading: "Downloading\u2026",
  trimming: "Trimming\u2026",
  finalizing: "Finalizing\u2026",
};
function showProgress(pct, label) {
  $("progress").hidden = false;
  $("progressFill").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("progressStage").textContent = label;
  $("progressPct").textContent = Math.round(pct) + "%";
}
function resetProgress() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  $("progress").hidden = true;
  $("progressFill").style.width = "0%";
  $("doneMsg").hidden = true;
}

/* ---------- download (start job -> poll -> save) ---------- */
async function startDownload() {
  if (!validateTimes()) return;

  resetProgress();
  setStatus("");
  $("downloadBtn").disabled = true;
  showProgress(0, "Starting\u2026");

  const body = {
    url: $("url").value.trim(),
    height: $("quality").value,
    mode: currentMode(),
    start: $("start").value,
    end: $("end").value,
    duration: state.duration,
  };

  try {
    const res = await fetch("/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed.");
    pollProgress(data.job_id);
  } catch (e) {
    resetProgress();
    setStatus(e.message, true);
    $("downloadBtn").disabled = false;
  }
}

function pollProgress(jobId) {
  pollTimer = setInterval(async () => {
    let data;
    try {
      const r = await fetch(`/progress/${jobId}`);
      data = await r.json();
    } catch { return; }

    if (data.status === "error") {
      resetProgress();
      setStatus(data.error || "Download failed.", true);
      $("downloadBtn").disabled = false;
      return;
    }

    showProgress(data.percent || 0, STAGE_LABELS[data.stage] || "Working\u2026");

    if (data.status === "done") {
      clearInterval(pollTimer);
      pollTimer = null;
      showProgress(100, "Done");
      const frame = document.createElement("iframe");
      frame.style.display = "none";
      frame.src = `/file/${jobId}`;
      document.body.appendChild(frame);
      $("doneMsg").hidden = false;
      $("downloadBtn").disabled = false;
    }
  }, 400);
}

/* ---------- wire up ---------- */
$("fetchBtn").addEventListener("click", fetchInfo);
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") fetchInfo(); });
$("downloadBtn").addEventListener("click", startDownload);
document.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => setMode(b.dataset.mode))
);
document.querySelectorAll(".step").forEach(holdRepeat);
["start", "end"].forEach((id) =>
  $(id).addEventListener("input", () => { validateTimes(); renderQualities(); })
);