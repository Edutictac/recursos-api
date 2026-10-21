"""Llibres publications: inert, validated SVG pages compiled to static HTML.

Storage and catalogue changes share one SQLite transaction. No imported HTML or
JavaScript is executed or served. The editable is an optional attachment only.
"""
import base64
import hashlib
import html
import io
import json
import math
import os
import re
import secrets
import stat
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from defusedxml import ElementTree as SafeXML
from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from .models import Resource

MAX_UPLOAD = 30 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
PROVIDER = "edutictac-llibres"
ID = re.compile(r"[a-f0-9]{32}\Z")
DRAFT = re.compile(r"[a-zA-Z0-9-]{16,64}\Z")
TAGS = set("svg g defs rect circle ellipse path polygon polyline line text tspan image clipPath linearGradient radialGradient stop pattern filter feGaussianBlur feOffset feFlood feComposite feMerge feMergeNode feColorMatrix title desc".split())
ATTRS = set("id transform viewBox version x y x1 y1 x2 y2 dx dy width height rx ry r cx cy d points fill stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit stroke-dasharray stroke-dashoffset opacity fill-opacity stroke-opacity fill-rule clip-rule clip-path font-family font-size font-style font-weight text-anchor dominant-baseline text-decoration visibility style space href preserveAspectRatio filter filterUnits primitiveUnits stdDeviation in in2 result operator k1 k2 k3 k4 flood-color flood-opacity values type offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod fx fy fr patternUnits patternContentUnits patternTransform".split())
ATTRS.update({"vector-effect", "paint-order", "letter-spacing"})
CSS = set("stroke stroke-width stroke-dasharray stroke-linecap stroke-linejoin stroke-miterlimit stroke-dashoffset fill fill-rule fill-opacity stroke-opacity opacity font-family font-size font-style font-weight text-decoration white-space visibility filter clip-path paint-order".split())
CSS.update({"image-rendering", "letter-spacing", "text-decoration-thickness"})
_COMPILERS = threading.BoundedSemaphore(2)
ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


def fail(code="invalid_publication", status=422):
    raise HTTPException(status, code)


