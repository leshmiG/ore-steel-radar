# Ore & Steel Radar

Fully automatic global intelligence for iron ore, pellets, DRI and steel. Every morning it reads the news on
28 industry topics and 80+ companies worldwide, keeps what matters, has Claude score each item, and publishes an
animated dashboard and a brief for the **CEO, CFO, CTO (operations), SMO and CPO**. No company details needed.

```
Google News searches + industry feeds (~50 sources)
  -> keyword pre-filter -> dedupe (one story from many outlets = 1 signal, outlets counted)
  -> triage with a fast model (drops off-topic headlines cheaply)
  -> Claude analysis: watch area, region, companies, tailwind/headwind, impact 1-10, confidence, so what, action, leaders
  -> data/radar.db (history) -> docs/index.html (dashboard) + docs/brief.md (brief) + docs/archive/<date>.html
```

## What the dashboard shows

- **Headline ticker** of the top 10 signals, and a **3D hero** of iron ore pellets around a furnace glow
- **Leader switch** (Everyone, CEO, CFO, CTO, SMO, CPO) that filters every number, chart and signal
- **Top 3 calls** for the selected leader, each with so what and a suggested action
- **Charts:** pressure by watch area (radar), signal matrix (impact vs confidence, act-now zone),
  tailwinds vs headwinds, companies in the news, regions, and daily signal flow (after 3 days of history).
  Every chart has a table view.
- **All signals** with filters by watch area, region, tailwind/headwind and search; every item links to its source

## Repository layout

| Path | What it is |
| --- | --- |
| `radar.py` | The pipeline: collect, triage, analyse, report, serve |
| `templates/dashboard.html` | Dashboard design (Three.js hero, Chart.js charts) |
| `config.yaml` | Topics, companies by group, feeds, leaders, watch areas, regions, settings |
| `docs/` | Published dashboard, brief and daily archive (GitHub Pages serves this) |
| `data/` | SQLite history of every headline and analysis |
| `demo/demo_items.json` | Demo data used by `python radar.py demo` |
| `tests/` | Offline tests with a sample feed and a fake Claude client |
| `.github/workflows/radar.yml` | Daily GitHub Action: test, run, commit results |
| `Dockerfile` | Run and host on your own server or AWS |

## Option 1: Run on your laptop

```bash
git clone <your-repo-url> ore-steel-radar && cd ore-steel-radar
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # put your ANTHROPIC_API_KEY in .env
python radar.py run                                      # collect + analyse + report
python radar.py serve                                    # open http://localhost:8000
```

Without an API key the pipeline still runs with a keyword fallback (no so-what), so you can try it for free.

## Option 2: GitHub (runs daily, free hosting with GitHub Pages)

1. Create an empty repository on GitHub and push this folder:
   ```bash
   git remote add origin https://github.com/<you>/ore-steel-radar.git
   git push -u origin main
   ```
2. **Settings > Secrets and variables > Actions > New repository secret**: `ANTHROPIC_API_KEY`.
3. **Settings > Actions > General > Workflow permissions**: Read and write permissions.
4. **Actions > Daily radar > Run workflow** for the first run. After that it runs every day at 06:30 Bahrain time.
5. **Settings > Pages**: Deploy from a branch, `main`, folder `/docs`. Your dashboard is at
   `https://<you>.github.io/ore-steel-radar/`.

> **Privacy:** a GitHub Pages site is public, even from a private repository (unless your company has GitHub
> Enterprise with private Pages), and Pages on private repos needs a paid plan. For internal use, keep the repo
> private and host with Option 3 behind your company login.

## Option 3: Docker (company server, AWS, Azure)

```bash
docker build -t ore-steel-radar .
docker run -d --name radar -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... -e RADAR_DAILY_AT=06:30 -e TZ=Asia/Bahrain \
  -v radar-data:/app/data -v radar-docs:/app/docs ore-steel-radar
```

The container hosts the dashboard on port 8000 and refreshes it every day. Runs as-is on ECS/Fargate or EC2.
For Amazon Bedrock instead of the Anthropic API, set `llm_provider: bedrock`, `bedrock_model` and
`bedrock_triage_model` in `config.yaml`.

## Commands

| Command | What it does |
| --- | --- |
| `python radar.py run` | Collect, analyse and report: the daily job |
| `python radar.py collect` / `analyse` / `report` | Run one step |
| `python radar.py report --days 30` | Dashboard for the last 30 days |
| `python radar.py serve [--daily 06:30]` | Host `docs/` on port 8000, optionally refreshing daily |
| `python radar.py demo` | Build the dashboard from the demo data |
| `python radar.py run --feeds file.xml` | Test with your own RSS file or URL |
| `pytest -q` | Run the offline tests |

## Configure (optional)

Everything works out of the box. To tune it, edit `config.yaml`:
- `competitors`: companies by group (iron ore and pellets, China, Asia and India, Europe and CIS, Americas,
  GCC/Middle East/Africa, green steel and technology). Add or remove names freely.
- `topics`: each becomes a Google News search.
- `perspective`: the viewpoint for the so-what (a generic GCC pellet and steel producer by default).
- `settings`: models, impact thresholds, cost cap (`max_items_per_run`), dashboard window (`report_days`).

## Cost

Around 50 searches return several hundred headlines a day. The keyword filter and dedupe cut that down, the fast
triage model drops the off-topic rest cheaply, and typically 80 to 200 signals a day get the full analysis.
`max_items_per_run` caps the spend.

## Rules built in

- Claude may use only the headline and summary and is told never to invent numbers, names or dates.
- Every signal keeps its source link. Check the source before acting.
- Only RSS feeds and public pages are used. Do not add LinkedIn scraping; it breaks LinkedIn's terms.
