#!/usr/bin/env python3
"""Ore & Steel Radar: automatic global intelligence for iron ore, pellets and steel.

Pipeline:  collect (RSS) -> dedupe -> triage (cheap model) -> analyse (Claude) -> store (SQLite)
           -> report (docs/index.html dashboard + docs/brief.md)

Usage:
    python radar.py run                  # collect + analyse + report (the daily job)
    python radar.py collect | analyse | markets | report [--days 7]
    python radar.py serve [--daily 06:30] # host docs/ and refresh daily (Docker / server)
    python radar.py demo                 # dashboard from demo/demo_items.json
    python radar.py backfill --days 120  # one-off: search the last 120 days in two-week windows
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
LEGACY_FUNCTIONS = {"CEO": "GOV", "CFO": "FIN", "SMO": "FIN", "CPO": "SCM", "CTO": "INN"}  # older databases
SIGNALS = ["tailwind", "headwind", "neutral"]
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# A headline must mention one of these (or a tracked company) before any LLM spend.
CORE_TERMS = [
    "iron ore", "pellet", "steel", "dri", "hbi", "direct reduc", "briquett", "eaf", "arc furnace",
    "blast furnace", "scrap", "mine", "mining", "freight", "capesize", "dry bulk", "cbam", "tariff",
    "safeguard", "hydrogen", "metallurg", "coking coal", "rebar", "coil", "hormuz",
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
AREA_FUNCTIONS = {
    "Markets and prices": ["FIN"], "Supply and demand": ["GOV", "FIN"], "Technology and process": ["INN"],
    "Policy and regulation": ["GOV", "FIN"], "Customers and end markets": ["GOV", "SCM"],
    "Suppliers and logistics": ["SCM"], "Research and innovation": ["INN"], "Macro and geopolitics": ["GOV", "FIN", "SCM"],
    "Competitors": ["GOV", "FIN"],
}
FALLBACK_TAGS = {
    "#IronOre": ["iron ore"], "#Pellets": ["pellet"], "#DRI": ["dri", "direct reduc"], "#GreenSteel": ["green steel", "decarbon"],
    "#HydrogenDRI": ["hydrogen"], "#SteelPrices": ["steel price", "rebar", "coil", "hrc"], "#ChinaSteel": ["china"],
    "#IndiaSteel": ["india"], "#GCCSteel": ["saudi", "uae", "oman", "bahrain", "qatar", "gcc"], "#StraitOfHormuz": ["hormuz"],
    "#Freight": ["freight", "capesize"], "#Shipping": ["shipping", "vessel", "tanker"], "#CBAM": ["cbam", "carbon border"],
    "#Tariffs": ["tariff", "safeguard", "duty"], "#Simandou": ["simandou"], "#Earnings": ["profit", "results", "earnings"],
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


# ---------------------------------------------------------------- value-chain focus and function routing
_KW_CACHE = {}


def kw_regex(words):
    """One regex for a keyword list: short words need whole-word matches, longer ones match as word stems."""
    key = tuple(words)
    if key not in _KW_CACHE:
        parts = []
        for w in words:
            w = w.strip().lower()
            if not w:
                continue
            if not re.search(r"[a-z0-9]", w):
                parts.append(re.escape(w))
            elif len(w) <= 4:
                parts.append(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])")
            else:
                parts.append(rf"(?<![a-z0-9]){re.escape(w)}")
        _KW_CACHE[key] = re.compile("|".join(parts) or r"(?!x)x")
    return _KW_CACHE[key]


def focus_of(text, cfg):
    """Value-chain areas a signal touches (iron ore, pellets, pelletizing, DRP/DRI, meltshop, CCM, sections, market)."""
    t = str(text).lower()
    areas = cfg.get("focus_areas") or {}
    order = cfg.get("focus_order") or list(areas)
    return [k for k in order if k in areas and kw_regex(areas[k]["words"]).search(t)]


def rule_functions(text, cfg):
    t = f" {str(text).lower()} "
    return [f for f, words in (cfg.get("function_rules") or {}).items() if f in cfg["functions"] and kw_regex(words).search(t)]


def focus_insight(focus, functions, cfg):
    """Rule-based 'how it affects us' and 'how to use it' for a value-chain area, from config."""
    area = (cfg.get("focus_areas") or {}).get(focus[0]) if focus else None
    if not area:
        return "", ""
    fn = next((f for f in functions if f in area.get("uses", {})), None) or next(iter(area.get("uses", {})), None)
    return area.get("uses", {}).get(fn, ""), area.get("action", "")


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
    cfg.setdefault("functions", {"GOV": {"name": "Governance & Strategy", "remit": ""}, "FIN": {"name": "Finance & Risk", "remit": ""},
                                 "SCM": {"name": "Supply Chain", "remit": ""}, "INN": {"name": "Innovation & Research", "remit": ""}})
    cfg.setdefault("hashtags", [])
    return cfg


def norm_functions(codes, cfg):
    out = []
    for c in codes or []:
        c = LEGACY_FUNCTIONS.get(c, c)
        if c in cfg["functions"] and c not in out:
            out.append(c)
    return out


def norm_tags(tags):
    out = []
    for t in tags or []:
        t = "#" + re.sub(r"[^A-Za-z0-9]", "", str(t).lstrip("#"))
        if len(t) > 2 and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out[:4]


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
        what_happened TEXT, so_what TEXT, action TEXT, analysed_by TEXT, analysed_at TEXT, checks TEXT, tags TEXT);
    CREATE TABLE IF NOT EXISTS item_sources(
        item_id TEXT, name TEXT, domain TEXT, link TEXT, seen_at TEXT, PRIMARY KEY(item_id, domain));
    """)
    cols = {r[1] for r in db.execute("PRAGMA table_info(insights)")}
    for col in ("checks", "tags"):  # upgrade databases created by older versions
        if col not in cols:
            db.execute(f"ALTER TABLE insights ADD COLUMN {col} TEXT")
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


