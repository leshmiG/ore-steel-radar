# Daily analyst review (run by a scheduled Claude task)

Goal: every morning, turn the radar's new headlines into analyst-quality signals and refresh the
**Decision on the table** section, then publish. Perspective: an iron ore pelletizing and steel company in the
GCC (DR-grade pellets, DRP/DRI, meltshop/EAF, CCM, heavy section mill), importing ore and exporting pellets and
steel by sea through the Gulf.

## Steps

1. Refresh the data: `gh workflow run radar.yml -R leshmiG/ore-steel-radar --ref main`, wait for that run to
   finish (`gh run list -R leshmiG/ore-steel-radar -L 1`), then `git pull`.
   If the run fails, carry on with the data already in the repo and say so in the summary.
2. `python radar.py export --file pending.json --since 2` lists headlines that only have keyword analysis.
3. Read every headline. Write `analysed.json`: a JSON array with one object per headline id:
   - off-topic (gold, copper, lithium, equipment for sale, job ads, unrelated news): `{"id": "...", "relevant": false}`
   - otherwise the same fields the AI pipeline uses:
     `id, watch_area, event_type, companies, countries, region, functions (GOV/FIN/SCM/INN), hashtags,
     signal, impact (1-10), confidence (1-10), what_happened (≤30 words, facts from the headline only),
     so_what (≤35 words: how it affects a pelletizing and steel company, naming the value-chain stage),
     action (≤20 words, starts with a verb: how we can use it)`.
   - Never invent numbers. The same event from several outlets gets the same `what_happened` text, so the
     dashboard shows it once with all outlets.
4. `python radar.py import --file analysed.json`
5. Rewrite `data/agenda.json` (same structure as the current file) with 5 to 8 items for **Decision on the
   table**, covering the last 7 days with today's news first:
   - Think beyond the headline: second-order effects, timing windows, who gains or loses, what competitors
     will do. Each item: `title` (a sharp one-line insight, not a news headline), `focus` (IRON_ORE, PELLETS,
     PELLETIZING, DRP, MELTSHOP, CCM, HSM or STEEL_MARKET), `impact`, `facts` (only facts the radar reported),
     `decision` (the concrete decision for leadership).
   - Also `questions` (3 to ask the team), `also` (1 to 3 smaller items), `skip`, `period`, `as_of` (today,
     YYYY-MM-DD) and `by: "Claude (analyst review)"`.
   - Mark anything that rests only on one keyword-scored headline as "(to confirm)".
6. `python radar.py report`, then `python -m pytest -q tests`.
7. Commit `data/` and `docs/` (do not commit pending.json or analysed.json) and push to `main`.
   Commit message: `Daily analyst review YYYY-MM-DD`.
8. Reply with a 5-line summary: headlines reviewed, kept, and the Decision on the table titles.
