"""Recursos API — backend autoalojado que sustituye a Firebase Firestore+Auth.

API REST (FastAPI + SQLite) para la PWA del Banc de recursos: favoritos,
valoraciones (con resumen agregado), avisos de actividad rota, propuestas de
actividades y modo admin por token. Identidad anónima por cookie firmada (sin Google).

Endpoints:
  GET  /api/health
  GET  /api/preferences
  POST /api/favorites/toggle   { game_key }
  POST /api/ratings            { game_key, value }
  POST /api/reports            { game_key }
  GET  /api/submissions
  POST /api/submissions        { title, url, notes, area, language, name? }
  GET  /api/auth/login
  GET  /api/auth/callback
  GET  /api/auth/logout
  GET  /api/auth/me
  POST /api/admin/resources/hide { game_key }
"""

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from secrets import compare_digest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from edutictac_community.community import Identity, create_community_router
from edutictac_community.db import connect as _db_connect
from edutictac_community.ratelimit import RateLimiter
from edutictac_community.session import SignedSession
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import config, oidc
from app.db import init_index_schema
from app.httpclient import get_bytes
from app.models import Resource

DB_PATH = os.environ.get("RECURSOS_DB", "/var/lib/recursos-api/recursos.db")
SESSION_SECRET = os.environ.get("RECURSOS_SECRET", "")
SESSION_COOKIE = "recursos_session"
AUTH_NEXT_COOKIE = "recursos_auth_next"
COOKIE_SECURE = os.environ.get("RECURSOS_COOKIE_SECURE", "1").lower() not in {"0", "false", "no"}
EDUTICTAC_ID_API_URL = os.environ.get("EDUTICTAC_ID_API_URL", "").rstrip("/")
EDUTICTAC_ID_TEACHER_TOKEN = os.environ.get("EDUTICTAC_ID_TEACHER_TOKEN", "")
ALLOWED_AUTH_NEXT_HOSTS = {"edutictac.es", "recursos.edutictac.es"}

RATE_WINDOW = 60
RATE_MAX = 60
_rate_limiter = RateLimiter(max_calls=RATE_MAX, window_seconds=RATE_WINDOW)