def unpack(raw, limit=MAX_EXPANDED):
    """Bound both declared and actual bytes; never extract to a filesystem."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        files, total = {}, 0
        if len(archive.infolist()) > 2048:
            fail()
        for entry in archive.infolist():
            name = entry.filename
            total += entry.file_size
            if (name in files or name.startswith(("/", "\\")) or "\\" in name or
                    re.match(r"[a-z]:", name, re.I) or ".." in name.split("/") or
                    stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 or
                    entry.compress_type not in (0, 8) or total > limit):
                fail()
            with archive.open(entry) as stream:
                data = stream.read(min(limit, entry.file_size) + 1)
            if len(data) != entry.file_size:
                fail()
            files[name] = data
        return files
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, NotImplementedError):
        fail()


def raster(raw, cover=False):
    if not raw or len(raw) > 5 * 1024 * 1024 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fail()
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format != "PNG" or max(probe.size) > 4096 or getattr(probe, "n_frames", 1) != 1:
                fail()
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            clean = Image.new("RGBA", image.size)
            clean.paste(image.convert("RGBA"))
            if cover:
                clean.thumbnail((640, 360))
            out = io.BytesIO()
            clean.save(out, "PNG")
            data = out.getvalue()
            if len(data) > 5 * 1024 * 1024:
                fail()
            return data
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        fail()


def safe_value(value):
    if len(value) > 256 * 1024 or re.search(r"[\\@<>]|(?:https?|javascript|data|file):|//", value, re.I):
        fail()
    for match in re.finditer(r"url\s*\((.*?)\)", value, re.I):
        if not re.fullmatch(r"#[a-zA-Z0-9_.-]+", match[1]):
            fail()
    return value


def sanitize_svg(raw, files):
    if len(raw) > 12 * 1024 * 1024:
        fail()
    try:
        root = SafeXML.fromstring(raw, forbid_dtd=True)
    except Exception:
        fail()
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        fail()
    try:
        width, height = float(root.get("width", "0")), float(root.get("height", "0"))
        if not (0 < width <= 1920 and 0 < height <= 1200):
            fail()
        if root.get("viewBox"):
            bounds = [float(v) for v in root.get("viewBox").replace(",", " ").split()]
            if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds) or not (0 < bounds[2] <= 1920 and 0 < bounds[3] <= 1200):
                fail()
    except ValueError:
        fail()
    elements = list(root.iter())
    if len(elements) > 5000:
        fail()
    for node in elements:
        if not node.tag.startswith("{http://www.w3.org/2000/svg}") or node.tag.split("}")[-1] not in TAGS:
            fail()
        tag = node.tag.split("}")[-1]
        for key, value in list(node.attrib.items()):
            name = key.split("}")[-1]
            if name not in ATTRS or (key.startswith("{") and not key.startswith(("{http://www.w3.org/1999/xlink}", "{http://www.w3.org/XML/1998/namespace}"))):
                fail()
            if name == "href":
                if tag != "image" or not value.startswith("data:image/png;base64,"):
                    fail()
                try:
                    image = raster(base64.b64decode(value.split(",", 1)[1], validate=True))
                except (ValueError, TypeError):
                    fail()
                path = "images/" + hashlib.sha256(image).hexdigest() + ".png"
                files[path] = (image, "image/png")
                node.set(key, path)
            elif name == "style":
                for declaration in value.split(";"):
                    if not declaration.strip():
                        continue
                    pair = declaration.split(":", 1)
                    if len(pair) != 2 or pair[0].strip() not in CSS:
                        fail()
                    safe_value(pair[1])
            else:
                safe_value(value)
        if len(node.text or "") > 100000:
            fail()
    return ET.tostring(root, encoding="unicode")


def credit_file(raw):
    if len(raw) > 420 * 1024:
        fail()
    try:
        c = json.loads(raw)
        if c.get("format") != "llibres-rebost-credit" or c.get("version") != 1 or not re.fullmatch(r"img-[a-f0-9]{64}", c.get("id", "")):
            fail()
        for k in ("name", "attribution", "license", "source_id", "source_revision", "source_path", "source_repository", "changes"):
            if not isinstance(c.get(k), str) or not 0 < len(c[k]) <= 2000:
                fail()
        for k, limit in (("source", 256 * 1024), ("licenseText", 128 * 1024)):
            if not isinstance(c.get(k), str) or not 0 < len(c[k].encode()) <= limit:
                fail()
        return c
    except (ValueError, TypeError, AttributeError):
        fail()


def text(metadata, key, limit, required=False):
    value = metadata.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        fail()
    return value.strip()


def compile_book(raw):
    with _COMPILERS:
        return _compile_book(raw)


def _compile_book(raw):
    package = unpack(raw)
    try:
        manifest = json.loads(package["manifest.json"])
    except (KeyError, ValueError, TypeError):
        fail()
    if not isinstance(manifest, dict):
        fail()
    if manifest.get("format") != "llibres-publication" or manifest.get("version") != 1 or manifest.get("visibility") != "public" or manifest.get("confirmed") is not True:
        fail()
    meta = {k: text(manifest, k, n, required) for k, n, required in (
        ("title", 200, True), ("description", 2000, False), ("author", 120, True),
        ("license", 100, True), ("language", 12, True))}
    if meta["language"] not in ("es", "va", "ca-valencia") or meta["license"] not in ("CC-BY-SA-4.0", "CC-BY-4.0", "CC0-1.0"):
        fail()
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 51:
        fail()
    expected = {"manifest.json", "cover.png"}
    files, sections = {}, []
    for i, page in enumerate(pages):
        name = f"pages/{i + 1}.svg"
        expected.add(name)
        if not isinstance(page, str) or len(page) > 200 or name not in package:
            fail()
        svg = sanitize_svg(package[name], files)
        prev = f'<a href="#page-{i}">←</a>' if i else '<span>←</span>'
        next_ = f'<a href="#page-{i + 2}">→</a>' if i + 1 < len(pages) else '<span>→</span>'
        sections.append(f'<section class="page" id="page-{i + 1}" aria-label="{html.escape(page, quote=True)}">{svg}<nav>{prev}<span>{i + 1} / {len(pages)}</span>{next_}</nav></section>')
    credits = []
    for name, data in package.items():
        if re.fullmatch(r"credits/[a-f0-9]{64}\.txt", name):
            c = credit_file(data)
            files[name] = (data, "text/plain; charset=utf-8")
            credits.append(f'<li>{html.escape(c["name"])} · {html.escape(c["attribution"])} · {html.escape(c["license"])} — <a href="{name}" download>Font i llicència / Fuente y licencia</a></li>')
            expected.add(name)
    source = package.get("source.elpx")
    if source is not None:
        expected.add("source.elpx")
        if len(source) > 25 * 1024 * 1024:
            fail()
        source_files = unpack(source)
        if "content.xml" not in source_files:
            fail()
        xml = re.sub(br'<!DOCTYPE\s+ode\s+SYSTEM\s+[\'"]content\.dtd[\'"]\s*>', b"", source_files["content.xml"])
        try:
            SafeXML.fromstring(xml, forbid_dtd=True)
        except Exception:
            fail()
        files["source.elpx"] = (source, "application/octet-stream")
    if set(package) != expected:
        fail()
    files["cover.png"] = (raster(package["cover.png"], cover=True), "image/png")
    editable = '<a href="source.elpx" download>Descarrega editable / Descargar editable</a>' if source else ""
    css = 'body{margin:0;background:#edf0e9;color:#193f38;font:18px Arial}header,footer{padding:1rem;max-width:1280px;margin:auto}h1{font-size:1.4rem}main{max-width:1280px;margin:auto}.page{display:none}.page:first-child{display:block}main:has(.page:target) .page{display:none}.page:target{display:block!important}svg{display:block;width:100%;height:auto;background:#fffdf5}nav{display:flex;justify-content:space-between;align-items:center;padding:1rem}nav a{padding:.7rem 1.5rem;background:white;border-radius:.5rem}a{color:#193f38}footer{font-size:.85rem}li{margin:.6rem 0}'
    language = "ca-valencia" if meta["language"] != "es" else "es"
    document = f'<!doctype html><html lang="{language}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(meta["title"])}</title><style>{css}</style><header><h1>{html.escape(meta["title"])}</h1><p>{html.escape(meta["author"])}</p></header><main>{"".join(sections)}</main><footer><p>{html.escape(meta["license"])} · {editable}</p><details><summary>Crèdits i llicències / Créditos y licencias</summary><ul>{"".join(credits)}</ul></details><details><summary>Sobre este lector y licencias / Sobre este lector i llicències</summary><p><a href="../../source.zip" download>Codi font del servidor / Código fuente del servidor</a></p></details></footer></html>'
    files["index.html"] = (document.encode(), "text/html; charset=utf-8")
    if sum(len(body) for body, _ in files.values()) > 60 * 1024 * 1024:
        fail("publication_limit", 413)
    meta.update({"page_count": len(pages), "editable": source is not None})
    return meta, files


def current_reader(body):
    """Hide the technical source link in readers published before the UI change."""
    link = '<p><a href="../../source.zip" download>Codi font del servidor / Código fuente del servidor</a></p>'.encode()
    if link + b'<details><summary>' in body:
        about = '<details><summary>Sobre este lector y licencias / Sobre este lector i llicències</summary>'.encode() + link + b'</details>'
        body = body.replace(link, b'', 1).replace(b'</footer>', about + b'</footer>', 1)
    return body


class Publications:
    def __init__(self, connect, public_base, token):
        self.connect, self.token = connect, token
        parsed = urlsplit(public_base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid Llibres public base URL")
        self.base = public_base.rstrip("/")
        self.global_limit = int(os.environ.get("LLIBRES_PUBLIC_MAX_BYTES", str(2 * 1024 ** 3)))
        with connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS llibres_publications (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft TEXT NOT NULL,
              revision INTEGER NOT NULL, digest TEXT NOT NULL, metadata TEXT NOT NULL,
              active INTEGER NOT NULL, UNIQUE(owner,draft));
              CREATE TABLE IF NOT EXISTS llibres_files (
              book TEXT NOT NULL, path TEXT NOT NULL, body BLOB NOT NULL, mime TEXT NOT NULL,
              PRIMARY KEY(book,path));''')
        from .book_reviews import Reviews
        self.reviews = Reviews(self)

    def actor(self, request):
        supplied = request.headers.get("authorization", "")
        if not self.token or not secrets.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
            fail("publication_authentication_required", 401)
        actor = request.headers.get("x-llibres-publisher", "")
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{16,100}", actor):
            fail("publication_authentication_required", 401)
        return actor

    def key(self, actor, draft):
        if not DRAFT.fullmatch(draft):
            fail()
        return hashlib.sha256((actor + "\0" + draft).encode()).hexdigest()[:32]

    def result(self, row):
        return {"id": row["id"], "revision": row["revision"], "published": bool(row["active"]),
                "url": self.base + "/api/llibres/public/" + row["id"] + "/index.html", **json.loads(row["metadata"])}

    def publish(self, actor, draft, raw, expected):
        key, digest = self.key(actor, draft), hashlib.sha256(raw).hexdigest()
        meta, files = compile_book(raw)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.write_publication(db, actor, draft, digest, meta, files, expected)

    def write_publication(self, db, actor, draft, digest, meta, files, expected):
        key = self.key(actor, draft)
        old = db.execute("SELECT * FROM llibres_publications WHERE id=?", (key,)).fetchone()
        if old and old["digest"] == digest and old["active"]:
            return self.result(old)
        if expected != f'"{old["revision"] if old else 0}"':
            fail("publication_conflict", 412)
        size = sum(len(body) for body, _ in files.values())
        total = db.execute("SELECT coalesce(sum(length(body)),0) FROM llibres_files WHERE book!=?", (key,)).fetchone()[0]
        total += db.execute("SELECT coalesce(sum(length(body)),0) FROM llibres_review_files").fetchone()[0]
        owned = db.execute("SELECT count(*) FROM llibres_publications WHERE owner=? AND active=1 AND id!=?", (actor, key)).fetchone()[0]
        owner_bytes = db.execute("SELECT coalesce(sum(length(f.body)),0) FROM llibres_files f JOIN llibres_publications p ON p.id=f.book WHERE p.owner=? AND p.id!=?", (actor, key)).fetchone()[0]
        owner_bytes += db.execute("SELECT coalesce(sum(length(f.body)),0) FROM llibres_review_files f JOIN llibres_submissions s ON s.id=f.book WHERE s.owner=?", (actor,)).fetchone()[0]
        if total + size > self.global_limit or owner_bytes + size > 200 * 1024 * 1024 or owned >= 50:
            fail("publication_limit", 413)
        revision = old["revision"] + 1 if old else 1
        db.execute("INSERT INTO llibres_publications VALUES (?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,metadata=excluded.metadata,active=1", (key, actor, draft, revision, digest, json.dumps(meta)))
        db.execute("DELETE FROM llibres_files WHERE book=?", (key,))
        db.executemany("INSERT INTO llibres_files VALUES (?,?,?,?)", [(key, name, body, mime) for name, (body, mime) in files.items()])
        url = self.base + "/api/llibres/public/" + key + "/"
        now = datetime.now(timezone.utc).isoformat()
        resource = Resource(provider=PROVIDER, external_id=key, title=meta["title"],
            description=meta["description"], author=meta["author"], license=meta["license"], license_known=True,
            language=[meta["language"]], resource_type="book", format="llibres", subject="Lengua",
            educational_stage="Primaria", play_url=url + "index.html", source_url=url + "index.html",
            download_url=url + "source.elpx" if meta["editable"] else "", reuse_url=url + "source.elpx" if meta["editable"] else "",
            thumbnail_url=url + "cover.png", metadata_json={"page_count":meta["page_count"],"revision":revision},
            indexed_at=now,last_synced_at=now).to_row()
        columns = list(resource)
        db.execute("INSERT INTO resources (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ") ON CONFLICT(provider,external_id) DO UPDATE SET " + ",".join(c + "=excluded." + c for c in columns if c not in ("provider", "external_id")), tuple(resource.values()))
        return self.result(db.execute("SELECT * FROM llibres_publications WHERE id=?", (key,)).fetchone())
    def router(self):
        router = APIRouter()
        router.include_router(self.reviews.router())

        @router.get("/api/internal/llibres/{draft}")
        def state(draft: str, request: Request):
            key = self.key(self.actor(request), draft)
            with self.connect() as db:
                row = db.execute("SELECT * FROM llibres_publications WHERE id=?", (key,)).fetchone()
            return self.result(row) if row else {"id":key,"revision":0,"published":False}

        @router.post("/api/internal/llibres/{draft}")
        async def publish(draft: str, request: Request):
            actor = self.actor(request)
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_UPLOAD:
                    fail("publication_limit", 413)
                raw.extend(chunk)
            return await run_in_threadpool(self.publish, actor, draft, bytes(raw), request.headers.get("if-match"))

        @router.delete("/api/internal/llibres/{draft}")
        def withdraw(draft: str, request: Request):
            key = self.key(self.actor(request), draft)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM llibres_publications WHERE id=?", (key,)).fetchone()
                if not row:
                    fail("not_found", 404)
                if row["active"] and request.headers.get("if-match") != f'"{row["revision"]}"':
                    fail("publication_conflict", 412)
                if row["active"]:
                    db.execute("UPDATE llibres_publications SET active=0,revision=revision+1 WHERE id=?", (key,))
                    db.execute("UPDATE resources SET active=0 WHERE provider=? AND external_id=?", (PROVIDER, key))
                    db.execute("DELETE FROM llibres_files WHERE book=?", (key,))
                return self.result(db.execute("SELECT * FROM llibres_publications WHERE id=?", (key,)).fetchone())

        @router.get("/api/llibres/source.zip")
        def source():
            # Only source-code paths, never environment, database or runtime data.
            root = Path(__file__).resolve().parents[1]
            paths = [root / name for name in ("main.py", "requirements.txt", "LICENSE", "README.md", "Dockerfile", "seed.json")]
            paths.extend((root / "app").rglob("*.py"))
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(paths):
                    if path.is_file(): archive.write(path, "recursos-api/" + str(path.relative_to(root)))
            return Response(out.getvalue(), media_type="application/zip", headers={
                "Content-Disposition": 'attachment; filename="recursos-api-source.zip"',
                "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

        @router.get("/api/llibres/books")
        def books(q: str = "", offset: int = 0, limit: int = 24):
            if len(q) > 200 or offset < 0 or not 1 <= limit <= 80:
                fail()
            with self.connect() as db:
                rows = db.execute("SELECT * FROM resources WHERE provider=? AND active=1 AND instr(lower(title),lower(?))>0 ORDER BY id DESC LIMIT ? OFFSET ?", (PROVIDER, q, limit+1, offset)).fetchall()
            return {"items":[Resource.from_row(r).to_dict() for r in rows[:limit]], "more":len(rows)>limit, "bank_url":self.base}

        @router.get("/api/llibres/public/{book}/{path:path}")
        def content(book: str, path: str):
            if not ID.fullmatch(book):
                fail("not_found", 404)
            with self.connect() as db:
                row = db.execute("SELECT f.body,f.mime FROM llibres_files f JOIN llibres_publications p ON p.id=f.book WHERE f.book=? AND f.path=? AND p.active=1", (book, path)).fetchone()
            if not row:
                fail("not_found", 404)
            headers = {"Cache-Control":"no-store", "X-Content-Type-Options":"nosniff", "Referrer-Policy":"no-referrer",
                "Content-Security-Policy":"sandbox allow-downloads; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors *",
                "Cross-Origin-Resource-Policy":"cross-origin"}
            if path == "source.elpx" or path.startswith("credits/"):
                headers["Content-Disposition"] = 'attachment; filename="' + ("llibre.elpx" if path == "source.elpx" else path.split("/")[-1]) + '"'
            body = current_reader(row["body"]) if path == "index.html" else row["body"]
            return Response(body, media_type=row["mime"], headers=headers)

        return router
