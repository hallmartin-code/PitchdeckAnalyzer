"""
TEN Capital — Pitch Deck Analyzer (web app)

Upload a pitch deck PDF, the Claude API runs the deck analysis, and the branded
TEN Capital .docx comes back as a download.

Analysis takes a few minutes, so uploads start a background job and the page
polls for progress rather than holding the request open.

Run locally:
    pip install -r requirements.txt
    $env:ANTHROPIC_API_KEY = "sk-ant-..."      # PowerShell
    uvicorn app:app --reload

Environment:
    ANTHROPIC_API_KEY   required — from console.anthropic.com/settings/keys
    APP_PASSWORD        optional — when set, the whole app sits behind
                        HTTP Basic auth (username `ten`, or set APP_USERNAME)
    JOBS_DIR            optional — where uploads and reports are written
    JOB_TTL_MINUTES     optional — how long finished reports stay downloadable
    RESEND_API_KEY      optional — from resend.com/api-keys; when set, a copy
                        of every finished report is emailed via Resend
    REPORT_EMAIL_TO     optional — recipient for that copy (default
                        info@tencapital.group)
    REPORT_EMAIL_FROM   optional — Resend "from" address (default uses
                        Resend's unverified onboarding@resend.dev sender —
                        replace with an address on a domain verified in your
                        Resend account for production use)
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import tempfile
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from analysis import MAX_PDF_BYTES, PROGRESS_STEPS, AnalysisError, run_analysis
from email_report import send_report_email

APP_USERNAME = os.getenv("APP_USERNAME", "ten")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
JOBS_DIR = Path(os.getenv("JOBS_DIR") or Path(tempfile.gettempdir()) / "pitchdeck-jobs")
JOB_TTL = timedelta(minutes=int(os.getenv("JOB_TTL_MINUTES", "180")))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

_LOGO_PATH = Path(__file__).parent / "TEN_Capital_logo_footer.png"
LOGO_DATA_URI = (
    "data:image/png;base64," + base64.b64encode(_LOGO_PATH.read_bytes()).decode()
    if _LOGO_PATH.exists()
    else ""
)

app = FastAPI(title="TEN Capital — Pitch Deck Analyzer")
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
_basic = HTTPBasic(auto_error=False)


# --- Auth ---------------------------------------------------------------------


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """HTTP Basic gate — active only when APP_PASSWORD is set."""
    if not APP_PASSWORD:
        return
    ok = credentials is not None and secrets.compare_digest(
        credentials.username, APP_USERNAME
    ) and secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- Job tracking --------------------------------------------------------------


@dataclass
class Job:
    id: str
    company: str
    source: str
    state: str = "queued"  # queued | running | done | error
    step: int = 0
    message: str = "Queued"
    error: str = ""
    outputs: dict[str, Path] = field(default_factory=dict)  # {"docx": Path, "pdf": Path}
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "source": self.source,
            "state": self.state,
            "step": self.step,
            "steps_total": len(PROGRESS_STEPS),
            "message": self.message,
            "error": self.error,
            "downloads": (
                {fmt: f"/jobs/{self.id}/download/{fmt}" for fmt in self.outputs}
                if self.state == "done"
                else None
            ),
        }


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _sweep_expired() -> None:
    """Drop finished jobs (and their files) once they age out."""
    cutoff = datetime.now(timezone.utc) - JOB_TTL
    with _jobs_lock:
        stale = [j for j in _jobs.values() if j.created < cutoff and j.state in ("done", "error")]
        for job in stale:
            _jobs.pop(job.id, None)
    for job in stale:
        folder = JOBS_DIR / job.id
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*"), reverse=True):
            path.unlink(missing_ok=True)
        folder.rmdir()


def _safe_name(name: str) -> str:
    """A filename that survives Windows, Linux, and Content-Disposition."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:80] or "Pitch Deck"


