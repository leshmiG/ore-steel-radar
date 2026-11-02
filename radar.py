#!/usr/bin/env python3
"""Ore & Steel Radar: automatic global intelligence for iron ore, pellets and steel.

Pipeline:  collect (RSS) -> dedupe -> triage (cheap model) -> analyse (Claude) -> store (SQLite)
           -> report (docs/index.html dashboard + docs/brief.md)

Usage:
    python radar.py run                  # collect + analyse + report (the daily job)
    python radar.py collect | analyse | report [--days 7]
    python radar.py serve [--daily 06:30] # host docs/ and refresh daily (Docker / server)
    python radar.py demo                 # dashboard from demo/demo_items.json
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "templates" / "dashboard.html"
LEADERS = ["CEO", "CFO", "CTO", "SMO", "CPO"]
SIGNALS = ["tailwind", "headwind", "neutral"]
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# A headline must mention one of these (or a tracked company) before any LLM spend.
CORE_TERMS = [
    "iron ore", "pellet", "steel", "dri", "hbi", "direct reduc", "briquett", "eaf", "arc furnace",
    "blast furnace", "scrap", "mine", "mining", "freight", "capesize", "dry bulk", "cbam", "tariff",
    "safeguard", "hydrogen", "metallurg", "coking coal", "rebar", "coil",
]

# Keyword fallback (no API key): watch area by keywords
FALLBACK_AREAS = {
    "Markets and prices": ["price", "premium", "futures", "index", "forecast", "per tonne", "$/t"],
    "Supply and demand": ["output", "production", "capacity", "demand", "inventor", "shipments", "exports", "imports"],
    "Technology and process": ["hydrogen", "dri", "direct reduc", "arc furnace", "pelletiz", "process", "technology", "pilot"],
    "Policy and regulation": ["cbam", "tariff", "sanction", "regulat", "policy", "carbon border", "safeguard", "duty"],
    "Customers and end markets": ["construction", "automotive", "infrastructure", "contract", "offtake", "supply deal"],
    "Suppliers and logistics": ["freight", "port", "vessel", "shipping", "capesize", "strike", "gas price", "refractor", "electrode"],
    "Research and innovation": ["study", "research", "patent", "university", "startup"],
    "Macro and geopolitics": ["stimulus", "interest rate", "war", "conflict", "election", "currency", "gdp"],
}
AREA_LEADERS = {
    "Markets and prices": ["SMO", "CFO", "CPO"], "Supply and demand": ["CEO", "SMO"],
    "Technology and process": ["CTO"], "Policy and regulation": ["CEO", "CFO", "SMO"],
    "Customers and end markets": ["SMO"], "Suppliers and logistics": ["CPO", "CFO"],
    "Research and innovation": ["CTO"], "Macro and geopolitics": ["CEO", "CFO"], "Competitors": ["CEO", "SMO"],
}
REGION_WORDS = {
    "GCC and Middle East": ["saudi", "uae", "emirates", "oman", "bahrain", "qatar", "kuwait", "iran", "egypt", "gcc", "middle east", "duqm", "dubai", "abu dhabi"],
    "China": ["china", "chinese", "beijing", "shanghai", "dalian", "baowu", "hebei"],
    "India": ["india", "indian"],
    "Asia Pacific": ["japan", "korea", "vietnam", "indonesia", "australia", "pilbara", "taiwan", "asia"],
    "Europe": ["europe", "eu ", "germany", "sweden", "italy", "uk ", "britain", "france", "spain", "ukraine", "turkey"],
    "CIS": ["russia", "kazakhstan", "cis"],
    "North America": ["united states", "u.s.", "us ", "canada", "mexico", "america"],
    "South America": ["brazil", "chile", "peru", "argentina"],
    "Africa": ["africa", "guinea", "simandou", "mauritania", "liberia"],
}
HIGH_WORDS = ["acquire", "acquisition", "merger", "new plant", "capacity", "shutdown", "outage", "strike",
              "force majeure", "tariff", "sanction", "record", "cut", "closure", "breaks ground", "halt"]


# ---------------------------------------------------------------- config and storage
def load_env(path=ROOT / ".env"):
    """Minimal .env loader so local runs pick up ANTHROPIC_API_KEY without extra packages."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if os.getenv("RADAR_COMPANY_PROFILE"):
        cfg["perspective"] = os.environ["RADAR_COMPANY_PROFILE"]
    groups = cfg.get("competitors") or {}
    if isinstance(groups, list):  # allow a flat list too
        groups = {"companies": groups}
    cfg["competitor_groups"] = groups
    cfg["companies"] = [c for names in groups.values() for c in names]
    return cfg


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS items(
        id TEXT PRIMARY KEY, title TEXT, link TEXT, source TEXT, published TEXT,
        summary TEXT, query TEXT, first_seen TEXT, coverage INTEGER DEFAULT 1, analysed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS insights(
        item_id TEXT PRIMARY KEY, relevant INTEGER, watch_area TEXT, event_type TEXT, companies TEXT,
        countries TEXT, region TEXT, leaders TEXT, signal TEXT, impact INTEGER, confidence INTEGER,
        what_happened TEXT, so_what TEXT, action TEXT, analysed_by TEXT, analysed_at TEXT);
    """)
    return db


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- collect
def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_title(title):
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title or "")  # drop " - Reuters" style suffix
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def build_sources(cfg):
    """(label, url) pairs: one search per topic, companies OR-ed in groups of 4, plus extra feeds."""
    lookback = cfg["settings"].get("lookback", "2d")
    out = [(t, GOOGLE_NEWS.format(q=urllib.parse.quote(f"{t} when:{lookback}"))) for t in cfg.get("topics", [])]
    for group, names in cfg["competitor_groups"].items():
        for i in range(0, len(names), 4):
            ors = " OR ".join(f'"{n}"' for n in names[i:i + 4])
            q = f"({ors}) (iron ore OR pellets OR steel) when:{lookback}"
            out.append((f"companies:{group}", GOOGLE_NEWS.format(q=urllib.parse.quote(q))))
    out += [("feed", f) for f in cfg.get("feeds", [])]
    return out


def prefilter(text, cfg):
    t = text.lower()
    return any(k in t for k in CORE_TERMS) or any(c.lower() in t for c in cfg["companies"])


def collect(cfg, db, sources=None):
    """Pull RSS feeds. `sources` = list of (label, url_or_path) overriding the config (tests)."""
    import feedparser

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    recent = [(r["id"], norm_title(r["title"])) for r in
              db.execute("SELECT id, title FROM items WHERE first_seen >= ?", (since,))]
    added = dup = skipped = 0
    for label, url in (sources or build_sources(cfg)):
        try:
            feed = feedparser.parse(url)
        except Exception as exc:  # one bad feed never stops the run
            print(f"collect: {label} failed ({exc})")
            continue
        feed_title = clean(feed.feed.get("title", "")) if feed.get("feed") else ""
        for e in feed.entries:
            title = clean(e.get("title", ""))
            if not title:
                continue
            summary = clean(e.get("summary", ""))[:600]
            if not prefilter(f"{title} {summary}", cfg):
                skipped += 1
                continue
            key = norm_title(title)
            item_id = hashlib.sha1(key.encode()).hexdigest()[:16]
            match = next((rid for rid, t in recent if rid == item_id or SequenceMatcher(None, t, key).ratio() >= 0.88), None)
            if match:
                db.execute("UPDATE items SET coverage = coverage + 1 WHERE id = ?", (match,))
                dup += 1
                continue
            src = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
            db.execute("INSERT INTO items(id,title,link,source,published,summary,query,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                       (item_id, title, e.get("link", ""), src or feed_title,
                        e.get("published", "") or e.get("updated", ""), summary, label, now_iso()))
            recent.append((item_id, key))
            added += 1
    db.commit()
    print(f"collect: {added} new, {dup} duplicates merged, {skipped} off-topic skipped")
    return added


# ---------------------------------------------------------------- analyse
TRIAGE_PROMPT = """You screen news headlines for a global iron ore, pellet and steel intelligence service.
Keep a headline if it is about iron ore, pellets, DRI/HBI, steelmaking, steel markets or steel companies,
their raw materials, energy, freight, technology, or policy and macro events that move them.
Reply with ONLY a JSON array of the ids to keep, e.g. ["a1","b2"]."""


def analysis_prompt(cfg):
    leaders = "\n".join(f"- {k}: {v}" for k, v in cfg["leaders"].items())
    return f"""You are the market intelligence analyst for a global iron ore, pellet and steel radar.
Write every "so what" from this perspective: {cfg['perspective']}

Leaders you serve:
{leaders}

Watch areas: {", ".join(cfg['watch_areas'])}
Regions: {", ".join(cfg['regions'])}

Rules:
- Use ONLY the headline and summary given. Never invent numbers, dates, names or facts.
- If the text is too thin to judge, keep impact low and say what to check.
- relevant = false for items unrelated to iron ore, pellets, DRI, steelmaking, their inputs, markets or policy.
- impact 1-10 for a pellet and steel producer: 8-10 act now (capacity, deals, outages, big price or policy
  moves near its markets); 5-7 watch; 1-4 note only.
- confidence 1-10: official release or hard data high; opinion, forecast or rumour low.
- signal: "tailwind" if it likely helps a pellet and steel producer's margins or position, "headwind" if it
  likely hurts, "neutral" if mixed or unclear.
- leaders: only those whose decisions this could change (from {", ".join(LEADERS)}).
- region: one value from the regions list.
- what_happened: max 30 words, factual. so_what: max 35 words. action: max 20 words, starts with a verb, or "".

Reply with ONLY a JSON array, one object per headline:
[{{"id": "...", "relevant": true, "watch_area": "...", "event_type": "2-3 words", "companies": ["..."],
   "countries": ["..."], "region": "...", "leaders": ["CEO"], "signal": "tailwind", "impact": 7,
   "confidence": 8, "what_happened": "...", "so_what": "...", "action": "..."}}]"""


def llm_clients(cfg):
    """Return (client, analysis_model, triage_model) or (None, None, None)."""
    s = cfg["settings"]
    provider = s.get("llm_provider", "anthropic")
    if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        return anthropic.Anthropic(), s["model"], s.get("triage_model") or None
    if provider == "bedrock" and s.get("bedrock_model"):
        import anthropic
        client = anthropic.AnthropicBedrock(aws_region=s.get("aws_region", "us-east-1"))
        return client, s["bedrock_model"], s.get("bedrock_triage_model") or None
    return None, None, None


def ask(client, model, system, payload, max_tokens=4000):
    resp = client.messages.create(model=model, max_tokens=max_tokens, temperature=0, system=system,
                                  messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    text = resp.content[0].text
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in model reply")
    return json.loads(text[start:end + 1])


def clamp(v, lo=1, hi=10):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def guess_region(text):
    t = f" {text.lower()} "
    for region, words in REGION_WORDS.items():
        if any(w in t for w in words):
            return region
    return "Global"


def fallback_analysis(row, cfg):
    text = f"{row['title']} {row['summary']}".lower()
    if str(row["query"]).startswith("companies:"):
        area = "Competitors"
    else:
        scores = {a: sum(k in text for k in kws) for a, kws in FALLBACK_AREAS.items()}
        area = max(scores, key=scores.get) if max(scores.values()) else "Supply and demand"
    return {"relevant": True, "watch_area": area, "event_type": "Headline",
            "companies": [c for c in cfg["companies"] if c.lower() in text][:3], "countries": [],
            "region": guess_region(text), "leaders": AREA_LEADERS.get(area, ["CEO"]), "signal": "neutral",
            "impact": min(10, 3 + 2 * sum(w in text for w in HIGH_WORDS)), "confidence": 4,
            "what_happened": row["title"],
            "so_what": "AI analysis is off. Add ANTHROPIC_API_KEY to get the impact and so-what.", "action": ""}


def save_insight(db, item_id, a, by, cfg):
    region = a.get("region") if a.get("region") in cfg["regions"] else "Global"
    signal = a.get("signal") if a.get("signal") in SIGNALS else "neutral"
    db.execute("INSERT OR REPLACE INTO insights VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        item_id, int(bool(a.get("relevant"))), a.get("watch_area", ""), a.get("event_type", ""),
        json.dumps(a.get("companies", [])), json.dumps(a.get("countries", [])), region,
        json.dumps([l for l in a.get("leaders", []) if l in LEADERS]), signal,
        clamp(a.get("impact")), clamp(a.get("confidence")), a.get("what_happened", ""),
        a.get("so_what", ""), a.get("action", ""), by, now_iso()))
    db.execute("UPDATE items SET analysed = 1 WHERE id = ?", (item_id,))


def triage(cfg, client, model, rows):
    """Cheap first pass: drop off-topic headlines before the expensive analysis."""
    if not model:
        return rows
    size = cfg["settings"].get("triage_batch_size", 40)
    keep = []
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        try:
            ids = set(ask(client, model, TRIAGE_PROMPT, [{"id": r["id"], "headline": r["title"]} for r in chunk], 1500))
        except Exception as exc:
            print(f"triage: batch failed ({exc}); keeping all")
            ids = {r["id"] for r in chunk}
        keep += [r for r in chunk if r["id"] in ids]
    print(f"triage: kept {len(keep)} of {len(rows)} headlines")
    return keep


def analyse(cfg, db):
    s = cfg["settings"]
    rows = db.execute("SELECT * FROM items WHERE analysed = 0 ORDER BY first_seen LIMIT ?",
                      (s.get("max_items_per_run", 500),)).fetchall()
    if not rows:
        print("analyse: nothing new")
        return 0
    client, model, triage_model = llm_clients(cfg)
    if client is None:
        print("analyse: no AI model configured, using keyword fallback")
        for r in rows:
            save_insight(db, r["id"], fallback_analysis(r, cfg), "fallback", cfg)
        db.commit()
        return len(rows)

    kept = triage(cfg, client, triage_model, rows)
    kept_ids = {r["id"] for r in kept}
    for r in rows:  # mark dropped headlines so they are not re-sent
        if r["id"] not in kept_ids:
            save_insight(db, r["id"], {"relevant": False}, "triage", cfg)
    db.commit()

    system = analysis_prompt(cfg)
    size = s.get("batch_size", 10)
    done = 0
    for i in range(0, len(kept), size):
        batch = kept[i:i + size]
        payload = [{"id": r["id"], "headline": r["title"], "summary": r["summary"], "source": r["source"]} for r in batch]
        try:
            results = {x.get("id"): x for x in ask(client, model, system, payload)}
        except Exception as exc:  # unanalysed items are retried next run
            print(f"analyse: batch {i // size + 1} failed ({exc}); will retry next run")
            continue
        for r in batch:
            if r["id"] in results:
                save_insight(db, r["id"], results[r["id"]], model, cfg)
                done += 1
        db.commit()
    print(f"analyse: {done} of {len(kept)} headlines analysed with {model}")
    return done


# ---------------------------------------------------------------- report
def item_date(published, first_seen):
    """The day the news happened: the feed's publish date, else the day the radar first saw it."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(published).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    if re.match(r"\d{4}-\d{2}-\d{2}", published or ""):
        return published[:10]
    return first_seen[:10]


def load_items(db, history_days):
    """All relevant signals first seen in the last `history_days`, plus headlines scanned per day."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=history_days)).isoformat()
    scanned = {r[0]: r[1] for r in db.execute(
        "SELECT substr(first_seen,1,10), COUNT(*) FROM items WHERE first_seen >= ? GROUP BY 1", (since,))}
    rows = db.execute("""SELECT i.*, n.* FROM items i JOIN insights n ON n.item_id = i.id
                         WHERE i.first_seen >= ? AND n.relevant = 1 ORDER BY n.impact DESC, i.first_seen DESC""",
                      (since,)).fetchall()
    items = [{
        "title": r["title"], "link": r["link"], "source": r["source"], "published": (r["published"] or "")[:16],
        "date": item_date(r["published"], r["first_seen"]), "first_seen": r["first_seen"][:10],
        "coverage": r["coverage"], "watch_area": r["watch_area"], "event_type": r["event_type"],
        "companies": json.loads(r["companies"] or "[]"), "countries": json.loads(r["countries"] or "[]"),
        "region": r["region"] or "Global", "leaders": json.loads(r["leaders"] or "[]"), "signal": r["signal"] or "neutral",
        "impact": r["impact"], "confidence": r["confidence"], "what_happened": r["what_happened"],
        "so_what": r["so_what"], "action": r["action"]} for r in rows]
    return scanned, items


def write_reports(cfg, scanned_by_day, items, days, note=""):
    """docs/index.html embeds the last `days` days; every day also gets docs/data/<date>.json for the date filter."""
    s = cfg["settings"]
    out = ROOT / s.get("output_dir", "docs")
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    by_day = {}
    for x in items:
        by_day.setdefault(x["date"], []).append(x)
    all_days = sorted(set(by_day) | set(scanned_by_day))
    for d in all_days:  # one small file per day, loaded on demand by the dashboard
        (data_dir / f"{d}.json").write_text(json.dumps({"date": d, "items": by_day.get(d, [])}, ensure_ascii=False), encoding="utf-8")
    keep = {f"{d}.json" for d in all_days} | {"days.json"}
    for f in data_dir.glob("*.json"):  # drop days that fell out of the history window
        if f.name not in keep:
            f.unlink()
    day_list = [{"date": d, "signals": len(by_day.get(d, [])), "scanned": scanned_by_day.get(d, 0)} for d in all_days]
    (data_dir / "days.json").write_text(json.dumps(day_list), encoding="utf-8")

    latest = max(all_days) if all_days else today
    start = (dt.date.fromisoformat(latest) - dt.timedelta(days=days - 1)).isoformat()
    inline_days = [d for d in all_days if d >= start]
    inline = [x for x in items if x["date"] >= start]
    data = {"generated": dt.datetime.now().strftime("%d %b %Y, %H:%M"), "today": today, "latest": latest,
            "default_days": days, "day_list": day_list, "inline_days": inline_days,
            "companies_tracked": len(cfg["companies"]), "leaders": cfg["leaders"], "areas": cfg["watch_areas"],
            "regions": cfg["regions"], "items": inline, "note": note}
    page = TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html_path = out / "index.html"
    html_path.write_text(page, encoding="utf-8")

    min_imp = s.get("min_impact_for_brief", 5)
    recent = sorted(inline, key=lambda x: (-x["impact"], -x["confidence"]))
    lines = [f"# Ore & Steel Radar brief, {today}", "",
             f"{sum(scanned_by_day.get(d, 0) for d in inline_days)} headlines scanned, {len(inline)} relevant signals "
             f"({start} to {latest}).", ""]
    for leader in LEADERS:
        top = [x for x in recent if leader in x["leaders"] and x["impact"] >= min_imp][:5]
        lines.append(f"## {leader}")
        if not top:
            lines.append("- Nothing above the impact threshold.")
        for x in top:
            lines.append(f"- **[{x['impact']}/10 {x['signal']}] {x['what_happened']}** {x['so_what']}"
                         + (f" *Action:* {x['action']}" if x["action"] else "") + f" ([{x['source']}]({x['link']}))")
        lines.append("")
    md_path = out / "brief.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    for a in [x for x in inline if x["impact"] >= s.get("alert_impact", 8) and x["first_seen"] == today]:
        print(f"ALERT [{a['impact']}/10] {', '.join(a['leaders'])}: {a['what_happened']} {a['link']}")
    print(f"report: index.html, brief.md and {len(all_days)} day files written to {out}")
    return html_path, md_path


def report(cfg, db, days=7):
    scanned, items = load_items(db, cfg["settings"].get("history_days", 90))
    return write_reports(cfg, scanned, items, days)


def demo(cfg):
    data = json.loads((ROOT / "demo" / "demo_items.json").read_text(encoding="utf-8"))
    items = sorted(data["items"], key=lambda x: -x["impact"])
    for x in items:
        x["date"] = item_date(x.get("published", ""), x["first_seen"])
    return write_reports(cfg, {items[0]["first_seen"]: data["scanned"]}, items, data.get("days", 7), data.get("note", ""))


# ---------------------------------------------------------------- hosting
def run_pipeline(cfg, days):
    db = connect(str(ROOT / cfg["settings"].get("db_path", "data/radar.db")))
    try:
        collect(cfg, db)
        analyse(cfg, db)
        report(cfg, db, days)
    finally:
        db.close()


def daily_loop(cfg, at, days):
    """Run the pipeline once a day at HH:MM (container local time; set TZ, e.g. TZ=Asia/Bahrain)."""
    hh, mm = (int(x) for x in at.split(":"))
    while True:
        now = dt.datetime.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            run_pipeline(cfg, days)
        except Exception as exc:  # never kill the web server because one run failed
            print(f"daily run failed: {exc}")


def serve(cfg, port, daily, days):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    out = ROOT / cfg["settings"].get("output_dir", "docs")
    if not (out / "index.html").exists():
        run_pipeline(cfg, days)
    if daily:
        threading.Thread(target=daily_loop, args=(cfg, daily, days), daemon=True).start()
        print(f"serve: pipeline scheduled daily at {daily}")
    handler = partial(SimpleHTTPRequestHandler, directory=str(out))
    print(f"serve: dashboard on http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Ore & Steel Radar")
    ap.add_argument("command", choices=["run", "collect", "analyse", "report", "demo", "serve"])
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=None, help="dashboard window in days (default: settings.report_days)")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ap.add_argument("--daily", default=os.getenv("RADAR_DAILY_AT"), help="HH:MM daily refresh time for serve")
    ap.add_argument("--feeds", nargs="*", help="override sources with these RSS URLs or local files (testing)")
    args = ap.parse_args()

    load_env()
    cfg = load_config(args.config)
    days = args.days or cfg["settings"].get("report_days", 7)
    if args.command == "demo":
        demo(cfg)
        return
    if args.command == "serve":
        serve(cfg, args.port, args.daily, days)
        return
    db = connect(str(ROOT / cfg["settings"].get("db_path", "data/radar.db")))
    sources = [("test", f) for f in args.feeds] if args.feeds else None
    if args.command in ("run", "collect"):
        collect(cfg, db, sources)
    if args.command in ("run", "analyse"):
        analyse(cfg, db)
    if args.command in ("run", "report"):
        report(cfg, db, days)


if __name__ == "__main__":
    sys.exit(main())
