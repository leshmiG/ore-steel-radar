"""End-to-end tests with a local RSS file and a fake Claude client (no network, no API key)."""
import json
import types
from pathlib import Path

import radar

HERE = Path(__file__).parent
FEED = str(HERE / "fixtures" / "sample_feed.xml")


def make_cfg(tmp_path):
    cfg = radar.load_config(radar.ROOT / "config.yaml")
    cfg["settings"]["output_dir"] = str(tmp_path / "docs")
    return cfg


def test_sources_cover_topics_and_all_companies(tmp_path):
    cfg = make_cfg(tmp_path)
    sources = radar.build_sources(cfg)
    company_queries = [u for label, u in sources if label.startswith("companies:")]
    assert len(cfg["companies"]) > 60
    assert len(company_queries) == sum(-(-len(v) // 4) for v in cfg["competitor_groups"].values())
    assert len(sources) >= len(cfg["topics"]) + len(company_queries)


def test_collect_dedupes_and_filters(tmp_path):
    cfg = make_cfg(tmp_path)
    db = radar.connect(str(tmp_path / "radar.db"))
    assert radar.collect(cfg, db, [("test", FEED)]) == 3   # 5 items: 1 duplicate merged, 1 off-topic skipped
    assert db.execute("SELECT coverage FROM items WHERE title LIKE 'Vale%'").fetchone()["coverage"] == 2
    assert radar.collect(cfg, db, [("test", FEED)]) == 0   # second run adds nothing


def test_fallback_analysis_and_report(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = make_cfg(tmp_path)
    db = radar.connect(str(tmp_path / "radar.db"))
    radar.collect(cfg, db, [("test", FEED)])
    assert radar.analyse(cfg, db) == 3
    html_path, md_path = radar.report(cfg, db, 7)
    page = html_path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "Port strike" in page and "__DATA__" not in page
    brief = md_path.read_text(encoding="utf-8")
    assert "## Finance & Risk" in brief and "## Innovation & Research" in brief
    data_dir = Path(cfg["settings"]["output_dir"]) / "data"
    days = json.loads((data_dir / "days.json").read_text(encoding="utf-8"))
    assert days and all((data_dir / f"{d['date']}.json").exists() for d in days)
    assert sum(len(json.loads((data_dir / f"{d['date']}.json").read_text())["items"]) for d in days) == 3


def test_item_date_uses_publish_date():
    assert radar.item_date("Fri, 30 Oct 2026 10:00:00 GMT", "2026-11-02T01:00:00+00:00") == "2026-10-30"
    assert radar.item_date("2026-10-13", "2026-11-02T01:00:00+00:00") == "2026-10-13"
    assert radar.item_date("", "2026-11-02T01:00:00+00:00") == "2026-11-02"


def test_triage_then_analysis(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    db = radar.connect(str(tmp_path / "radar.db"))
    radar.collect(cfg, db, [("test", FEED)])
    calls = {"triage": 0, "analyse": 0}

    class FakeMessages:
        def create(self, **kw):
            payload = json.loads(kw["messages"][0]["content"])
            if kw["system"] == radar.TRIAGE_PROMPT:
                calls["triage"] += 1
                keep = [p["id"] for p in payload if "CBAM" not in p["headline"]]  # drop one item
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(keep))])
            calls["analyse"] += 1
            assert "Use ONLY the headline" in kw["system"]
            out = [{"id": p["id"], "relevant": True, "watch_area": "Suppliers and logistics", "event_type": "Test",
                    "companies": [], "countries": ["Brazil"], "region": "Mars", "functions": ["FIN", "SCM", "BOSS"],
                    "hashtags": ["IronOre", "#Strait of Hormuz", "#IronOre"],
                    "signal": "tailwind", "impact": 15, "confidence": "7", "what_happened": "x", "so_what": "y",
                    "action": "z"} for p in payload]
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="Result:\n" + json.dumps(out))])

    fake = types.SimpleNamespace(messages=FakeMessages())
    monkeypatch.setattr(radar, "llm_clients", lambda cfg: (fake, "fake-model", "fake-triage"))
    assert radar.analyse(cfg, db) == 2
    assert calls == {"triage": 1, "analyse": 1}
    rows = db.execute("SELECT * FROM insights WHERE relevant = 1").fetchall()
    assert len(rows) == 2
    r = rows[0]
    assert json.loads(r["leaders"]) == ["FIN", "SCM"] and r["impact"] == 10 and r["confidence"] == 7
    assert json.loads(r["tags"]) == ["#IronOre", "#StraitofHormuz"]
    assert r["region"] == "Global" and r["signal"] == "tailwind"
    assert db.execute("SELECT COUNT(*) FROM items WHERE analysed = 0").fetchone()[0] == 0


def test_demo_builds(tmp_path):
    cfg = make_cfg(tmp_path)
    html_path, md_path = radar.demo(cfg)
    page = html_path.read_text(encoding="utf-8")
    assert "LKAB" in page and '"2026-10-30"' in page and "competitor_groups" in page
    assert "## Supply Chain" in md_path.read_text(encoding="utf-8")


