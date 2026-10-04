# -*- coding: ascii -*-
"""E2E: parse-extract module full chain over the REAL HTTP routing stack.

Uses fastapi.testclient.TestClient, which drives the whole app (middleware ->
router -> Depends(get_db) -> aiosqlite) inside one portal/event loop, so the
query-string / multipart / path-param binding that the browser actually hits
is exercised for real (the direct-call unit tests bypass that layer).

app.db.DB_PATH is overridden to a temp file BEFORE the app starts, so the
production database (backend/data/scheme_assistant.db) is never touched.
Read-back verification uses a separate read-only sqlite3 handle on that same
temp file (WAL-safe), never the app's connection.

Chain: project -> scheme -> outline parse (F1 linkage) -> bad project_id 404
-> save-as-outline (F1 backfill) -> manual correction UPSERT on a
never-extracted item (F2) -> GET /results shows it -> delete project
cascades the outline row. Output pure ASCII. Exit 0 = PASS.
"""
import io
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.db as dbmod
dbmod.DB_PATH = Path(tempfile.mkdtemp(prefix="e2e_lk_")) / "e2e.db"

from fastapi.testclient import TestClient
from app.main import app

OUTLINE = "\n".join([
    "\u7b2c\u4e00\u7ae0 \u5de5\u7a0b\u6982\u51b5",
    "\u7b2c\u4e8c\u7ae0 \u7f16\u5236\u4f9d\u636e",
    "\u7b2c\u4e09\u7ae0 \u65bd\u5de5\u90e8\u7f72",
    "\u7b2c\u56db\u7ae0 \u4e3b\u8981\u65bd\u5de5\u5de5\u827a\u6280\u672f",
    "\u7b2c\u4e94\u7ae0 \u5b89\u5168\u4fdd\u8bc1\u63aa\u65bd",
    "\u7b2c\u516d\u7ae0 \u8ba1\u7b97\u4e66",
]).encode("utf-8")


def main():
    ok = []
    with TestClient(app) as c:
        con = sqlite3.connect(str(dbmod.DB_PATH), check_same_thread=False)

        # 1. project
        r = c.post("/api/v1/projects", json={"name": "E2E Linkage Project"})
        assert r.status_code == 200, (r.status_code, r.text)
        pid = r.json()["id"]
        ok.append("1 project created")

        # 1b. scheme via public API (save-as-outline + bid-analysis pid resolve)
        r = c.post(f"/api/v1/projects/{pid}/schemes", json={"name": "S-E2E"})
        assert r.status_code == 200, (r.status_code, r.text)
        sid = r.json()["id"]
        ok.append("1b scheme created")

        # 2. parse with project_id -> F1 linkage persisted (real query binding)
        r = c.post("/api/v1/upload-outline/parse",
                   params={"project_id": pid},
                   files={"file": ("outline.txt", io.BytesIO(OUTLINE),
                                   "text/plain")})
        assert r.status_code == 200, (r.status_code, r.text)
        uid = r.json()["id"]
        row = con.execute(
            "SELECT project_id FROM uploaded_outlines WHERE id=?",
            (uid,)).fetchone()
        assert row and row[0] == pid, f"F1 broken: {row!r}"
        ok.append("2 parse persisted project_id (query-string bound)")

        # 3. unknown project -> 404, no orphan row
        r = c.post("/api/v1/upload-outline/parse",
                   params={"project_id": "no-such-project"},
                   files={"file": ("outline.txt", io.BytesIO(OUTLINE),
                                   "text/plain")})
        assert r.status_code == 404, (r.status_code, r.text)
        ok.append("3 unknown project rejected 404")

        # 4. simulate legacy no-link record, then save-as-outline backfills
        con.execute("UPDATE uploaded_outlines SET project_id='' WHERE id=?",
                    (uid,))
        con.commit()
        outline = [{"title": t.split(" ", 1)[1]}
                   for t in OUTLINE.decode("utf-8").splitlines()]
        r = c.post(f"/api/v1/upload-outline/{uid}/save-as-outline",
                   json={"scheme_id": sid, "outline": outline})
        assert r.status_code == 200, (r.status_code, r.text)
        row = con.execute(
            "SELECT project_id, status FROM uploaded_outlines WHERE id=?",
            (uid,)).fetchone()
        assert row[0] == pid and row[1] == "saved", row
        ok.append("4 save-as-outline backfilled project_id (legacy self-heal)")

        # 5. F2: manual correction on a NEVER-extracted item creates the row
        r = c.put("/api/v1/bid-analysis/results/schemeBasicInfo",
                  json={"content": "## manual: pit depth 8m"},
                  params={"scheme_id": sid})
        assert r.status_code == 200, (r.status_code, r.text)
        item = r.json().get("item")
        assert item and item["source"] == "manual", r.text
        row = con.execute(
            "SELECT status, source, content FROM bid_analysis_items "
            "WHERE item_id='schemeBasicInfo' AND project_id=?",
            (pid,)).fetchone()
        assert row and row[0] == "success" and "8m" in row[2], row
        ok.append("5 manual correction UPSERT created the row")

        # 5b. results list (GET) shows the corrected item
        r = c.get("/api/v1/bid-analysis/results", params={"scheme_id": sid})
        assert r.status_code == 200, r.text
        data = r.json()
        items = data.get("items") or data.get("results") or []
        found = [i for i in items if i.get("item_id") == "schemeBasicInfo"]
        assert found, f"UPSERTed row missing from GET /results: keys={list(data)}"
        ok.append("5b corrected row visible via GET /results")

        # 6. delete project -> cascade removes the linked outline row
        r = c.delete(f"/api/v1/projects/{pid}")
        assert r.status_code == 200, (r.status_code, r.text)
        n = con.execute(
            "SELECT COUNT(*) FROM uploaded_outlines WHERE id=?",
            (uid,)).fetchone()[0]
        assert n == 0, "cascade failed"
        ok.append("6 delete project cascaded uploaded_outlines (write<->cleanup)")
        con.close()

    print("E2E PASS")
    for line in ok:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