def _run_job(job: Job, pdf_path: Path) -> None:
    def progress(index: int, label: str) -> None:
        job.step, job.message = index, label

    job.state, job.message = "running", PROGRESS_STEPS[0]
    try:
        output_base = pdf_path.parent / f"{_safe_name(job.company)} - TEN Capital Deck Analysis"
        job.outputs = run_analysis(pdf_path, output_base, company_name=job.company, progress=progress)
        job.state, job.step = "done", len(PROGRESS_STEPS)
        job.message = "Report ready"
        send_report_email(job.company, job.source, job.outputs)
    except AnalysisError as exc:
        job.state, job.error, job.message = "error", str(exc), "Analysis failed"
    except Exception as exc:  # surface a usable message, keep the trace in the logs
        traceback.print_exc()
        job.state, job.message = "error", "Analysis failed"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        pdf_path.unlink(missing_ok=True)  # the deck is not ours to keep


# --- Routes --------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.post("/analyze")
async def analyze(
    _: None = Depends(require_auth),
    deck: UploadFile = File(...),
    company: str = Form(""),
) -> JSONResponse:
    if not (deck.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF pitch deck.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY is not set on the server. Add it in the Railway service variables.",
        )

    _sweep_expired()

    job_id = uuid.uuid4().hex[:12]
    folder = JOBS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    pdf_path = folder / _safe_name(Path(deck.filename).name)

    size = 0
    with pdf_path.open("wb") as handle:
        while chunk := await deck.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                handle.close()
                pdf_path.unlink(missing_ok=True)
                folder.rmdir()
                raise HTTPException(
                    413, f"Deck exceeds the {MAX_PDF_BYTES // 1024 // 1024} MB limit the API accepts."
                )
            handle.write(chunk)

    job = Job(
        id=job_id,
        company=(company.strip() or pdf_path.stem),
        source=pdf_path.name,
    )
    with _jobs_lock:
        _jobs[job_id] = job
    _executor.submit(_run_job, job, pdf_path)
    return JSONResponse(job.as_dict(), status_code=202)


@app.get("/jobs/{job_id}")
def job_status(job_id: str, _: None = Depends(require_auth)) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    return job.as_dict()


_DOWNLOAD_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@app.get("/jobs/{job_id}/download/{fmt}")
def job_download(job_id: str, fmt: str, _: None = Depends(require_auth)) -> FileResponse:
    if fmt not in _DOWNLOAD_MEDIA_TYPES:
        raise HTTPException(404, "Unknown report format.")
    job = _jobs.get(job_id)
    output = job.outputs.get(fmt) if job else None
    if job is None or job.state != "done" or output is None or not output.exists():
        raise HTTPException(404, "No report available for that job.")
    return FileResponse(output, filename=output.name, media_type=_DOWNLOAD_MEDIA_TYPES[fmt])


