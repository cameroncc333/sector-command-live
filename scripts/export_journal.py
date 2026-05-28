"""Export last 30 journal decisions to docs/journal.json for the GitHub Pages dashboard."""
import sqlite3
import json
import os

db = "data/sector_command.db"
if os.path.exists(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 30").fetchall()
    con.close()
    data = []
    for r in rows:
        d = dict(r)
        try:
            d["why_trace"] = json.loads(d.get("why_trace") or "[]")
        except Exception:
            pass
        data.append(d)
    os.makedirs("docs", exist_ok=True)
    with open("docs/journal.json", "w") as f:
        json.dump(data, f)
    print(f"[dashboard] exported {len(data)} journal entries")
else:
    print("[dashboard] no journal DB yet")
    os.makedirs("docs", exist_ok=True)
    with open("docs/journal.json", "w") as f:
        json.dump([], f)