def test_source_tiers_and_verification(tmp_path):
    cfg = make_cfg(tmp_path)
    assert radar.tier_of("reuters.com", cfg) == "tier1"
    assert radar.tier_of("news.mining.com", cfg) == "tier2"
    assert radar.tier_of("lkab.com", cfg) == "official"
    assert radar.tier_of("commerce.gov", cfg) == "official"
    assert radar.tier_of("randomblog.net", cfg) == "tier3"
    src = lambda *t: [{"domain": f"d{i}.com", "name": f"d{i}", "tier": x} for i, x in enumerate(t)]
    assert radar.verification(src("official")) == "verified"
    assert radar.verification(src("tier1", "tier3")) == "verified"
    assert radar.verification(src("tier3", "tier3", "tier3")) == "verified"
    assert radar.verification(src("tier2")) == "single"
    assert radar.verification(src("tier3")) == "unconfirmed"
    assert radar.verification(src("tier3", "tier3")) == "unconfirmed"


def test_figure_check_flags_invented_numbers():
    source = "Vale Q3 iron ore output falls 6% to 88.4 Mt, net profit down 53%"
    assert radar.figure_check("Output fell 6% to 88.4 Mt.", source) == []
    assert radar.figure_check("Output fell 9% to 88.4 Mt.", source) == ["9"]
    assert radar.figure_check("Sales of 1,200 t", "sales of 1200 t") == []


def test_collect_counts_distinct_outlets(tmp_path):
    cfg = make_cfg(tmp_path)
    db = radar.connect(str(tmp_path / "radar.db"))
    radar.collect(cfg, db, [("test", FEED)])
    radar.collect(cfg, db, [("test", FEED)])            # same outlets again must not inflate coverage
    row = db.execute("SELECT id, coverage FROM items WHERE title LIKE 'Vale%'").fetchone()
    assert row["coverage"] == 2
    domains = {r["domain"] for r in db.execute("SELECT domain FROM item_sources WHERE item_id = ?", (row["id"],))}
    assert "reuters.com" in domains


