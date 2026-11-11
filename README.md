# Ore & Steel Radar

Fully automatic global intelligence for iron ore, pellets, DRI and steel. Every morning it reads the news on
28 industry topics, 80+ companies and 36 industry websites (Fastmarkets, Argus, S&P Global, Mysteel, SMM, Kallanish,
Reuters, GCC and Indian business press and more), pulls free market data, keeps what matters, has Claude score each item, and publishes an
animated dashboard and a brief for four functions: **Governance & Strategy, Finance & Risk, Supply Chain, and
Innovation & Research**. No company details needed. It also watches the **Strait of Hormuz** and tags every signal
with hashtags so you can see what is trending.

```
Google News searches, 95 industry, maritime, regional and official websites, GDELT global news and RSS feeds
  (~180 sources and searches)
  + FRED/Stooq market data + IMF PortWatch and Straits.live Strait of Hormuz shipping data
  -> keyword pre-filter -> dedupe (one story from many outlets = 1 signal, every outlet recorded)
  -> triage with a fast model (drops off-topic headlines cheaply)
  -> Claude analysis: watch area, region, companies, tailwind/headwind, impact 1-10, confidence, so what, action, leaders
  -> checks: source credibility, cross-outlet verification, figures in the summary must appear in the source text
  -> data/radar.db (history) -> docs/index.html (dashboard) + docs/brief.md (brief) + docs/data/<date>.json (one file per day)
```

## How information is checked

| Check | What it does |
| --- | --- |
| Source credibility | Every outlet is rated: official (company, exchange, government, worldsteel), tier 1 (Reuters, Bloomberg, Fastmarkets, Argus, S&P Global, Mysteel, SMM, Kallanish), tier 2 (established trade and regional press) or tier 3 (everything else). Edit the lists under `source_tiers` in `config.yaml`. |
| Cross-outlet verification | **Verified** = an official source, or 2+ independent outlets including a credible one, or 3+ outlets. **Single source** = one credible outlet so far. **Unconfirmed** = only lesser-known outlets. Status upgrades automatically as more outlets report the story. |
| Figure check | Every number in the AI's "what happened" must appear in the source headline or summary; otherwise the card shows a warning. |
| Confidence caps | Unconfirmed signals are capped at 5/10 confidence and single-source at 7/10; a failed figure check costs 2 points. |
| Grounding | Claude is told to use only the headline and summary and never invent numbers, names or dates. Every card links to all outlets. |

Paywalled sites (Fastmarkets, Argus, S&P Global, Kallanish) are read through their public headlines and snippets only.

## Strait of Hormuz watch

| Data | Source | Notes |
| --- | --- | --- |
| Daily transits by vessel type (tankers, dry bulk, other cargo) | [IMF PortWatch](https://portwatch.imf.org/), chokepoint6 (satellite AIS) | Free, no key. Latest 3 days are provisional. If the PortWatch service is down, the radar uses the same series mirrored by Straits.live. |
| Pre-crisis baseline | Median daily transits, 28 Feb 2025 to 27 Feb 2026 | Change the window under `hormuz` in `config.yaml`. |
| Vessels holding offshore, war-risk insurance, latest incidents | [Straits.live](https://straits.live/) OSINT aggregator (free tier) | Insurance is a curated estimate and can be stale; each incident links to its source. |
| Hormuz news | Google News, GDELT, UKMTO/JMIC, maritime press (gCaptain, Lloyd's List, TradeWinds, Maritime Executive, Windward, Kpler, Vortexa) | Flows into the signals with the `#StraitOfHormuz` hashtag. |

Open-source intelligence tools such as God's Eye View draw on the same public feeds (AIS, GDELT, news RSS); the radar
uses those feeds directly so the figures can be checked and archived.

## What the dashboard shows

- **Date filters** in the top bar: Day, 7 days, 30 days, All, or a custom range, with arrows to step back through
  previous days or weeks. History is kept for 180 days; older days load on demand from `docs/data/`.
- **Top 10 · trending** beside the headline: the top 10 news for the selected hashtag. Hashtags are ranked by how much
  they grew against the previous period; `#StraitOfHormuz` is always shown (`pinned_hashtags` in `config.yaml`).
  Hashtags come from the news itself, not from social media.
- **Strait of Hormuz watch:** status, transits against the pre-crisis level, vessels holding offshore, war-risk
  insurance, a daily transit chart by vessel type and the latest incidents.
- **Market pulse:** iron ore, Australian coal, European gas, US steel PPI and Brent prices with sparklines, plus share
  price moves of Vale, Rio Tinto, BHP, ArcelorMittal, Nucor, POSCO and others.
- **Function tabs** (Governance & Strategy, Finance & Risk, Supply Chain, Innovation & Research) that filter every
  number, chart and signal, and a **competitor picker** to focus on chosen companies.
- **Top 3 calls**, then charts: pressure by watch area, companies in the news and regions. Every
  chart has a table view.
- **Sources we monitor:** every website by credibility tier, plus the searches, market data and shipping data used.
- **All signals** shows impact 7 and above by default; filters for impact, hashtag, watch area, region, verification,
  tailwind/headwind and search. Every signal carries a verification badge and links to all outlets that reported it.

## Repository layout

| Path | What it is |
| --- | --- |
| `radar.py` | The pipeline: collect, triage, analyse, report, serve |
| `templates/dashboard.html` | Dashboard design (Three.js hero, Chart.js charts) |
| `config.yaml` | Topics, companies by group, feeds, leaders, watch areas, regions, settings |
| `docs/` | Published dashboard, brief and per-day data files (GitHub Pages serves this) |
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

## Load four months of history (one-off)

The daily job only looks back two days. To fill the dashboard with history, run **Actions > Backfill history > Run
workflow** once (defaults: 120 days, up to 4,000 headlines analysed). It searches every topic, company and website in
two-week windows, analyses what it finds and commits the result; expect it to take 30 to 90 minutes. This is a
one-time Claude API cost that scales with `max_items`; check usage in the Anthropic console. Locally:
`python radar.py backfill --days 120 --max 4000`.

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
| `python radar.py collect` / `analyse` / `markets` / `report` | Run one step (`markets` also refreshes the Hormuz watch) |
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
- `settings`: models, impact thresholds, cost cap (`max_items_per_run`), default dashboard window (`report_days`)
  and how many days of history the date filter keeps (`history_days`, default 90).

## Cost

Around 50 searches return several hundred headlines a day. The keyword filter and dedupe cut that down, the fast
triage model drops the off-topic rest cheaply, and typically 80 to 200 signals a day get the full analysis.
`max_items_per_run` caps the spend.

## Rules built in

- Claude may use only the headline and summary and is told never to invent numbers, names or dates.
- Every signal keeps its source link. Check the source before acting.
- Only RSS feeds and public pages are used. Do not add LinkedIn scraping; it breaks LinkedIn's terms.

## Value-chain focus and analyst imports

Every signal is mapped to the value chain in `config.yaml` (`focus_areas`): iron ore, iron pellets, pelletizing,
DRP/DRI, meltshop, CCM/casting, heavy section mill and steel market. Each stage carries a per-function
"how we use it" line, and `function_rules` route keyword matches to Governance & Strategy, Finance & Risk,
Supply Chain and Innovation & Research.

Without an API key you can still get analyst-quality signals:

```bash
python radar.py export --file pending.json --since 1   # today's keyword-only headlines
# ...analyse them (e.g. with Claude in chat) into the same JSON fields the AI returns...
python radar.py import --file analysed.json             # saves them and rebuilds the dashboard
python radar.py reclassify                              # re-apply keyword rules after editing config.yaml
```