# --- Front end -----------------------------------------------------------------

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TEN Capital — Pitch Deck Analyzer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --navy-950:#0B1526; --navy-900:#101E33; --navy-800:#16283F; --navy-700:#1E354F;
    --coral:#EE5A4E; --coral-soft:#F0776C; --amber:#F3A22A; --teal:#35BEBB;
    --ink-100:#F3F6FA; --ink-300:#C4D0E0; --ink-500:#7E90A8; --ink-600:#5C6E86;
  }
  *{ box-sizing:border-box; }
  html, body{
    margin:0; padding:0; min-height:100vh;
    background:var(--navy-950); color:var(--ink-100);
    font-family:'Inter', "Segoe UI", system-ui, sans-serif;
    font-size:15px; line-height:1.55;
  }
  body{ display:flex; align-items:flex-start; justify-content:center; padding:48px 20px 40px; position:relative; overflow-x:hidden; }

  /* ambient tri-color glow, echoing the logo's three figures */
  body::before{
    content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background:
      radial-gradient(480px 380px at 14% 8%, rgba(238,90,78,0.16), transparent 60%),
      radial-gradient(480px 380px at 86% 6%, rgba(243,162,42,0.13), transparent 60%),
      radial-gradient(560px 420px at 50% 100%, rgba(53,190,187,0.14), transparent 60%);
  }

  /* `margin:auto 0` centres vertically the way the mockup does, but never clips
     the top of the card once the progress panel makes it taller than the viewport. */
  .stage{ position:relative; z-index:1; width:100%; max-width:620px; margin:auto 0; }

  /* brand lockup */
  .brand{ margin-bottom:24px; }
  .logo-badge{
    display:inline-flex; align-items:center; justify-content:center;
    width:96px; height:52px; border-radius:12px; background:#fff;
    box-shadow:0 12px 24px -12px rgba(0,0,0,.5);
  }
  .logo-badge img{ max-width:76%; max-height:68%; display:block; }

  .card{
    background:linear-gradient(180deg, var(--navy-900) 0%, var(--navy-800) 100%);
    border:1px solid var(--navy-700); border-radius:20px;
    padding:44px 44px 36px; position:relative; overflow:hidden;
    box-shadow:0 30px 60px -20px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.03);
  }
  .card::after{
    content:""; position:absolute; top:-2px; left:44px; right:44px; height:2px; border-radius:2px;
    background:linear-gradient(90deg, var(--coral), var(--amber), var(--teal));
  }

  .eyebrow{
    display:flex; align-items:center; gap:8px;
    font-family:'JetBrains Mono', ui-monospace, monospace; font-size:11px;
    letter-spacing:.14em; text-transform:uppercase; color:var(--teal); margin-bottom:14px;
  }
  .eyebrow::before{
    content:""; width:6px; height:6px; border-radius:50%; background:var(--teal);
    box-shadow:0 0 0 3px rgba(53,190,187,.18);
  }

  h1{
    font-family:'Sora', system-ui, sans-serif; font-size:28px; font-weight:700;
    line-height:1.25; letter-spacing:-.01em; margin:0 0 12px;
  }
  h1 .arrow{ color:var(--ink-500); font-weight:400; margin:0 4px; }
  h1 .to{
    background:linear-gradient(90deg, var(--coral-soft), var(--amber));
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  .lede{ color:var(--ink-300); font-size:15px; line-height:1.6; margin:0 0 32px; max-width:46ch; }

  /* fields */
  .field{ margin-bottom:16px; }
  label.field-label{
    display:block; font-family:'JetBrains Mono', ui-monospace, monospace;
    font-size:10.5px; letter-spacing:.14em; text-transform:uppercase;
    color:var(--ink-500); margin-bottom:8px;
  }
  input[type=text]{
    width:100%; padding:12px 14px; border-radius:10px; font:inherit;
    background:rgba(255,255,255,.02); border:1px solid var(--navy-700); color:var(--ink-100);
    transition:border-color .18s ease, background .18s ease;
  }
  input[type=text]::placeholder{ color:var(--ink-600); }
  input[type=text]:focus{ outline:none; border-color:var(--teal); background:rgba(53,190,187,.04); }

  /* dropzone */
  .dropzone{
    display:block; border:1.5px dashed var(--navy-700); border-radius:14px;
    padding:38px 24px; text-align:center; cursor:pointer;
    background:rgba(255,255,255,.015);
    transition:border-color .18s ease, background .18s ease, transform .18s ease;
  }
  .dropzone:hover, .dropzone:focus-within{ border-color:var(--teal); background:rgba(53,190,187,.05); }
  .dropzone:active{ transform:scale(.997); }
  .dropzone.dragover{ border-color:var(--amber); background:rgba(243,162,42,.07); }
  .dropzone.has-file{ border-style:solid; border-color:var(--teal); background:rgba(53,190,187,.06); }

  .dropzone-icon{
    width:38px; height:38px; margin:0 auto 14px; border-radius:10px;
    background:rgba(255,255,255,.04);
    border:1px solid var(--navy-700); display:flex; align-items:center; justify-content:center;
  }
  .dropzone.has-file .dropzone-icon{
    background:rgba(53,190,187,.14);
    border-color:rgba(53,190,187,.4);
  }
  .dropzone-icon svg{ width:18px; height:18px; }
  .dropzone-title{ font-size:15px; font-weight:600; margin-bottom:6px; word-break:break-word; }
  .dropzone-sub{
    font-family:'JetBrains Mono', ui-monospace, monospace; font-size:11.5px;
    color:var(--ink-500); letter-spacing:.01em;
  }
  .dropzone-sub b{ color:var(--ink-300); font-weight:500; }
  .file-input{
    position:absolute; width:1px; height:1px; opacity:0; pointer-events:none;
  }

  /* CTA */
  .cta{
    width:100%; margin-top:22px; padding:16px 20px; border:0; border-radius:12px;
    background:linear-gradient(90deg, var(--coral) 0%, var(--coral-soft) 45%, var(--amber) 100%);
    color:#17130E; font-family:'Sora', system-ui, sans-serif; font-weight:700; font-size:15px;
    letter-spacing:.01em; cursor:pointer; transition:filter .15s ease, transform .15s ease;
    box-shadow:0 10px 24px -10px rgba(238,90,78,.45);
  }
  .cta:hover:not(:disabled){ filter:brightness(1.06); transform:translateY(-1px); }
  .cta:active:not(:disabled){ transform:translateY(0); }
  .cta:disabled{
    background:var(--navy-700); color:var(--ink-500); box-shadow:none; cursor:not-allowed;
  }

  /* progress */
  .status{ display:none; margin-top:28px; padding-top:24px; border-top:1px solid var(--navy-700); }
  .status.on{ display:block; }
  .status-head{
    display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px;
    font-family:'JetBrains Mono', ui-monospace, monospace; font-size:11px;
    letter-spacing:.1em; text-transform:uppercase; color:var(--ink-500);
  }
  .bar{ height:6px; border-radius:3px; background:var(--navy-950); overflow:hidden; }
  .bar > i{
    display:block; height:100%; width:0; border-radius:3px; transition:width .5s ease;
    background:linear-gradient(90deg, var(--coral), var(--amber), var(--teal));
  }
  .steps{ list-style:none; margin:18px 0 0; padding:0; }
  .steps li{
    position:relative; padding:6px 0 6px 24px; font-size:14px; color:var(--ink-600);
    transition:color .2s ease;
  }
  .steps li::before{
    content:""; position:absolute; left:2px; top:13px; width:7px; height:7px;
    border-radius:50%; background:var(--navy-700);
  }
  .steps li.done{ color:var(--ink-300); }
  .steps li.done::before{ background:var(--teal); }
  .steps li.active{ color:var(--ink-100); font-weight:600; }
  .steps li.active::before{ background:var(--amber); box-shadow:0 0 0 4px rgba(243,162,42,.15); animation:pulse 1.6s ease-in-out infinite; }
  @keyframes pulse{ 50%{ box-shadow:0 0 0 7px rgba(243,162,42,0); } }

  /* result */
  .result:empty{ display:none; }
  .done-box, .error-box{ margin-top:22px; padding:18px; border-radius:12px; font-size:14px; }
  .done-box{ background:rgba(53,190,187,.09); border:1px solid rgba(53,190,187,.32); }
  .done-box strong{ font-family:'Sora', system-ui, sans-serif; }
  .error-box{
    background:rgba(238,90,78,.09); border:1px solid rgba(238,90,78,.34);
    color:var(--ink-300); white-space:pre-wrap;
  }
  .error-box strong{ color:var(--coral-soft); font-family:'Sora', system-ui, sans-serif; }
  a.download{
    display:inline-block; margin-top:12px; margin-right:10px; padding:12px 22px; border-radius:10px;
    background:linear-gradient(90deg, var(--coral) 0%, var(--coral-soft) 45%, var(--amber) 100%);
    color:#17130E; font-family:'Sora', system-ui, sans-serif; font-weight:700;
    font-size:14px; text-decoration:none;
    box-shadow:0 10px 24px -10px rgba(238,90,78,.45);
  }
  a.download:hover{ filter:brightness(1.06); }
  a.download:last-child{ margin-right:0; }

  .disclosure{
    margin-top:22px; padding-top:18px; border-top:1px solid var(--navy-700);
    font-size:12px; line-height:1.6; color:var(--ink-500);
  }
  .disclosure code{
    font-family:'JetBrains Mono', ui-monospace, monospace; font-size:11.5px;
    background:var(--navy-950); border:1px solid var(--navy-700); color:var(--ink-300);
    padding:2px 6px; border-radius:5px;
  }
  footer{
    text-align:center; margin-top:22px;
    font-family:'JetBrains Mono', ui-monospace, monospace; font-size:11px;
    letter-spacing:.08em; text-transform:uppercase; color:var(--ink-600);
  }

  :focus-visible{ outline:2px solid var(--teal); outline-offset:2px; }

  @media (max-width:480px){
    body{ padding:32px 14px; }
    .card{ padding:32px 24px 28px; }
    .card::after{ left:24px; right:24px; }
    h1{ font-size:23px; }
  }
  @media (prefers-reduced-motion: reduce){
    *{ animation:none !important; transition:none !important; }
  }
</style>
</head>
<body>
<div class="stage">

  <div class="brand">
    <span class="logo-badge"><img src="%%LOGO%%" alt="TEN Capital Network"></span>
  </div>

  <div class="card">
    <div class="eyebrow">Deck Analyzer</div>
    <h1>Pitch Deck<span class="arrow">&rarr;</span><span class="to">Investor Review</span></h1>
    <p class="lede">Upload a deck and get the branded TEN Capital review as a Word document or PDF —
      section-by-section strengths, weaknesses, and recommendations from Claude.</p>

    <form id="form">
      <div class="field">
        <label class="field-label" for="company">Company name</label>
        <input type="text" id="company" name="company" autocomplete="organization"
               placeholder="Leave blank to use the file name">
      </div>

      <label class="dropzone" id="dropzone" for="deck">
        <div class="dropzone-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="var(--ink-100)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M14 3v4a1 1 0 0 0 1 1h4"/>
            <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z"/>
          </svg>
        </div>
        <div class="dropzone-title" id="dz-title">Click or drop a deck here</div>
        <div class="dropzone-sub" id="dz-sub"><b>.pdf</b> &nbsp;&middot;&nbsp; up to 32&nbsp;MB</div>
        <input class="file-input" type="file" id="deck" name="deck" accept="application/pdf,.pdf" required>
      </label>

      <button class="cta" type="submit" id="go">Run deck analysis</button>
    </form>

    <div class="status" id="status" aria-live="polite">
      <div class="status-head"><span id="phase">Analyzing</span><span id="clock">0:00</span></div>
      <div class="bar"><i id="fill"></i></div>
      <ul class="steps" id="steps"></ul>
    </div>
    <div class="result" id="result"></div>

    <div class="disclosure">
      The deck is processed on the server and deleted as soon as the report is built.
      Finished reports stay downloadable for a limited time — save the <code>.docx</code> or
      <code>.pdf</code> when it appears. A copy of every generated report is also emailed to the
      TEN Capital team.
    </div>
  </div>

  <footer>Claude Opus 5 &middot; typically 2&ndash;4 minutes &middot; keep this tab open</footer>
</div>

<script>
const STEPS = %%STEPS%%;
const MAX_BYTES = %%MAX_BYTES%%;

const form = document.getElementById('form');
const statusBox = document.getElementById('status');
const stepsList = document.getElementById('steps');
const fill = document.getElementById('fill');
const result = document.getElementById('result');
const go = document.getElementById('go');
const deck = document.getElementById('deck');
const dropzone = document.getElementById('dropzone');
const dzTitle = document.getElementById('dz-title');
const dzSub = document.getElementById('dz-sub');
const phase = document.getElementById('phase');
const clock = document.getElementById('clock');

const IDLE_TITLE = dzTitle.textContent;
const IDLE_SUB = dzSub.innerHTML;
let timer = null;

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// --- file selection -----------------------------------------------------------

function showFile() {
  const file = deck.files && deck.files[0];
  if (!file) {
    dropzone.classList.remove('has-file');
    dzTitle.textContent = IDLE_TITLE;
    dzSub.innerHTML = IDLE_SUB;
    return;
  }
  dropzone.classList.add('has-file');
  dzTitle.textContent = file.name;
  dzSub.innerHTML = `<b>${(file.size / 1048576).toFixed(1)} MB</b> &nbsp;&middot;&nbsp; ready to analyze`;
}

deck.addEventListener('change', showFile);

['dragenter', 'dragover'].forEach((type) =>
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.classList.add('dragover');
  }));

