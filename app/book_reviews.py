"""Private pupil submissions, reviewed by a Commons-authorized class teacher.

All identity and class headers come from the authenticated Commons proxy. Never
publish student bytes directly: approve the exact compiled, immutable revision.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from .books import MAX_UPLOAD, ID, PROVIDER, compile_book, current_reader, fail


async def upload(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_UPLOAD:
            fail("publication_limit", 413)
        raw.extend(chunk)
    return bytes(raw)


async def json_body(request, limit=128*1024):
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>limit: fail("publication_limit",413)
        raw.extend(chunk)
    try: data=json.loads(raw)
    except (ValueError,UnicodeDecodeError): fail()
    if not isinstance(data,dict): fail()
    return data


class Reviews:
    def __init__(self, publications):
        self.p = publications
        with self.p.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS llibres_submissions (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft TEXT NOT NULL,
              group_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
              digest TEXT NOT NULL, metadata TEXT NOT NULL, feedback TEXT NOT NULL,
              updated_at TEXT NOT NULL, reviewed_by TEXT NOT NULL DEFAULT '',
              approved_revision INTEGER NOT NULL DEFAULT 0, UNIQUE(owner,draft));
              CREATE TABLE IF NOT EXISTS llibres_review_files (
              book TEXT NOT NULL, path TEXT NOT NULL, body BLOB NOT NULL, mime TEXT NOT NULL,
              PRIMARY KEY(book,path));''')
            columns={r[1] for r in db.execute("PRAGMA table_info(llibres_submissions)")}
            for name in ("approved_by", "approved_at"):
                if name not in columns:
                    db.execute("ALTER TABLE llibres_submissions ADD COLUMN "+name+" TEXT NOT NULL DEFAULT ''")
            db.execute("""UPDATE llibres_submissions SET approved_by=reviewed_by,approved_at=updated_at
              WHERE status='approved' AND approved_revision>0 AND approved_by=''""")

    def group(self, request):
        value = request.headers.get("x-llibres-group", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
            fail("review_class_required", 403)
        return value

    def row(self, db, key, group):
        if not ID.fullmatch(key):
            fail("not_found", 404)
        row = db.execute("SELECT * FROM llibres_submissions WHERE id=? AND group_id=?", (key, group)).fetchone()
        if not row:
            fail("not_found", 404)
        return row

    def result(self, db, row):
        pub = db.execute("SELECT * FROM llibres_publications WHERE id=?", (row["id"],)).fetchone()
        return {"id": row["id"], "revision": row["revision"], "status": row["status"],
                "feedback": row["feedback"], "group_id": row["group_id"],
                "updated_at": row["updated_at"], "approved_revision": row["approved_revision"],
                "published": bool(pub and pub["active"]),
                "url": self.p.result(pub)["url"] if pub and pub["active"] else None,
                **json.loads(row["metadata"])}

    def submit(self, actor, group, draft, raw, expected):
        key = self.p.key(actor, draft)
        digest = hashlib.sha256(raw).hexdigest()
        meta, files = compile_book(raw)
        with self.p.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM llibres_submissions WHERE id=?", (key,)).fetchone()
            if old and old["group_id"] != group:
                fail("review_class_changed", 409)
            if old and old["digest"] == digest and old["status"] in ("pending", "approved"):
                return self.result(db, old)
            if expected != f'"{old["revision"] if old else 0}"':
                fail("publication_conflict", 412)
            size = sum(len(body) for body, _ in files.values())
            total = db.execute("SELECT coalesce(sum(length(body)),0) FROM llibres_files").fetchone()[0]
            total += db.execute("SELECT coalesce(sum(length(body)),0) FROM llibres_review_files WHERE book!=?", (key,)).fetchone()[0]
            owned = db.execute("SELECT count(*) FROM llibres_submissions WHERE owner=? AND status!='cancelled' AND id!=?", (actor, key)).fetchone()[0]
            usage = db.execute("SELECT coalesce(sum(length(f.body)),0) FROM llibres_review_files f JOIN llibres_submissions s ON s.id=f.book WHERE s.owner=? AND s.id!=?", (actor, key)).fetchone()[0]
            usage += db.execute("SELECT coalesce(sum(length(f.body)),0) FROM llibres_files f JOIN llibres_publications p ON p.id=f.book WHERE p.owner=?", (actor,)).fetchone()[0]
            if total + size > self.p.global_limit or usage + size > 200 * 1024**2 or owned >= 50:
                fail("publication_limit", 413)
            revision = old["revision"] + 1 if old else 1
            now = datetime.now(timezone.utc).isoformat()
            db.execute("""INSERT INTO llibres_submissions
              (id,owner,draft,group_id,revision,status,digest,metadata,feedback,updated_at)
              VALUES (?,?,?,?,?,'pending',?,?,'',?) ON CONFLICT(id) DO UPDATE SET
              revision=excluded.revision,status='pending',digest=excluded.digest,
              metadata=excluded.metadata,feedback='',updated_at=excluded.updated_at,reviewed_by=''""",
              (key, actor, draft, group, revision, digest, json.dumps(meta), now))
            db.execute("DELETE FROM llibres_review_files WHERE book=?", (key,))
            db.executemany("INSERT INTO llibres_review_files VALUES (?,?,?,?)",
                           [(key, name, body, mime) for name, (body, mime) in files.items()])
            return self.result(db, self.row(db, key, group))

    def decide(self, actor, group, key, expected, decision, feedback=""):
        if decision not in ("approve", "return", "withdraw", "cancel"):
            fail()
        if not isinstance(feedback, str) or len(feedback) > 1000 or (decision == "return" and not feedback.strip()):
            fail("review_feedback_required")
        with self.p.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, key, group)
            if expected != f'"{row["revision"]}"':
                fail("publication_conflict", 412)
            if decision == "cancel" and row["status"] not in ("pending", "returned"):
                fail("publication_conflict", 412)
            if decision in ("approve", "return") and row["status"] != "pending":
                fail("publication_conflict", 412)
            if decision == "approve":
                files = {r["path"]: (r["body"], r["mime"]) for r in db.execute("SELECT * FROM llibres_review_files WHERE book=?", (key,))}
                if "index.html" not in files:
                    fail()
                # Free the private copy in this transaction before enforcing the
                # public quota. A failure rolls back both files and the decision.
                db.execute("DELETE FROM llibres_review_files WHERE book=?", (key,))
                pub = db.execute("SELECT revision FROM llibres_publications WHERE id=?", (key,)).fetchone()
                self.p.write_publication(db, row["owner"], row["draft"], row["digest"],
                    json.loads(row["metadata"]), files, f'"{pub["revision"] if pub else 0}"')
                status = "approved"
            elif decision == "return":
                status = "returned"
            elif decision == "cancel":
                status = "cancelled"
                db.execute("DELETE FROM llibres_review_files WHERE book=?", (key,))
            else:
                status = "cancelled"
                db.execute("UPDATE llibres_publications SET active=0,revision=revision+1 WHERE id=? AND active=1", (key,))
                db.execute("UPDATE resources SET active=0 WHERE provider=? AND external_id=?", (PROVIDER, key))
                db.execute("DELETE FROM llibres_files WHERE book=?", (key,))
                db.execute("DELETE FROM llibres_review_files WHERE book=?", (key,))
            db.execute("""UPDATE llibres_submissions SET status=?,revision=revision+1,feedback=?,
              updated_at=?,reviewed_by=?,approved_revision=CASE WHEN ?='approved' THEN revision ELSE approved_revision END,
              approved_by=CASE WHEN ?='approved' THEN ? ELSE approved_by END,
              approved_at=CASE WHEN ?='approved' THEN ? ELSE approved_at END
              WHERE id=?""", (status, feedback.strip(), datetime.now(timezone.utc).isoformat(), actor, status,
                                status, actor, status, datetime.now(timezone.utc).isoformat(), key))
            return self.result(db, self.row(db, key, group))

    def router(self):
        router = APIRouter()

        @router.get("/api/internal/llibres/submissions/{draft}")
        @router.post("/api/internal/llibres/submissions/{draft}")
        @router.delete("/api/internal/llibres/submissions/{draft}")
        async def own(draft: str, request: Request):
            actor, group = self.p.actor(request), self.group(request)
            key = self.p.key(actor, draft)
            if request.method == "POST":
                return await run_in_threadpool(self.submit, actor, group, draft, await upload(request), request.headers.get("if-match"))
            if request.method == "DELETE":
                with self.p.connect() as db:
                    row = self.row(db, key, group)
                    if row["owner"] != actor: fail("not_found", 404)
                return await run_in_threadpool(self.decide, actor, group, key, request.headers.get("if-match"), "withdraw" if request.query_params.get("publication")=="1" else "cancel")
            with self.p.connect() as db:
                row = db.execute("SELECT * FROM llibres_submissions WHERE id=? AND group_id=?", (key, group)).fetchone()
                return self.result(db, row) if row else {"id": key, "revision": 0, "status": "draft", "published": False}

        @router.post("/api/internal/llibres/reviews")
        async def listing(request: Request):
            self.p.actor(request)
            data = await json_body(request)
            groups = data.get("groups") if isinstance(data, dict) else None
            if not isinstance(groups, list) or len(groups) > 1000 or not all(isinstance(g, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", g) for g in groups): fail()
            offset = data.get("offset", 0)
            if not isinstance(offset, int) or not 0 <= offset <= 100000: fail()
            where = "" if data.get("all") is True else " AND status='pending'"
            scope = "1=1" if data.get("all_classes") is True else "group_id IN ("+",".join("?" for _ in groups)+")"
            parameters = (offset,) if data.get("all_classes") is True else (*groups, offset)
            with self.p.connect() as db:
                rows = db.execute("SELECT * FROM llibres_submissions WHERE "+scope+where+" ORDER BY (status='pending') DESC,updated_at DESC,id LIMIT 25 OFFSET ?", parameters).fetchall()
                return {"items": [self.result(db, r) for r in rows[:24]], "more": len(rows)>24}

        @router.get("/api/internal/llibres/review-scope/{key}")
        def scope(key: str, request: Request):
            self.p.actor(request)
            if not ID.fullmatch(key): fail("not_found", 404)
            with self.p.connect() as db:
                row = db.execute("SELECT group_id,owner FROM llibres_submissions WHERE id=?", (key,)).fetchone()
                if not row: fail("not_found", 404)
                return {"group_id": row["group_id"], "owner":row["owner"]}

        @router.post("/api/internal/llibres/reviews/{key}")
        async def decision(key: str, request: Request):
            actor, group = self.p.actor(request), self.group(request)
            data = await json_body(request, 8192)
            if not isinstance(data, dict): fail()
            return await run_in_threadpool(self.decide, actor, group, key, request.headers.get("if-match"), data.get("decision"), data.get("feedback", ""))

        @router.get("/api/internal/llibres/reviews/{key}/files/{path:path}")
        def preview(key: str, path: str, request: Request):
            self.p.actor(request)
            group = self.group(request)
            if not re.fullmatch(r"(?:index\.html|cover\.png|source\.elpx|images/[a-f0-9]{64}\.png|credits/[a-f0-9]{64}\.txt)", path): fail("not_found", 404)
            with self.p.connect() as db:
                self.row(db, key, group)
                row = db.execute("SELECT body,mime FROM llibres_review_files WHERE book=? AND path=?", (key, path)).fetchone()
                if not row: fail("not_found", 404)
            body = current_reader(row["body"]).replace(b'../../source.zip', b'/api/llibres/source.zip') if path == "index.html" else row["body"]
            headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
              "Content-Security-Policy": "sandbox allow-same-origin allow-downloads; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"}
            if path == "source.elpx" or path.startswith("credits/"):
                headers["Content-Disposition"] = 'attachment; filename="'+("llibre.elpx" if path == "source.elpx" else path.split("/")[-1])+'"'
            return Response(body, media_type=row["mime"], headers=headers)

        return router