def test_markets_parse_and_keep_last_good(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(radar, "ROOT", tmp_path)
    monthly = [["observation_date", "X"]] + [[f"20{24 + m // 12}-{m % 12 + 1:02d}-01", str(100 + m)] for m in range(30)]
    daily = [["Date", "Open", "High", "Low", "Close", "Volume"]] + [[f"2026-{6 + d // 28:02d}-{d % 28 + 1:02d}", "1", "1", "1", str(10 + d * 0.1), "5"] for d in range(120)]
    cfg["markets"] = {"commodities": [{"id": "PIORECRUSDM", "name": "Iron ore", "unit": "$/t", "note": ""}],
                      "equities": [{"symbol": "vale.us", "name": "Vale"}]}
    monkeypatch.setattr(radar, "fetch_csv", lambda url: monthly if "fred" in url else daily)
    out = radar.fetch_markets(cfg)
    c, e = out["commodities"][0], out["equities"][0]
    assert c["freq"] == "monthly" and c["latest"] == 129 and c["change_pct"] == round((129 / 128 - 1) * 100, 1)
    assert c["yoy_pct"] == round((129 / 117 - 1) * 100, 1)
    assert e["change_1m_pct"] is not None and e["latest"] == round(10 + 119 * 0.1, 10)
    def boom(url):
        raise OSError("offline")
    monkeypatch.setattr(radar, "fetch_csv", boom)
    again = radar.fetch_markets(cfg)                    # sources down: keep the last good values
    assert again["commodities"][0]["latest"] == 129 and again["equities"][0]["name"] == "Vale"


def test_backfill_windows_and_dedupe(tmp_path):
    cfg = make_cfg(tmp_path)
    src = radar.build_sources(cfg, ("2026-07-01", "2026-07-15"))
    assert all("after%3A2026-07-01" in u and "when%3A" not in u for _, u in src)
    assert not any(label == "feed" for label, _ in src)            # RSS feeds have no date window
    assert any("Hormuz" in label for label, _ in src)
    d = radar.Deduper([("a", "Vale cuts iron ore output guidance for 2026 - Reuters")])
    assert d.match("x", radar.norm_title("Vale cuts iron ore output guidance for 2026 - Mining.com")) == "a"
    assert d.match("y", radar.norm_title("China steel output rises in September")) is None


def test_legacy_functions_map_to_new_tabs(tmp_path):
    cfg = make_cfg(tmp_path)
    assert radar.norm_functions(["CEO", "CFO", "SMO", "CPO", "CTO", "XYZ"], cfg) == ["GOV", "FIN", "SCM", "INN"]


def test_hormuz_portwatch_down_falls_back_to_mirror(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(radar, "ROOT", tmp_path)
    days = [f"2025-{m:02d}-{d:02d}" for m in range(3, 13) for d in (1, 15)] + [f"2026-10-{d:02d}" for d in range(19, 26)]
    rows = "date,n_total,n_tanker,n_cargo\n" + "\n".join(
        f"{d},{80 if d < '2026' else 3},{40 if d < '2026' else 1},{40 if d < '2026' else 2}" for d in days)

    def fake(url, timeout=30):
        if "arcgis" in url:
            return '{"error":{"code":400,"message":"Cannot perform query."}}'
        if url.endswith("/data/transits.csv"):
            return rows
        if url.endswith("/status"):
            return '{"verdict":{"short":"closed"},"ships":{"strandedOffshore":124,"stranded":862}}'
        if "insurance" in url:
            return '{"asOf":"2026-10-07","data":{"warRiskMultiple":32}}'
        if url.endswith("/data/events.csv"):
            return 'occurred_at_iso,type,title,source_url\n2026-11-02T08:43:06.000Z,strike,"LNG tanker attacked",https://example.com/a\n'
        raise OSError(url)

    monkeypatch.setattr(radar, "fetch_text", fake)
    h = radar.fetch_hormuz(cfg)
    assert h["transit_source"] == "IMF PortWatch via Straits.live"
    assert h["baseline"] == 80 and h["ma7"] == 3.0 and h["pct_of_baseline"] == 4
    assert h["holding_offshore"] == 124 and h["war_risk_multiple"] == 32 and h["verdict"] == "closed"
    assert h["events"][0]["type"] == "strike" and h["events"][0]["at"] == "2026-11-02 08:43"
    monkeypatch.setattr(radar, "fetch_text", lambda url, timeout=30: (_ for _ in ()).throw(OSError("offline")))
    again = radar.fetch_hormuz(cfg)                       # everything down: keep last good values
    assert again["ma7"] == 3.0 and again["holding_offshore"] == 124 and again["events"]


def test_portwatch_parses_vessel_types(monkeypatch):
    feat = {"features": [{"attributes": {"date": 1761350400000, "n_total": 5, "n_tanker": 2, "n_dry_bulk": 1,
                                         "n_container": 1, "n_general_cargo": 1, "n_roro": 0}}]}
    monkeypatch.setattr(radar, "fetch_text", lambda url, timeout=30: json.dumps(feat))
    rows, src = radar.portwatch_transits("chokepoint6", "2025-01-01")
    assert src == "IMF PortWatch" and rows == [{"date": "2025-10-25", "total": 5, "tanker": 2, "dry_bulk": 1, "other": 2}]


def test_gdelt_records(monkeypatch):
    payload = {"articles": [{"url": "https://www.mining.com/x", "title": "Iron ore slips", "seendate": "20261102T091500Z", "domain": "mining.com"}]}
    monkeypatch.setattr(radar, "fetch_text", lambda url, timeout=30: json.dumps(payload))
    recs = list(radar.gdelt_records('"iron ore"'))
    assert recs[0]["published"] == "2026-11-02" and recs[0]["domain"] == "mining.com" and recs[0]["title"] == "Iron ore slips"


def test_old_item_outside_dedupe_window_is_merged_not_crashing(tmp_path):
    cfg = make_cfg(tmp_path)
    db = radar.connect(str(tmp_path / "radar.db"))
    radar.collect(cfg, db, [("test", FEED)])
    db.execute("UPDATE items SET first_seen = '2026-01-01T00:00:00+00:00'")   # older than the 14-day window
    db.commit()
    # exact repeats of old headlines must not crash (UNIQUE) or be re-added; only the reworded copy is new
    assert radar.collect(cfg, db, [("test", FEED)]) == 1
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4


def test_equities_fall_back_to_yahoo(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(radar, "ROOT", tmp_path)
    cfg["markets"] = {"commodities": [], "equities": [{"symbol": "vale.us", "name": "Vale"}]}
    monkeypatch.setattr(radar, "fetch_csv", lambda url: (_ for _ in ()).throw(ValueError("not a dated CSV")))
    ts = [1756684800 + 86400 * i for i in range(70)]
    chart = {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [10 + i * 0.1 for i in range(70)]}]}}]}}
    monkeypatch.setattr(radar, "fetch_text", lambda url, timeout=30: json.dumps(chart) if "VALE" in url else "")
    e = radar.fetch_markets(cfg)["equities"][0]
    assert e["name"] == "Vale" and e["change_1m_pct"] is not None and e["change_3m_pct"] is not None


def test_focus_and_function_rules():
    cfg = radar.load_config(radar.ROOT / "config.yaml")
    assert radar.focus_of("Saudi Arabia opens anti-dumping probe into hot-rolled steel sections", cfg)[0] == "HSM"
    assert "DRP" in radar.focus_of("Midrex chosen for direct reduction at new DRI plant", cfg)
    assert "MELTSHOP" in radar.focus_of("New electric arc furnace meltshop starts", cfg)
    assert radar.focus_of("Company drives growth", cfg) == []          # 'dri' must not match 'drives'
    assert "SCM" in radar.rule_functions("Freight rates surge as vessels avoid Hormuz", cfg)