['dragleave', 'dragend', 'drop'].forEach((type) =>
  dropzone.addEventListener(type, () => dropzone.classList.remove('dragover')));

dropzone.addEventListener('drop', (event) => {
  event.preventDefault();
  const files = event.dataTransfer && event.dataTransfer.files;
  if (files && files.length) {
    deck.files = files;
    showFile();
  }
});

// --- progress -----------------------------------------------------------------

function renderSteps(step, state) {
  stepsList.innerHTML = STEPS.map((label, i) => {
    let cls = '';
    if (state === 'done' || i < step) cls = 'done';
    else if (i === step && state === 'running') cls = 'active';
    return `<li class="${cls}">${escapeHtml(label)}</li>`;
  }).join('');
  const pct = state === 'done' ? 100 : Math.round((step / STEPS.length) * 100);
  fill.style.width = pct + '%';
}

function startClock() {
  const began = Date.now();
  stopClock();
  timer = setInterval(() => {
    const secs = Math.floor((Date.now() - began) / 1000);
    clock.textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
  }, 1000);
}

function stopClock() {
  if (timer) { clearInterval(timer); timer = null; }
}

function finish(label, buttonText) {
  stopClock();
  phase.textContent = label;
  go.disabled = false;
  go.textContent = buttonText;
}