app = FastAPI(title="Recursos API")
logger = logging.getLogger("recursos_api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://edutictac.es"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


def get_conn() -> sqlite3.Connection:
    return _db_connect(DB_PATH)


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rating_summary (
                game_key TEXT PRIMARY KEY,
                sum INTEGER NOT NULL DEFAULT 0,
                count INTEGER NOT NULL DEFAULT 0,
                avg REAL NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS broken_reports (
                game_key TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                admin_reported INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                notes TEXT DEFAULT '',
                area TEXT DEFAULT 'General',
                language TEXT DEFAULT '',
                submitted_by TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id TEXT NOT NULL,
                game_key TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (user_id, game_key)
            );
            CREATE TABLE IF NOT EXISTS ratings (
                user_id TEXT NOT NULL,
                game_key TEXT NOT NULL,
                value INTEGER NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, game_key)
            );
            CREATE TABLE IF NOT EXISTS reports (
                user_id TEXT NOT NULL,
                game_key TEXT NOT NULL,
                reported_at TEXT,
                PRIMARY KEY (user_id, game_key)
            );
            """
        )
        _seed(conn)


def _seed(conn: sqlite3.Connection) -> None:
    seed_path = Path(__file__).with_name("seed.json")
    if not seed_path.exists():
        return
    data = json.loads(seed_path.read_text(encoding="utf-8"))

    for row in data.get("rating_summary", []):
        conn.execute(
            "INSERT OR IGNORE INTO rating_summary (game_key, sum, count, avg) VALUES (?, ?, ?, ?)",
            (row["game_key"], row.get("sum", 0), row.get("count", 0), row.get("avg", 0)),
        )
    for row in data.get("broken_reports", []):
        conn.execute(
            "INSERT OR IGNORE INTO broken_reports (game_key, count, admin_reported) VALUES (?, ?, ?)",
            (row["game_key"], row.get("count", 0), 1 if row.get("admin_reported") else 0),
        )
    for row in data.get("submissions", []):
        conn.execute(
            "INSERT OR IGNORE INTO submissions (id, title, url, notes, area, language, submitted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row.get("title", ""),
                row.get("url", ""),
                row.get("notes", ""),
                row.get("area", "General"),
                row.get("language", ""),
                row.get("submitted_by", ""),
            ),
        )


init_db()
init_index_schema()


# --- Sesión firmada (cookie) ---

_session = SignedSession(SESSION_SECRET, SESSION_COOKIE, cookie_secure=COOKIE_SECURE)


def make_session(uid: str, admin: bool = False) -> str:
    return _session.encode({"uid": uid, "admin": bool(admin)})


def parse_session(cookie: str | None) -> dict | None:
    return _session.decode(cookie)


def set_session_cookie(response: Response, value: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=max_age,
    )


def auth_next_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return "/"
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return "/"
    if parsed.scheme != "https" or parsed.netloc not in ALLOWED_AUTH_NEXT_HOSTS:
        return "/"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def set_auth_next_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        AUTH_NEXT_COOKIE,
        value,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/api/auth",
        max_age=600,
    )


def delete_auth_next_cookie(response: Response) -> None:
    response.delete_cookie(
        AUTH_NEXT_COOKIE,
        path="/api/auth",
        secure=COOKIE_SECURE,
        samesite="lax",
    )


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        samesite="lax",
    )


def describe_oidc_error(exc: Exception) -> str:
    parts = [exc.__class__.__name__]
    for attr in ("error", "description"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={str(value)[:160]}")
    message = str(exc)
    if message and "code=" not in message and "state=" not in message:
        parts.append(f"message={message[:160]}")
    return " ".join(parts)


def session_admin(uid: str, cookie_admin: bool) -> bool:
    if uid.startswith("oidc:") and oidc.is_admin_sub(uid[len("oidc:"):]):
        return True
    return cookie_admin


def get_session(request: Request) -> tuple[str, bool]:
    """Devuelve (uid, admin). Crea identidad anónima si no hay cookie válida."""
    cookie = request.cookies.get(SESSION_COOKIE)
    sess = parse_session(cookie)
    if sess and sess.get("uid"):
        uid = sess["uid"]
        return uid, session_admin(uid, bool(sess.get("admin")))
    return "", False


def rate_limited(ip: str) -> bool:
    return _rate_limiter(ip)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# --- Modelos ---

class SubmissionIn(BaseModel):
    title: str
    url: str
    notes: str = ""
    area: str = "General"
    language: str = ""
    name: str = ""


class StudentLoginIn(BaseModel):
    public_code: str
    pin: str


class StudentBatchIn(BaseModel):
    count: int
    pin_length: int = 4


class PinRegenerateIn(BaseModel):
    pin_length: int = 4


class ActivityAssignmentIn(BaseModel):
    group_id: str
    app_id: str
    activity_id: str
    title: str = ""


class PlayResourceMetadataIn(BaseModel):
    title: str
    description: str = ""
    type: str
    language: str = "es"
    subject: str = ""
    educational_stage: str = ""
    tags: list[str] = []
    license: str = "CC BY-SA"
    author_pseudonym: str = ""


class PlayResourcePackageIn(BaseModel):
    format: str = "h5p"
    delivery: str = "download_url"


class PlayResourcePublishIn(BaseModel):
    contract_version: str
    external_id: str
    version: str
    idempotency_key: str
    metadata: PlayResourceMetadataIn
    visibility: str
    canonical_url: str
    download_url: str
    package: PlayResourcePackageIn


# --- Endpoints ---

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/submissions")
def list_submissions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM submissions ORDER BY created_at DESC").fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "notes": r["notes"],
            "area": r["area"],
            "language": r["language"],
            "submitted_by": r["submitted_by"],
        }
        for r in rows
    ]


@app.post("/api/submissions", status_code=201)
def add_submission(payload: SubmissionIn, request: Request, response: Response) -> dict:
    if rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    uid, _ = _ensure_uid(request, response)
    title = payload.title.strip()
    url = payload.url.strip()
    if not title or not url:
        raise HTTPException(status_code=400, detail="title and url required")

    sub_id = secrets.token_urlsafe(10)
    created_at = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO submissions (id, title, url, notes, area, language, submitted_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sub_id, title, url, payload.notes, payload.area or "General", payload.language, payload.name, created_at),
        )
    return {
        "id": sub_id,
        "title": title,
        "url": url,
        "notes": payload.notes,
        "area": payload.area or "General",
        "language": payload.language,
        "submitted_by": payload.name,
    }


def _ensure_uid(request: Request, response: Response) -> tuple[str, bool]:
    """Garantiza identidad anónima: devuelve (uid, admin) y fija la cookie si faltaba."""
    cookie = request.cookies.get(SESSION_COOKIE)
    sess = parse_session(cookie)
    if sess and sess.get("uid"):
        uid = sess["uid"]
        return uid, session_admin(uid, bool(sess.get("admin")))
    uid = secrets.token_hex(16)
    set_session_cookie(response, make_session(uid, admin=False), 60 * 60 * 24 * 365)
    return uid, False


def resolve_community_identity(request: Request, response: Response) -> Identity:
    if request.method == "GET" and request.url.path.endswith("/preferences"):
        uid, admin = get_session(request)
        return Identity(uid=uid, admin=admin)
    uid, admin = _ensure_uid(request, response)
    return Identity(uid=uid, admin=admin)


app.include_router(
    create_community_router(
        DB_PATH,
        resolve_community_identity,
        rate_limited=rate_limited,
        key_field="game_key",
        db_key_column="game_key",
        admin_hide_path="/admin/resources/hide",
    ),
    prefix="/api",
)


def _student_payload(uid: str) -> dict:
    if not uid.startswith("student:"):
        return {"student_logged_in": False, "student_code": ""}
    parts = uid.split(":", 2)
    return {
        "student_logged_in": True,
        "student_code": parts[1] if len(parts) > 1 else "",
    }


def _require_teacher(request: Request) -> str:
    uid, _ = get_session(request)
    if not uid.startswith("oidc:"):
        raise HTTPException(status_code=401, detail="teacher authentication required")
    return uid


# --- Índice federado de recursos (búsqueda unificada) ---

def _norm(s: str) -> str:
    return re.sub(r"[\u0300-\u036f]", "", s or "").lower()


@app.get("/api/resources")
def list_resources(
    q: str = "",
    provider: str = "",
    format: str = "",
    subject: str = "",
    stage: str = "",
    language: str = "",
    license: str = "",
    license_known: bool | None = None,
    limit: int = 48,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    where = ["active = 1"]
    params: list = []

    if provider:
        where.append("provider = ?")
        params.append(provider)
    if format:
        where.append("format = ?")
        params.append(format)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if stage:
        where.append("educational_stage = ?")
        params.append(stage)
    if language:
        where.append("language LIKE ?")
        params.append(f'%"{language}"%')
    if license:
        where.append("license = ?")
        params.append(license)
    if license_known is not None:
        where.append("license_known = ?")
        params.append(1 if license_known else 0)

    term = _norm(q).strip()
    if term:
        where.append(
            "(lower(title) LIKE ? OR lower(COALESCE(title_ca,'')) LIKE ? "
            "OR lower(COALESCE(description,'')) LIKE ? OR lower(COALESCE(description_ca,'')) LIKE ? "
            "OR lower(COALESCE(author,'')) LIKE ? OR lower(tags) LIKE ? OR lower(external_id) LIKE ?)"
        )
        like = f"%{term}%"
        params.extend([like, like, like, like, like, like, like])

    where_sql = " WHERE " + " AND ".join(where)

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM resources{where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM resources{where_sql} ORDER BY title LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [Resource.from_row(r).to_dict() for r in rows],
    }


# --- Ingesta server-to-server de EduTicTac Play ---

PLAY_PROVIDER = "edutictac-play"
PLAY_TYPES = {"multiple-choice", "true-false", "memory", "matching"}


def _require_play_ingest(request: Request) -> None:
    expected = config.PLAY_INGEST_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Play integration is not configured")
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Play integration authentication required")


def _play_url(value: str, field: str) -> str:
    parsed = urllib.parse.urlparse(value)
    local_http = parsed.scheme == "http" and parsed.hostname.lower() in {"localhost", "127.0.0.1"}
    if (parsed.scheme != "https" and not local_http) or parsed.username or parsed.password or not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"{field} must be an HTTPS URL")
    if parsed.hostname.lower() not in config.PLAY_ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail=f"{field} host is not allowed")
    return value[:2000]


def _play_resource_payload(payload: PlayResourcePublishIn, activity_id: str) -> dict:
    expected_external_id = f"{PLAY_PROVIDER}:{activity_id}"
    if payload.external_id != expected_external_id:
        raise HTTPException(status_code=400, detail="external_id does not match activity id")
    if payload.contract_version != "1":
        raise HTTPException(status_code=400, detail="unsupported contract_version")
    if payload.visibility != "public":
        raise HTTPException(status_code=400, detail="only public activities can be indexed")
    if payload.metadata.type not in PLAY_TYPES:
        raise HTTPException(status_code=400, detail="unsupported Play activity type")
    if payload.package.format != "h5p" or payload.package.delivery != "download_url":
        raise HTTPException(status_code=400, detail="unsupported H5P package delivery")
    if not payload.metadata.title.strip() or len(payload.metadata.title) > 200:
        raise HTTPException(status_code=400, detail="invalid activity title")
    if not payload.idempotency_key.startswith(f"{expected_external_id}:"):
        raise HTTPException(status_code=400, detail="invalid idempotency key")
    if len(payload.version) > 128 or len(payload.idempotency_key) > 256:
        raise HTTPException(status_code=400, detail="version metadata is too long")
    if len(payload.metadata.tags) > 20 or any(len(tag) > 80 for tag in payload.metadata.tags):
        raise HTTPException(status_code=400, detail="too many or too-long tags")
    now = datetime.now(timezone.utc).isoformat()
    metadata = payload.metadata.model_dump()
    return {
        "provider": PLAY_PROVIDER,
        "external_id": expected_external_id,
        "title": payload.metadata.title.strip()[:200],
        "description": payload.metadata.description.strip()[:2000],
        "author": payload.metadata.author_pseudonym.strip()[:120],
        "license": payload.metadata.license.strip()[:80],
        "license_known": 1 if payload.metadata.license.strip() else 0,
        "language": json.dumps([payload.metadata.language.strip()[:12] or "es"], ensure_ascii=False),
        "resource_type": "interactive",
        "format": "h5p",
        "subject": payload.metadata.subject.strip()[:120],
        "educational_stage": payload.metadata.educational_stage.strip()[:120],
        "educational_level": "[]",
        "tags": json.dumps(payload.metadata.tags, ensure_ascii=False),
        "source_url": payload.canonical_url,
        "play_url": payload.canonical_url,
        "download_url": payload.download_url,
        "reuse_url": payload.download_url,
        "thumbnail_url": "",
        "metadata_json": json.dumps({"contract": payload.contract_version, "version": payload.version, "idempotency_key": payload.idempotency_key, **metadata}, ensure_ascii=False),
        "created_at_source": now,
        "updated_at_source": now,
        "indexed_at": now,
        "last_synced_at": now,
        "active": 1,
    }


@app.post("/api/integrations/recursos/activities/{activity_id}/publish")
def publish_play_activity(activity_id: str, payload: PlayResourcePublishIn, request: Request) -> dict:
    _require_play_ingest(request)
    canonical_url = _play_url(payload.canonical_url, "canonical_url")
    download_url = _play_url(payload.download_url, "download_url")
    payload = payload.model_copy(update={"canonical_url": canonical_url, "download_url": download_url})
    row = _play_resource_payload(payload, activity_id)
    with get_conn() as conn:
        previous = conn.execute(
            "SELECT id, active, metadata_json FROM resources WHERE provider = ? AND external_id = ?",
            (PLAY_PROVIDER, row["external_id"]),
        ).fetchone()
        previous_metadata = json.loads(previous["metadata_json"] or "{}") if previous else {}
        previous_key = previous_metadata.get("idempotency_key")
        if previous and previous_key == payload.idempotency_key and previous["active"]:
            status = "published"
            resource_id = previous["id"]
        else:
            columns = ", ".join(row)
            placeholders = ", ".join(f":{key}" for key in row)
            conn.execute(
                f"INSERT INTO resources ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT(provider, external_id) DO UPDATE SET "
                + ", ".join(f"{key} = excluded.{key}" for key in row if key not in {"provider", "external_id"}),
                row,
            )
            saved = conn.execute(
                "SELECT id FROM resources WHERE provider = ? AND external_id = ?",
                (PLAY_PROVIDER, row["external_id"]),
            ).fetchone()
            resource_id = saved["id"]
            status = "updated" if previous else "published"
    return {"resource_id": row["external_id"], "database_id": resource_id, "canonical_url": row["canonical_url"] if "canonical_url" in row else canonical_url, "status": status, "version": payload.version}


@app.get("/api/integrations/recursos/activities/{activity_id}")
def get_play_activity(activity_id: str, request: Request) -> dict:
    _require_play_ingest(request)
    external_id = f"{PLAY_PROVIDER}:{activity_id}"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, active, metadata_json, play_url FROM resources WHERE provider = ? AND external_id = ?",
            (PLAY_PROVIDER, external_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="activity not found")
    metadata = json.loads(row["metadata_json"] or "{}")
    return {"resource_id": external_id, "database_id": row["id"], "canonical_url": row["play_url"], "status": "published" if row["active"] else "unpublished", "version": metadata.get("version", "")}


@app.post("/api/integrations/recursos/activities/{activity_id}/unpublish")
def unpublish_play_activity(activity_id: str, request: Request) -> dict:
    _require_play_ingest(request)
    external_id = f"{PLAY_PROVIDER}:{activity_id}"
    with get_conn() as conn:
        result = conn.execute(
            "UPDATE resources SET active = 0, updated_at_source = ? WHERE provider = ? AND external_id = ?",
            (datetime.now(timezone.utc).isoformat(), PLAY_PROVIDER, external_id),
        )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="activity not found")
    return {"resource_id": external_id, "status": "unpublished"}


# --- Proxy de miniaturas (evita hotlink a terceros) ---

THUMB_DIR = os.environ.get("RECURSOS_THUMB_DIR", "/var/lib/recursos-api/thumb")
THUMB_MAX_BYTES = 5 * 1024 * 1024
THUMB_TTL = 7 * 24 * 3600

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}


def _thumb_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()
    return Path(THUMB_DIR) / digest[:2] / f"{digest}.bin"


@app.get("/api/thumb")
def thumb_proxy(url: str = "") -> Response:
    """Sirve una miniatura remota (allowlist) con caché en disco."""
    if not url:
        raise HTTPException(400, "falta url")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "url inválida")
    if parsed.hostname not in config.THUMB_ALLOWED_HOSTS:
        raise HTTPException(403, "host no permitido")

    cache_path = _thumb_cache_path(url)
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < THUMB_TTL:
        data = cache_path.read_bytes()
    else:
        try:
            data = get_bytes(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("thumb fetch failed %s: %s", url, exc)
            raise HTTPException(502, "no se pudo obtener la imagen") from exc

        if len(data) > THUMB_MAX_BYTES:
            raise HTTPException(413, "imagen demasiado grande")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)

    ext = Path(parsed.path).suffix.lower()
    media_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/admin/sources")
def admin_sources(request: Request) -> dict:
    uid, admin = get_session(request)
    if not admin:
        raise HTTPException(status_code=403, detail="admin required")
    with get_conn() as conn:
        counts = {
            r["provider"]: r["n"]
            for r in conn.execute(
                "SELECT provider, COUNT(*) AS n FROM resources WHERE active = 1 GROUP BY provider"
            )
        }
        last = {}
        for r in conn.execute(
            "SELECT * FROM sync_runs WHERE id IN (SELECT MAX(id) FROM sync_runs GROUP BY provider)"
        ):
            last[r["provider"]] = {
                "status": r["status"],
                "fetched": r["fetched"],
                "created": r["created"],
                "updated": r["updated"],
                "unchanged": r["unchanged"],
                "errors": r["errors"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            }
    return {
        "providers": [
            {
                "provider": p,
                "resources": counts.get(p, 0),
                "last_sync": last.get(p),
            }
            for p in sorted(counts)
        ]
    }


# --- OIDC (Authentik / EduTicTac Commons) ---

@app.get("/api/auth/login")
def auth_login(next_url: str = Query("", alias="next")) -> RedirectResponse:
    if not oidc.enabled():
        raise HTTPException(status_code=503, detail="oidc not configured")
    url, state_cookie = oidc.build_login_url()
    return_to = auth_next_url(next_url)
    response = RedirectResponse(url)
    response.set_cookie(
        oidc.OIDC_STATE_COOKIE, state_cookie,
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/api/auth", max_age=600,
    )
    set_auth_next_cookie(response, return_to)
    return response


@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not oidc.enabled():
        raise HTTPException(status_code=503, detail="oidc not configured")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    state_cookie = request.cookies.get(oidc.OIDC_STATE_COOKIE)
    try:
        info = oidc.handle_callback(state, code, state_cookie)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OIDC callback failed: %s", describe_oidc_error(exc))
        raise HTTPException(status_code=401, detail="oauth callback failed") from exc

    uid = f"oidc:{info['sub']}"
    admin = bool(info["admin"])
    return_to = auth_next_url(request.cookies.get(AUTH_NEXT_COOKIE, ""))
    response = RedirectResponse(return_to)
    set_session_cookie(response, make_session(uid, admin=admin), 60 * 60 * 24 * 30)
    response.delete_cookie(
        oidc.OIDC_STATE_COOKIE,
        path="/api/auth",
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    delete_auth_next_cookie(response)
    return response


@app.get("/api/auth/logout")
def auth_logout() -> RedirectResponse:
    response = RedirectResponse("/")
    delete_session_cookie(response)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    uid, admin = get_session(request)
    logged_in = bool(uid.startswith("oidc:"))
    sub = uid[len("oidc:"):] if logged_in else ""
    return {
        "logged_in": logged_in,
        "admin": admin,
        "sub": sub,
        "admin_sub": sub,
        **_student_payload(uid),
    }


@app.post("/api/student/login")
def student_login(payload: StudentLoginIn, request: Request, response: Response) -> dict:
    if rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    if not EDUTICTAC_ID_API_URL:
        raise HTTPException(status_code=503, detail="student login not configured")
    try:
        id_response = httpx.post(
            f"{EDUTICTAC_ID_API_URL}/api/auth/student",
            json={
                "public_code": payload.public_code.strip(),
                "pin": payload.pin.strip(),
            },
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("student login id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code == 401:
        raise HTTPException(status_code=401, detail="invalid student credentials")
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    identity = data.get("identity") if isinstance(data, dict) else None
    if not isinstance(identity, dict) or not identity.get("id") or not identity.get("public_code"):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    public_code = re.sub(r"[^A-Z0-9]+", "", str(identity["public_code"]).upper())[:12]
    uid = f"student:{public_code}:{identity['id']}"
    set_session_cookie(response, make_session(uid, admin=False), 60 * 60 * 24 * 180)
    return {"ok": True, "student_code": public_code}


@app.post("/api/student/logout")
def student_logout(response: Response) -> dict:
    delete_session_cookie(response)
    return {"ok": True}


@app.post("/api/teacher/student-batches")
def create_student_batch(payload: StudentBatchIn, request: Request) -> dict:
    _require_teacher(request)
    if rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    count = max(1, min(120, int(payload.count or 0)))
    pin_length = 6 if int(payload.pin_length or 4) == 6 else 4
    try:
        id_response = httpx.post(
            f"{EDUTICTAC_ID_API_URL}/api/identities/batch",
            json={"count": count, "pin_length": pin_length, "tenant_id": "recursos"},
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        logger.warning("student batch id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    identities = data.get("identities") if isinstance(data, dict) else None
    group = data.get("group") if isinstance(data, dict) else {}
    if not isinstance(identities, list):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return {
        "batch_id": (group or {}).get("id", ""),
        "credentials": [
            {
                "code": str(item.get("public_code", "")),
                "pin": str(item.get("pin", "")),
            }
            for item in identities
            if isinstance(item, dict) and item.get("public_code") and item.get("pin")
        ],
    }


@app.get("/api/teacher/student-summary")
def student_summary(request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    try:
        id_response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/summary",
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("student summary id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.get("/api/teacher/student-identities")
def student_identities(request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    try:
        id_response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/identities",
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("student identities id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not isinstance(data.get("identities"), list):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.get("/api/teacher/student-identities/by-code/{public_code}")
def student_identity_by_code(public_code: str, request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    safe_code = re.sub(r"[^A-Z0-9]+", "", public_code.upper())[:12]
    if not safe_code:
        raise HTTPException(status_code=400, detail="invalid student code")
    try:
        id_response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/identities/by-code/{safe_code}",
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("student identity lookup id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code == 404:
        raise HTTPException(status_code=404, detail="student identity not found")
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.post("/api/teacher/activity-assignments", status_code=201)
def create_activity_assignment(payload: ActivityAssignmentIn, request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    body = {
        "group_id": str(payload.group_id or "").strip(),
        "app_id": str(payload.app_id or "").strip(),
        "activity_id": str(payload.activity_id or "").strip(),
        "title": str(payload.title or "").strip()[:160],
    }
    if not body["group_id"] or not body["app_id"] or not body["activity_id"]:
        raise HTTPException(status_code=400, detail="group_id, app_id and activity_id are required")
    try:
        id_response = httpx.post(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/activity-assignments",
            json=body,
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("activity assignment id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code == 404:
        raise HTTPException(status_code=404, detail="group not found")
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not isinstance(data.get("assignment"), dict):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.get("/api/teacher/activity-assignments")
def activity_assignments(request: Request, group_id: str = "", limit: int = 100) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    safe_limit = max(1, min(500, int(limit or 100)))
    params: dict[str, str | int] = {"limit": safe_limit}
    if group_id:
        params["group_id"] = group_id.strip()
    try:
        id_response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/activity-assignments",
            params=params,
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("activity assignments id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not isinstance(data.get("assignments"), list):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.get("/api/teacher/activity-assignments/{assignment_id}/results")
def activity_assignment_results(assignment_id: str, request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    try:
        id_response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/teacher/activity-assignments/{assignment_id}/results",
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("activity assignment results id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code == 404:
        raise HTTPException(status_code=404, detail="assignment not found")
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not isinstance(data.get("assignment"), dict) or not isinstance(data.get("results"), list):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return data


@app.post("/api/teacher/student-identities/{identity_id}/regenerate-pin")
def regenerate_student_pin(identity_id: str, payload: PinRegenerateIn, request: Request) -> dict:
    _require_teacher(request)
    if not EDUTICTAC_ID_API_URL or not EDUTICTAC_ID_TEACHER_TOKEN:
        raise HTTPException(status_code=503, detail="student credential service not configured")
    safe_pin_length = 6 if int(payload.pin_length or 4) == 6 else 4
    try:
        id_response = httpx.post(
            f"{EDUTICTAC_ID_API_URL}/api/identities/{identity_id}/regenerate-pin",
            params={"pin_length": safe_pin_length},
            headers={"Authorization": f"Bearer {EDUTICTAC_ID_TEACHER_TOKEN}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        logger.warning("student regenerate pin id api failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=502, detail="student identity service unavailable") from exc
    if id_response.status_code == 404:
        raise HTTPException(status_code=404, detail="student identity not found")
    if id_response.status_code >= 400:
        raise HTTPException(status_code=502, detail="student identity service rejected request")
    data = id_response.json()
    if not isinstance(data, dict) or not data.get("id") or not data.get("pin"):
        raise HTTPException(status_code=502, detail="invalid student identity response")
    return {"id": str(data["id"]), "pin": str(data["pin"])}