def build_sources(cfg, window=None):
    """(label, url) pairs: one search per topic, companies OR-ed in groups of 4, industry sites, extra feeds.
    window=(after, before) dates replaces the rolling lookback (used by backfill)."""
    when = f"after:{window[0]} before:{window[1]}" if window else f"when:{cfg['settings'].get('lookback', '2d')}"
    out = [(t, GOOGLE_NEWS.format(q=urllib.parse.quote(f"{t} {when}"))) for t in cfg.get("topics", [])]
    for group, names in cfg["competitor_groups"].items():
        for i in range(0, len(names), 4):
            ors = " OR ".join(f'"{n}"' for n in names[i:i + 4])
            q = f"({ors}) (iron ore OR pellets OR steel) {when}"
            out.append((f"companies:{group}", GOOGLE_NEWS.format(q=urllib.parse.quote(q))))
    for d in cfg.get("site_watch", []):
        q = f"site:{d} (iron ore OR pellets OR steel OR DRI OR HBI OR Hormuz) {when}"
        out.append((f"site:{d}", GOOGLE_NEWS.format(q=urllib.parse.quote(q))))
    if not window:
        out += [("feed", f) for f in cfg.get("feeds", [])]
    return out


def domain_of(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def tier_of(domain, cfg):
    """official / tier1 / tier2 / tier3 credibility of a source domain."""
    if not domain:
        return "tier3"
    if re.search(r"\.gov(\.[a-z]{2})?$", domain):
        return "official"
    for tier in ("official", "tier1", "tier2"):
        for d in cfg.get("source_tiers", {}).get(tier, []):
            if domain == d or domain.endswith("." + d):
                return tier
    return "tier3"


def verification(sources):
    """verified: an official source, or 2+ independent outlets incl. a tier 1/2 one, or 3+ outlets.
    single: one credible (tier 1/2) outlet. unconfirmed: only lesser-known outlets."""
    tiers = [x["tier"] for x in sources]
    outlets = len({x["domain"] or x["name"] for x in sources})
    credible = any(t in ("tier1", "tier2") for t in tiers)
    if "official" in tiers or (outlets >= 2 and credible) or outlets >= 3:
        return "verified"
    return "single" if credible else "unconfirmed"


NUM = re.compile(r"\d+(?:[.,]\d+)*")


def figure_check(text, source_text):
    """Return the figures in `text` that do not appear in the source headline/summary."""
    src = source_text.replace(",", "")
    return [n for n in NUM.findall(text or "") if n.replace(",", "") not in src]


def prefilter(text, cfg):
    t = text.lower()
    return any(k in t for k in CORE_TERMS) or any(c.lower() in t for c in cfg["companies"])


class Deduper:
    """Finds the same story told by different outlets: exact title id, or >= 88% similar titles
    that share at least two longer words (an index keeps this fast for thousands of headlines)."""
    def __init__(self, rows):
        self.titles, self.index = {}, {}
        for rid, title in rows:
            self.add(rid, norm_title(title))

    @staticmethod
    def words(key):
        return {w for w in key.split() if len(w) >= 5}

    def add(self, rid, key):
        self.titles[rid] = key
        for w in self.words(key):
            self.index.setdefault(w, set()).add(rid)

    def match(self, rid, key):
        if rid in self.titles:
            return rid
        counts = {}
        for w in self.words(key):
            for other in self.index.get(w, ()):
                counts[other] = counts.get(other, 0) + 1
        for other, n in counts.items():
            if n >= 2 and SequenceMatcher(None, self.titles[other], key).ratio() >= 0.88:
                return other
        return None


def rss_records(url, label):
    """Yield normalised records from one RSS/Atom feed."""
    import feedparser
    feed = feedparser.parse(url)
    feed_title = clean(feed.feed.get("title", "")) if feed.get("feed") else ""
    for e in feed.entries:
        srcd = e.get("source") if isinstance(e.get("source"), dict) else {}
        domain = domain_of(srcd.get("href")) or domain_of(e.get("link"))
        yield {"title": clean(e.get("title", "")), "summary": clean(e.get("summary", ""))[:600], "link": e.get("link", ""),
               "source": clean(srcd.get("title", "")) or feed_title, "domain": "" if "news.google." in domain else domain,
               "published": e.get("published", "") or e.get("updated", "")}


GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_records(query, window=None, timespan="2d", maxrecords=75):
    """Yield records from the GDELT DOC 2.0 API (global news monitoring, free, no key)."""
    params = {"query": f"{query} sourcelang:english", "mode": "artlist", "format": "json", "maxrecords": maxrecords, "sort": "hybridrel"}
    if window:
        params["startdatetime"] = window[0].replace("-", "") + "000000"
        params["enddatetime"] = window[1].replace("-", "") + "000000"
    else:
        params["timespan"] = timespan
    text = fetch_text(f"{GDELT_API}?{urllib.parse.urlencode(params)}")
    data = json.loads(text) if text.strip().startswith("{") else {}
    for a in data.get("articles", []):
        seen = a.get("seendate", "")  # e.g. 20261102T091500Z
        published = f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if re.match(r"\d{8}", seen) else ""
        yield {"title": clean(a.get("title", "")), "summary": "", "link": a.get("url", ""),
               "source": a.get("domain", ""), "domain": domain_of(a.get("url")) or a.get("domain", ""), "published": published}


def collect(cfg, db, sources=None, pause=0.0, dedupe_days=14, window=None):
    """Pull RSS feeds (plus GDELT unless `sources` overrides). One story from many outlets = one item."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dedupe_days)).isoformat()
    seen = Deduper((r["id"], r["title"]) for r in db.execute("SELECT id, title FROM items WHERE first_seen >= ?", (since,)))
    counts = {"added": 0, "dup": 0, "skipped": 0}

    def ingest(rec, label):
        title = rec["title"]
        if not title:
            return
        if not prefilter(f"{title} {rec['summary']}", cfg):
            counts["skipped"] += 1
            return
        key = norm_title(title)
        item_id = hashlib.sha1(key.encode()).hexdigest()[:16]
        outlet = rec["domain"] or rec["source"].lower()
        match = seen.match(item_id, key)
        if not match and db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            match = item_id  # seen on an earlier day, outside the dedupe window
        if match:  # same story from another outlet: record the outlet, count distinct outlets
            db.execute("INSERT OR IGNORE INTO item_sources VALUES(?,?,?,?,?)", (match, rec["source"], outlet, rec["link"], now_iso()))
            db.execute("UPDATE items SET coverage = (SELECT COUNT(*) FROM item_sources WHERE item_id = ?) WHERE id = ?", (match, match))
            counts["dup"] += 1
            return
        db.execute("INSERT INTO items(id,title,link,source,published,summary,query,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                   (item_id, title, rec["link"], rec["source"], rec["published"], rec["summary"], label, now_iso()))
        db.execute("INSERT OR IGNORE INTO item_sources VALUES(?,?,?,?,?)", (item_id, rec["source"], outlet, rec["link"], now_iso()))
        seen.add(item_id, key)
        counts["added"] += 1

    for label, url in (sources or build_sources(cfg, window)):
        if pause:
            time.sleep(pause)
        try:
            for rec in rss_records(url, label):
                try:
                    ingest(rec, label)
                except sqlite3.Error as exc:  # one bad record never drops the rest of the feed
                    print(f"collect: skipped one item from {label} ({exc})")
        except Exception as exc:  # one bad feed never stops the run
            print(f"collect: {label} failed ({exc})")
    if sources is None:
        throttled = 0
        for q in cfg.get("gdelt_queries", []):
            if throttled >= 2:
                print("collect: GDELT is rate-limiting this runner; skipping the remaining GDELT queries")
                break
            for attempt in (1, 2):
                time.sleep(8 if attempt == 1 else 30)  # GDELT asks for at most one request every 5 seconds
                try:
                    for rec in gdelt_records(q, window):
                        try:
                            ingest(rec, f"gdelt:{q}")
                        except sqlite3.Error as exc:
                            print(f"collect: skipped one GDELT item ({exc})")
                    break
                except Exception as exc:
                    if "429" in str(exc) and attempt == 1:
                        continue
                    throttled += "429" in str(exc)
                    print(f"collect: GDELT '{q}' failed ({exc})")
                    break
    db.commit()
    print(f"collect: {counts['added']} new, {counts['dup']} duplicates merged, {counts['skipped']} off-topic skipped")
    return counts["added"]


# ---------------------------------------------------------------- analyse
TRIAGE_PROMPT = """You screen news headlines for a global iron ore, pellet and steel intelligence service.
Keep a headline if it is about iron ore, pellets, DRI/HBI, steelmaking, steel markets or steel companies,
their raw materials, energy, freight, technology, or policy and macro events that move them.
Reply with ONLY a JSON array of the ids to keep, e.g. ["a1","b2"]."""


def analysis_prompt(cfg):
    leaders = "\n".join(f"- {k} ({v['name']}): {v['remit']}" for k, v in cfg["functions"].items())
    chain = " -> ".join(a["name"] for a in (cfg.get("focus_areas") or {}).values()) or "iron ore -> pellets -> DRI -> steel"
    return f"""You are the market intelligence analyst for a global iron ore, pellet and steel radar.
Write every "so what" from this perspective: {cfg['perspective']}

Functions you serve:
{leaders}

Our value chain (map every signal to it): {chain}

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
- functions: only those whose decisions this could change (codes from {", ".join(cfg["functions"])}).
- hashtags: 2 to 4, preferably from {", ".join(cfg["hashtags"])}; add a new CamelCase hashtag only for a clearly new theme.
- region: one value from the regions list.
- what_happened: max 30 words, factual.
- so_what: max 35 words. How this affects an iron ore pelletizing and steel company: name the part of the value
  chain it hits (ore, pellets, pelletizing, DRP/DRI, meltshop, CCM, heavy sections, steel market) and the effect.
- action: max 20 words, starts with a verb: how the company can use this (e.g. renegotiate, hedge, benchmark, approach).

Reply with ONLY a JSON array, one object per headline:
[{{"id": "...", "relevant": true, "watch_area": "...", "event_type": "2-3 words", "companies": ["..."],
   "countries": ["..."], "region": "...", "functions": ["GOV"], "hashtags": ["#IronOre"], "signal": "tailwind", "impact": 7,
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
    """No AI available: route and explain a headline with keyword rules (value-chain focus + function rules)."""
    raw = f"{row['title']} {row['summary']}"
    text = raw.lower()
    if str(row["query"]).startswith("companies:"):
        area = "Competitors"
    else:
        scores = {a: sum(k in text for k in kws) for a, kws in FALLBACK_AREAS.items()}
        area = max(scores, key=scores.get) if max(scores.values()) else "Supply and demand"
    focus = focus_of(raw, cfg)
    functions = rule_functions(raw, cfg) or AREA_FUNCTIONS.get(area, ["GOV"])
    so_what, action = focus_insight(focus, functions, cfg)
    companies = [c for c in cfg["companies"] if kw_regex([c]).search(text)][:3]
    related = kw_regex(["hormuz", "freight", "shipping", "vessel", "tanker", "port", "coking coal", "coal", "natural gas", "lng",
                        "energy price", "tariff", "sanction", "mining", "miner", "metals"]).search(text)
    return {"relevant": bool(focus or companies or related), "watch_area": area, "event_type": "Headline",
            "companies": companies, "countries": [],
            "region": guess_region(text), "functions": functions, "signal": "neutral",
            "hashtags": [t for t, kws in FALLBACK_TAGS.items() if any(k in text for k in kws)][:4] or ["#Steel"],
            "impact": min(10, 3 + 2 * sum(w in text for w in HIGH_WORDS)), "confidence": 4,
            "what_happened": row["title"],
            "so_what": so_what or "Keyword match only; open the source to judge the effect on us.", "action": action}


def save_insight(db, item_id, a, by, cfg, source_text=""):
    region = a.get("region") if a.get("region") in cfg["regions"] else "Global"
    signal = a.get("signal") if a.get("signal") in SIGNALS else "neutral"
    checks = []
    missing = figure_check(a.get("what_happened", ""), source_text) if source_text else []
    if missing:  # the AI wrote a figure the source does not contain
        checks.append(f"Figures not in source: {', '.join(missing[:4])}")
    db.execute("INSERT OR REPLACE INTO insights VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        item_id, int(bool(a.get("relevant"))), a.get("watch_area", ""), a.get("event_type", ""),
        json.dumps(a.get("companies", [])), json.dumps(a.get("countries", [])), region,
        json.dumps(norm_functions(a.get("functions") or a.get("leaders"), cfg)), signal,
        clamp(a.get("impact")), clamp(a.get("confidence")), a.get("what_happened", ""),
        a.get("so_what", ""), a.get("action", ""), by, now_iso(), json.dumps(checks), json.dumps(norm_tags(a.get("hashtags")))))
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


def analyse(cfg, db, max_items=None):
    s = cfg["settings"]
    rows = db.execute("SELECT * FROM items WHERE analysed = 0 ORDER BY first_seen LIMIT ?",
                      (max_items or s.get("max_items_per_run", 500),)).fetchall()
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
                save_insight(db, r["id"], results[r["id"]], model, cfg, f"{r['title']} {r['summary']}")
                done += 1
        db.commit()
    print(f"analyse: {done} of {len(kept)} headlines analysed with {model}")
    return done


# ---------------------------------------------------------------- Strait of Hormuz watch
PORTWATCH_QUERY = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query"
STRAITS = "https://straits.live"


def fetch_text(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ore-steel-radar)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def find_number(obj, *names):
    """First numeric value whose key matches one of `names` (case-insensitive), searched breadth-first."""
    want = [n.lower() for n in names]
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in want and isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def portwatch_transits(chokepoint, since):
    """Daily transits by vessel type from IMF PortWatch (ArcGIS FeatureServer)."""
    params = {"where": f"portid='{chokepoint}' AND date >= timestamp '{since} 00:00:00'",
              "outFields": "date,n_total,n_tanker,n_dry_bulk,n_container,n_general_cargo,n_roro",
              "orderByFields": "date", "resultRecordCount": 2000, "f": "json"}
    data = json.loads(fetch_text(f"{PORTWATCH_QUERY}?{urllib.parse.urlencode(params)}"))
    if "error" in data or not data.get("features"):
        raise ValueError(str(data.get("error") or "no rows"))
    rows = []
    for f in data["features"]:
        a = f["attributes"]
        raw = a.get("date")
        d = dt.datetime.fromtimestamp(raw / 1000, dt.timezone.utc).date().isoformat() if isinstance(raw, (int, float)) else str(raw)[:10]
        other = sum(a.get(k) or 0 for k in ("n_container", "n_general_cargo", "n_roro"))
        rows.append({"date": d, "total": a.get("n_total") or 0, "tanker": a.get("n_tanker") or 0,
                     "dry_bulk": a.get("n_dry_bulk") or 0, "other": other})
    return rows, "IMF PortWatch"


def straits_transits():
    """Fallback: the same IMF PortWatch series mirrored by Straits.live (tanker vs other cargo only)."""
    import csv
    import io
    rows = [{"date": r["date"], "total": int(float(r["n_total"])), "tanker": int(float(r["n_tanker"])),
             "dry_bulk": None, "other": int(float(r["n_cargo"]))}
            for r in csv.DictReader(io.StringIO(fetch_text(f"{STRAITS}/data/transits.csv")))]
    if not rows:
        raise ValueError("no rows")
    return rows, "IMF PortWatch via Straits.live"


def summarise_transits(rows, h):
    rows = sorted(rows, key=lambda r: r["date"])
    base = sorted(r["total"] for r in rows if h["baseline_from"] <= r["date"] <= h["baseline_to"])
    baseline = base[len(base) // 2] if base else h.get("baseline_fallback")
    last7 = rows[-7:]
    ma7 = round(sum(r["total"] for r in last7) / len(last7), 1) if last7 else None
    dry7 = round(sum(r["dry_bulk"] or 0 for r in last7) / len(last7), 1) if last7 and last7[-1]["dry_bulk"] is not None else None
    return {"latest": rows[-1] if rows else None, "ma7": ma7, "dry_bulk_ma7": dry7, "baseline": baseline,
            "pct_of_baseline": round(ma7 / baseline * 100) if ma7 is not None and baseline else None,
            "series": rows[-h.get("chart_days", 180):]}


def fetch_hormuz(cfg):
    """Strait of Hormuz watch: daily transits (IMF PortWatch), vessels holding offshore, war-risk insurance and the
    latest incidents (Straits.live OSINT aggregator). Keeps the last good copy of any part that fails."""
    h = {"chokepoint": "chokepoint6", "baseline_from": "2025-02-28", "baseline_to": "2026-02-27", "chart_days": 180,
         **(cfg.get("hormuz") or {})}
    path = ROOT / "data" / "hormuz.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    out = {"fetched": now_iso(), "baseline_window": [h["baseline_from"], h["baseline_to"]]}
    since = (dt.date.fromisoformat(h["baseline_from"]) - dt.timedelta(days=1)).isoformat()
    for getter in (lambda: portwatch_transits(h["chokepoint"], since), straits_transits):
        try:
            rows, source = getter()
            out.update(summarise_transits(rows, h), transit_source=source)
            break
        except Exception as exc:
            print(f"hormuz: transit source failed ({exc})")
    else:
        for k in ("latest", "ma7", "dry_bulk_ma7", "baseline", "pct_of_baseline", "series", "transit_source"):
            if k in old:
                out[k] = old[k]
    try:
        status = json.loads(fetch_text(f"{STRAITS}/status"))
        out["holding_offshore"] = find_number(status, "strandedOffshore")
        v = status.get("verdict") if isinstance(status, dict) else None
        out["verdict"] = (v.get("short") or v.get("label")) if isinstance(v, dict) else v
    except Exception as exc:
        print(f"hormuz: status failed ({exc})")
        out["holding_offshore"], out["verdict"] = old.get("holding_offshore"), old.get("verdict")
    try:
        ins = json.loads(fetch_text(f"{STRAITS}/api/v1/insurance"))
        out["war_risk_multiple"] = find_number(ins, "warRiskMultiple", "multiple", "multiplier")
        out["insurance_as_of"] = ins.get("asOf") if isinstance(ins, dict) else None
    except Exception as exc:
        print(f"hormuz: insurance failed ({exc})")
        out["war_risk_multiple"], out["insurance_as_of"] = old.get("war_risk_multiple"), old.get("insurance_as_of")
    try:
        import csv
        import io
        ev = list(csv.DictReader(io.StringIO(fetch_text(f"{STRAITS}/data/events.csv"))))
        out["events"] = [{"at": e.get("occurred_at_iso", "")[:16].replace("T", " "), "type": e.get("type", ""),
                          "title": e.get("title", ""), "link": e.get("source_url", "")} for e in ev[:10]]
    except Exception as exc:
        print(f"hormuz: events failed ({exc})")
        out["events"] = old.get("events", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")
    print(f"hormuz: 7-day average {out.get('ma7')} transits/day vs baseline {out.get('baseline')}; {len(out.get('events', []))} incidents")
    return out


def load_agenda():
    """Optional analyst-written 'decisions this week' summary (data/agenda.json)."""
    path = ROOT / "data" / "agenda.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def load_hormuz():
    path = ROOT / "data" / "hormuz.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------- market data
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
STOOQ_CSV = "https://stooq.com/q/d/l/?s={symbol}&i=d"


def fetch_csv(url, timeout=60):
    import csv
    import io
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ore-steel-radar)"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(10)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}", rows[1][0] if rows[1] else ""):
        raise ValueError("not a dated CSV")
    return rows


def yahoo_closes(ticker):
    """Daily closes for the last 6 months from Yahoo Finance's chart data (fallback when Stooq has no CSV)."""
    data = json.loads(fetch_text(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=6mo&interval=1d"))
    res = data["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    pts = [[dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), round(c, 4)]
           for t, c in zip(res["timestamp"], closes) if c is not None]
    if len(pts) < 2:
        raise ValueError("no prices")
    return pts[-130:]


def pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b else None


def fetch_markets(cfg):
    """Commodity series (FRED) and competitor share prices (Stooq). Any source that fails is skipped;
    the last good copy in data/markets.json is kept for it."""
    path = ROOT / "data" / "markets.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    old_c = {x["id"]: x for x in old.get("commodities", [])}
    old_e = {x["symbol"]: x for x in old.get("equities", [])}
    out = {"fetched": now_iso(), "commodities": [], "equities": []}
    m = cfg.get("markets", {})
    for c in m.get("commodities", []):
        try:
            rows = fetch_csv(FRED_CSV.format(id=c["id"]))
            pts = [[r[0], float(r[1])] for r in rows[1:] if len(r) > 1 and r[1] not in ("", ".")]
            daily = len(pts) > 1 and (dt.date.fromisoformat(pts[-1][0]) - dt.date.fromisoformat(pts[-2][0])).days < 7
            pts = pts[-260:] if daily else pts[-36:]
            back = 252 if daily else 12
            out["commodities"].append({**c, "freq": "daily" if daily else "monthly", "points": pts, "latest": pts[-1][1],
                                       "date": pts[-1][0], "change_pct": pct(pts[-1][1], pts[-2][1]),
                                       "yoy_pct": pct(pts[-1][1], pts[-1 - back][1]) if len(pts) > back else None})
        except Exception as exc:
            print(f"markets: {c['id']} unavailable ({exc})")
            if c["id"] in old_c:
                out["commodities"].append(old_c[c["id"]])
    for e in m.get("equities", []):
        try:
            try:
                rows = fetch_csv(STOOQ_CSV.format(symbol=e["symbol"]))
                head = [h.lower() for h in rows[0]]
                ci = head.index("close")
                pts = [[r[0], float(r[ci])] for r in rows[1:] if len(r) > ci and r[ci]][-130:]
            except Exception:
                pts = yahoo_closes(e["symbol"].split(".")[0].upper())
            last = pts[-1][1]
            out["equities"].append({**e, "points": pts, "latest": last, "date": pts[-1][0],
                                    "change_1m_pct": pct(last, pts[-22][1]) if len(pts) > 22 else None,
                                    "change_3m_pct": pct(last, pts[-64][1]) if len(pts) > 64 else None})
        except Exception as exc:
            print(f"markets: {e['symbol']} unavailable ({exc})")
            if e["symbol"] in old_e:
                out["equities"].append(old_e[e["symbol"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out), encoding="utf-8")
    print(f"markets: {len(out['commodities'])} commodity series, {len(out['equities'])} share prices")
    return out


def load_markets():
    path = ROOT / "data" / "markets.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


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


def load_items(db, history_days, cfg):
    """All relevant signals first seen in the last `history_days`, plus headlines scanned per day."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=history_days)).isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=history_days)).isoformat()
    scanned = {}
    for r in db.execute("SELECT published, first_seen FROM items WHERE first_seen >= ?", (since,)):
        d = item_date(r["published"], r["first_seen"])
        if d >= cutoff:
            scanned[d] = scanned.get(d, 0) + 1
    rows = db.execute("""SELECT i.*, n.* FROM items i JOIN insights n ON n.item_id = i.id
                         WHERE i.first_seen >= ? AND n.relevant = 1 ORDER BY n.impact DESC, i.first_seen DESC""",
                      (since,)).fetchall()
    srcs = {}
    for s_ in db.execute("SELECT * FROM item_sources WHERE seen_at >= ? ORDER BY seen_at", (since,)):
        srcs.setdefault(s_["item_id"], []).append(s_)
    items = merge_same_story([finalise(r, srcs.get(r["id"], []), cfg) for r in rows], cfg)
    return scanned, [x for x in items if x["date"] >= cutoff]


def merge_same_story(items, cfg):
    """An analyst may describe several differently worded headlines as the same event. Show that event once,
    with every outlet that reported it (which also upgrades its verification)."""
    out, by_key = [], {}
    for x in items:
        key = x["what_happened"].strip().lower() if x.get("by") not in ("fallback", "") else None
        if key and key in by_key:
            m = by_key[key]
            seen = {(s_["domain"], s_["link"]) for s_ in m["sources"]}
            m["sources"] += [s_ for s_ in x["sources"] if (s_["domain"], s_["link"]) not in seen]
            m["outlets"] = len({s_["domain"] or s_["name"] for s_ in m["sources"]})
            m["verification"] = verification(m["sources"])
            m["sources"] = m["sources"][:8]
            continue
        if key:
            by_key[key] = x
        out.append(x)
    return out


def finalise(r, source_rows, cfg):
    """Attach outlets, verification status and checked confidence to one signal."""
    sources = [{"name": s_["name"], "domain": s_["domain"], "link": s_["link"], "tier": tier_of(s_["domain"], cfg)}
               for s_ in source_rows] or [{"name": r["source"], "domain": domain_of(r["link"]), "link": r["link"],
                                           "tier": tier_of(domain_of(r["link"]), cfg)}]
    rank = {"official": 0, "tier1": 1, "tier2": 2, "tier3": 3}
    sources.sort(key=lambda x: rank[x["tier"]])
    status = verification(sources)
    checks = json.loads(r["checks"] or "[]") if "checks" in r.keys() else []
    conf = r["confidence"]
    conf = min(conf, 5) if status == "unconfirmed" else min(conf, 7) if status == "single" else conf
    if checks:
        conf = max(1, conf - 2)
    text = f"{r['title']} {r['summary']} {r['what_happened']}"
    leaders = norm_functions(json.loads(r["leaders"] or "[]"), cfg)
    return {"focus": focus_of(text, cfg),
        "title": r["title"], "link": r["link"], "source": r["source"], "published": (r["published"] or "")[:16],
        "date": item_date(r["published"], r["first_seen"]), "first_seen": r["first_seen"][:10],
        "coverage": r["coverage"], "watch_area": r["watch_area"], "event_type": r["event_type"],
        "companies": json.loads(r["companies"] or "[]"), "countries": json.loads(r["countries"] or "[]"),
        "region": r["region"] or "Global", "leaders": leaders, "by": r["analysed_by"] or "",
        "tags": norm_tags(json.loads(r["tags"] or "[]") if "tags" in r.keys() else []), "signal": r["signal"] or "neutral",
        "impact": r["impact"], "confidence": r["confidence"], "what_happened": r["what_happened"],
        "so_what": r["so_what"], "action": r["action"], "sources": sources[:8], "outlets": len(sources),
        "verification": status, "checks": checks, "confidence": conf}


def source_catalog(cfg):
    """Everything the radar reads, for the 'Sources we monitor' section."""
    m = cfg.get("markets", {})
    return {"sites": sorted(({"domain": d, "tier": tier_of(d, cfg)} for d in cfg.get("site_watch", [])),
                            key=lambda x: (["official", "tier1", "tier2", "tier3"].index(x["tier"]), x["domain"])),
            "topics": cfg.get("topics", []), "gdelt": cfg.get("gdelt_queries", []), "feeds": cfg.get("feeds", []),
            "companies": len(cfg["companies"]), "company_searches": sum(-(-len(v) // 4) for v in cfg["competitor_groups"].values()),
            "commodities": [c["name"] for c in m.get("commodities", [])], "equities": [e["name"] for e in m.get("equities", [])]}


def write_reports(cfg, scanned_by_day, items, days, note="", markets=None, hormuz=None):
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
            "companies_tracked": len(cfg["companies"]), "competitor_groups": cfg["competitor_groups"], "functions": cfg["functions"],
            "focus_areas": {k: {"name": v["name"], "uses": v.get("uses", {}), "action": v.get("action", "")} for k, v in (cfg.get("focus_areas") or {}).items()},
            "focus_order": list((cfg.get("focus_areas") or {})), "pinned_hashtags": cfg.get("pinned_hashtags", []), "catalog": source_catalog(cfg), "areas": cfg["watch_areas"],
            "regions": cfg["regions"], "items": inline, "note": note, "markets": markets if markets is not None else load_markets(),
            "hormuz": hormuz if hormuz is not None else load_hormuz(), "agenda": load_agenda()}
    page = TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html_path = out / "index.html"
    html_path.write_text(page, encoding="utf-8")

    min_imp = s.get("min_impact_for_brief", 5)
    recent = sorted(inline, key=lambda x: (-x["impact"], -x["confidence"]))
    lines = [f"# Ore & Steel Radar brief, {today}", "",
             f"{sum(scanned_by_day.get(d, 0) for d in inline_days)} headlines scanned, {len(inline)} relevant signals "
             f"({start} to {latest}).", ""]
    for code, fn in cfg["functions"].items():
        top = [x for x in recent if code in x["leaders"] and x["impact"] >= min_imp][:5]
        lines.append(f"## {fn['name']}")
        if not top:
            lines.append("- Nothing above the impact threshold.")
        for x in top:
            lines.append(f"- **[{x['impact']}/10] {x['what_happened']}** {x['so_what']}"
                         + (f" *Action:* {x['action']}" if x["action"] else "") + f" ([{x['source']}]({x['link']}))")
        lines.append("")
    md_path = out / "brief.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    for a in [x for x in inline if x["impact"] >= s.get("alert_impact", 8) and x["first_seen"] == today]:
        print(f"ALERT [{a['impact']}/10] {', '.join(a['leaders'])}: {a['what_happened']} {a['link']}")
    print(f"report: index.html, brief.md and {len(all_days)} day files written to {out}")
    return html_path, md_path


def report(cfg, db, days=7):
    scanned, items = load_items(db, cfg["settings"].get("history_days", 90), cfg)
    return write_reports(cfg, scanned, items, days)


def demo(cfg):
    data = json.loads((ROOT / "demo" / "demo_items.json").read_text(encoding="utf-8"))
    items = sorted(data["items"], key=lambda x: -x["impact"])
    for x in items:
        x["date"] = item_date(x.get("published", ""), x["first_seen"])
        srcs = x.get("sources") or [{"name": x["source"], "link": x["link"]}]
        x["sources"] = sorted([{**s_, "domain": domain_of(s_["link"]), "tier": tier_of(domain_of(s_["link"]), cfg)} for s_ in srcs],
                              key=lambda s_: ["official", "tier1", "tier2", "tier3"].index(s_["tier"]))
        x["outlets"] = len(x["sources"])
        x["coverage"] = x["outlets"]
        x["leaders"] = norm_functions(x.get("leaders"), cfg)
        x["tags"] = norm_tags(x.get("tags"))
        x["verification"] = verification(x["sources"])
        x.setdefault("checks", [])
        x["confidence"] = min(x["confidence"], 5) if x["verification"] == "unconfirmed" else min(x["confidence"], 7) if x["verification"] == "single" else x["confidence"]
    return write_reports(cfg, {items[0]["first_seen"]: data["scanned"]}, items, data.get("days", 7), data.get("note", ""),
                         data.get("markets", {}), data.get("hormuz", {}))


# ---------------------------------------------------------------- backfill
def backfill(cfg, db, days=120, step=14, max_items=4000):
    """One-off history load: run every search over two-week windows covering the last `days` days."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    total = 0
    while start < end:
        stop = min(start + dt.timedelta(days=step), end + dt.timedelta(days=1))
        print(f"backfill: {start} to {stop}")
        total += collect(cfg, db, None, pause=0.7, dedupe_days=days + 14, window=(start.isoformat(), stop.isoformat()))
        start = stop
    print(f"backfill: {total} headlines collected over {days} days")
    analyse(cfg, db, max_items)


# ---------------------------------------------------------------- hosting
def run_pipeline(cfg, days):
    db = connect(str(ROOT / cfg["settings"].get("db_path", "data/radar.db")))
    try:
        collect(cfg, db)
        analyse(cfg, db)
        for fetch in (fetch_markets, fetch_hormuz):
            try:
                fetch(cfg)
            except Exception as exc:
                print(f"{fetch.__name__}: skipped ({exc})")
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


# ---------------------------------------------------------------- analyses from outside the pipeline
def import_insights(cfg, db, path, by="claude"):
    """Load analyses written outside the pipeline (e.g. by Claude in a chat session) as a JSON array of
    {"id": ..., <same fields as the AI analysis>}. Runs the same figure check as the API path."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    n = 0
    for a in rows:
        r = db.execute("SELECT title, summary FROM items WHERE id = ?", (a.get("id"),)).fetchone()
        if not r:
            continue
        if a.get("relevant", True) is False:
            save_insight(db, a["id"], {"relevant": False}, by, cfg)
        else:
            src = f"{r['title']} {r['summary']}"
            if figure_check(a.get("what_happened", ""), src):  # shared summary quotes figures this outlet did not: use its own headline
                a = {**a, "what_happened": r["title"]}
            save_insight(db, a["id"], {"relevant": True, **a}, by, cfg, src)
        n += 1
    db.commit()
    print(f"import: {n} insights saved from {path}")
    return n


def reclassify(cfg, db):
    """Re-run the keyword rules over every signal that was analysed without AI (after a config change)."""
    rows = db.execute("SELECT i.* FROM items i JOIN insights n ON n.item_id = i.id WHERE n.analysed_by = 'fallback'").fetchall()
    for r in rows:
        save_insight(db, r["id"], fallback_analysis(r, cfg), "fallback", cfg)
    db.commit()
    print(f"reclassify: {len(rows)} keyword-only signals re-routed")


def export_pending(cfg, db, path, since_days=1):
    """Write headlines that still have keyword-only analysis to a JSON file for review by an analyst or Claude."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).isoformat()
    rows = db.execute("""SELECT i.id, i.title, i.summary, i.source FROM items i JOIN insights n ON n.item_id = i.id
                         WHERE n.analysed_by = 'fallback' AND n.relevant = 1 AND i.first_seen >= ? ORDER BY i.first_seen""", (since,)).fetchall()
    Path(path).write_text(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"export: {len(rows)} headlines written to {path}")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Ore & Steel Radar")
    ap.add_argument("command", choices=["run", "collect", "analyse", "markets", "report", "demo", "serve", "backfill",
                                         "import", "reclassify", "export"])
    ap.add_argument("--file", help="JSON file for import / export")
    ap.add_argument("--since", type=int, default=1, help="days of headlines to export")
    ap.add_argument("--max", type=int, default=4000, help="max headlines analysed by backfill")
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
    if args.command == "backfill":
        backfill(cfg, db, args.days or 120, max_items=args.max)
        report(cfg, db, cfg["settings"].get("report_days", 7))
        return
    if args.command in ("import", "reclassify", "export"):
        if args.command == "import":
            import_insights(cfg, db, args.file)
        elif args.command == "reclassify":
            reclassify(cfg, db)
        else:
            export_pending(cfg, db, args.file or "pending.json", args.since)
            return
        report(cfg, db, days)
        return
    sources = [("test", f) for f in args.feeds] if args.feeds else None
    if args.command in ("run", "collect"):
        collect(cfg, db, sources)
    if args.command in ("run", "analyse"):
        analyse(cfg, db)
    if args.command in ("run", "markets"):
        for fetch in (fetch_markets, fetch_hormuz):
            try:
                fetch(cfg)
            except Exception as exc:  # market and shipping data are a bonus; never fail the run for them
                print(f"{fetch.__name__}: skipped ({exc})")
    if args.command in ("run", "report"):
        report(cfg, db, days)


if __name__ == "__main__":
    sys.exit(main())
