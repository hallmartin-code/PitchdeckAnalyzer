# TEN Capital — Pitch Deck Analyzer

Upload a pitch deck PDF → the Claude API reviews the deck section by section →
a branded TEN Capital `.docx` comes back as a download. Deployable to Railway as a web app.

## Files

| File | Role |
|---|---|
| [app.py](app.py) | FastAPI web app — upload page, background jobs, progress polling, download |
| [analysis.py](analysis.py) | The Claude pipeline; also runs standalone as a CLI |
| [schemas.py](schemas.py) | JSON schema for the structured-output call |
| [report_template.py](report_template.py) | Document structure and formatting — the branded `.docx` builder |
| [TEMPLATE_STRUCTURE.md](TEMPLATE_STRUCTURE.md) | Human-readable spec of the document and its fields |
| `TEN_Capital_logo_footer.png` | Footer logo — must ship with the app for the branded footer to render |

## How the analysis works

One Claude API call returning **validated JSON** (structured outputs) rather than prose, so
nothing has to be parsed out of free text. The deck rides along as a document block.

| Call | Produces |
|---|---|
| Deck analysis | Section 1 — section-by-section strengths/weaknesses/recommendations, design notes, revised slide outline |

Model: `claude-opus-5`, adaptive thinking, `effort: high`, streamed. Runs about 2–4 minutes and
costs roughly $0.50–1 per deck depending on deck size.

**Guardrails between the model and the document.** Model output is reconciled against the
template's fixed rows before rendering — the eleven deck sections and the five design dimensions.
A dropped, reordered, or reworded row is put back in place, so the document always has its full
structure.

## Run locally

```powershell
pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # from console.anthropic.com/settings/keys
uvicorn app:app --reload
```

Open http://127.0.0.1:8000.

Command line, no web app:

```powershell
python analysis.py "path\to\deck.pdf"
```

Preview the empty document layout without calling the API:

```powershell
python report_template.py        # writes _template_preview.docx
```

## Deploy to Railway

1. **Push this folder to a GitHub repo.** It needs `app.py`, `analysis.py`, `schemas.py`,
   `report_template.py`, `pdf_template.py`, `email_report.py`, `requirements.txt`, `Procfile`,
   `railway.json`, `.python-version`, and `TEN_Capital_logo_footer.png`.
2. **Railway → New Project → Deploy from GitHub repo**, and pick it. Nixpacks detects Python from
   `requirements.txt`; `railway.json` supplies the start command and the `/healthz` health check.
3. **Variables** (service → Variables):

   | Variable | Required | Purpose |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | yes | Your key from the Claude console |
   | `APP_PASSWORD` | strongly recommended | Puts the whole app behind HTTP Basic auth |
   | `APP_USERNAME` | no | Basic-auth username (default `ten`) |
   | `MAX_CONCURRENT_JOBS` | no | Simultaneous analyses (default `2`) |
   | `JOB_TTL_MINUTES` | no | How long finished reports stay downloadable (default `180`) |
   | `JOBS_DIR` | no | Where uploads and reports are written (default: system temp) |
   | `RESEND_API_KEY` | no | From resend.com/api-keys; when set, emails a copy of every finished report |
   | `REPORT_EMAIL_TO` | no | Recipient for that copy (default `info@tencapital.group`) |
   | `REPORT_EMAIL_FROM` | no | Resend "from" address — must be on a domain verified in your Resend account for production (default uses Resend's unverified `onboarding@resend.dev` sender) |

4. **Settings → Networking → Generate Domain** to get the public URL.

Set `APP_PASSWORD` before sharing the URL. Without it the app is open to anyone who finds it,
and every upload spends your API credit.

### Deployment notes

- **Uploads and reports are ephemeral.** Railway's filesystem resets on every deploy and restart,
  and jobs are held in memory, so a restart mid-analysis loses that job. Download the `.docx` when
  it's ready. If you want reports to survive restarts, attach a Railway volume and point `JOBS_DIR`
  at its mount path.
- **Uploaded decks are deleted** from the server as soon as the report is built.
- **Jobs run in the app process**, so a single Railway instance handles `MAX_CONCURRENT_JOBS` at a
  time; further uploads queue. Raising it raises memory use — each in-flight deck is held in memory
  as base64.
- **Deck size limit is 32 MB**, imposed by the API. Larger decks are rejected at upload with a
  clear message.
- The browser tab must stay open — it's what polls for progress and fetches the file.

## Changing the analysis

- **Document structure or formatting** → [report_template.py](report_template.py); the framework
  vocabularies (`DECK_SECTIONS`, `DESIGN_DIMENSIONS`) live there and drive the prompt, the schema,
  and the rendered document together.
- **What the model is asked** → `DECK_PROMPT` in [analysis.py](analysis.py).
- **Shape of the model's answer** → [schemas.py](schemas.py), kept in step with the template's
  data contract.
