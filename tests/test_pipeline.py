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
    assert "## CFO" in brief and "## CTO" in brief
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
                    "companies": [], "countries": ["Brazil"], "region": "Mars", "leaders": ["CFO", "CPO", "BOSS"],
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
    assert json.loads(r["leaders"]) == ["CFO", "CPO"] and r["impact"] == 10 and r["confidence"] == 7
    assert r["region"] == "Global" and r["signal"] == "tailwind"
    assert db.execute("SELECT COUNT(*) FROM items WHERE analysed = 0").fetchone()[0] == 0


def test_demo_builds(tmp_path):
    cfg = make_cfg(tmp_path)
    html_path, md_path = radar.demo(cfg)
    page = html_path.read_text(encoding="utf-8")
    assert "LKAB" in page and '"2026-10-30"' in page and "competitor_groups" in page
    assert "## CFO" in md_path.read_text(encoding="utf-8")