// --- job lifecycle ------------------------------------------------------------

async function poll(id) {
  const res = await fetch(`/jobs/${id}`);
  if (!res.ok) throw new Error('Lost track of the job — reload and try again.');
  const job = await res.json();
  renderSteps(job.step, job.state);

  if (job.state === 'done') {
    const labels = { docx: 'Download .docx', pdf: 'Download .pdf' };
    const links = Object.entries(job.downloads || {})
      .map(([fmt, href]) => `<a class="download" href="${href}">${labels[fmt] || `Download .${fmt}`}</a>`)
      .join('');
    result.innerHTML = `<div class="done-box"><strong>Report ready.</strong><br>${links}</div>`;
    finish('Complete', 'Run another analysis');
    return;
  }
  if (job.state === 'error') {
    showError(new Error(job.error || 'Analysis failed.'), 'Analysis failed.');
    return;
  }
  setTimeout(() => poll(id).catch(showError), 3000);
}

function showError(err, heading) {
  const title = heading ? `<strong>${escapeHtml(heading)}</strong><br>` : '';
  result.innerHTML = `<div class="error-box">${title}${escapeHtml(err.message)}</div>`;
  finish('Stopped', 'Try again');
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = deck.files && deck.files[0];
  result.innerHTML = '';

  if (!file) { showError(new Error('Choose a PDF pitch deck first.')); return; }
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    showError(new Error('The deck must be a PDF.'));
    return;
  }
  if (file.size > MAX_BYTES) {
    showError(new Error(`That deck is ${(file.size / 1048576).toFixed(1)} MB — the limit is ${MAX_BYTES / 1048576} MB.`));
    return;
  }

  go.disabled = true;
  go.textContent = 'Analyzing…';
  phase.textContent = 'Analyzing';
  clock.textContent = '0:00';
  statusBox.classList.add('on');
  renderSteps(0, 'running');
  startClock();

  try {
    const res = await fetch('/analyze', { method: 'POST', body: new FormData(form) });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || 'Upload failed.');
    }
    const job = await res.json();
    poll(job.id).catch(showError);
  } catch (err) {
    showError(err, 'Upload failed.');
  }
});
</script>
</body>
</html>
""".replace("%%STEPS%%", str(PROGRESS_STEPS).replace("'", '"')).replace(
    "%%MAX_BYTES%%", str(MAX_PDF_BYTES)
).replace("%%LOGO%%", LOGO_DATA_URI)
