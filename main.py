"""Recursos API — backend autoalojado que sustituye a Firebase Firestore+Auth.

API REST (FastAPI + SQLite) para la PWA del Banc de recursos: favoritos,
valoraciones (con resumen agregado), avisos de actividad rota, propuestas de
actividades y modo admin por token. Identidad anónima por cookie firmada (sin Google).

Endpoints:
  GET  /api/health
  GET  /api/preferences
  GET  /api/teacher/favorites
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
import hmac
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
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app import config, oidc
from app.catalog_import import import_catalog
from app.export import export_catalog
from app.db import init_index_schema
from app.httpclient import get_bytes, get_json_with_headers
from app.models import Resource
from app.books import Publications
from app.providers.eduhoot import EduHootProvider

DB_PATH = os.environ.get("RECURSOS_DB", "/var/lib/recursos-api/recursos.db")
SESSION_SECRET = os.environ.get("RECURSOS_SECRET", "")
SESSION_COOKIE = "recursos_session"
AUTH_NEXT_COOKIE = "recursos_auth_next"
COOKIE_SECURE = os.environ.get("RECURSOS_COOKIE_SECURE", "1").lower() not in {"0", "false", "no"}
EDUTICTAC_ID_API_URL = os.environ.get("EDUTICTAC_ID_API_URL", "").rstrip("/")
EDUTICTAC_ID_TEACHER_TOKEN = os.environ.get("EDUTICTAC_ID_TEACHER_TOKEN", "")
ALLOWED_AUTH_NEXT_HOSTS = {"edutictac.es", "recursos.edutictac.es"}
# Sessió central d'EduTicTac (Commons). Amb "1", el Banc reconeix la sessió de Commons,
# entra i ix per Commons i deixa d'acceptar les sessions pròpies de docent o alumne.
# Amb "0" torna el login propi del Banc (vegeu README, vuelta atrás).
CENTRAL_SESSION = os.environ.get("RECURSOS_CENTRAL_SESSION", "0").lower() in {"1", "true", "yes"}
COMMONS_PUBLIC_URL = os.environ.get("RECURSOS_COMMONS_URL", "https://commons.edutictac.es").rstrip("/")
CENTRAL_COOKIES = ("edutictac_oidc", "edutictac_id")
CENTRAL_CACHE_SECONDS = 30
RESOURCES_IMPORT_TOKEN = os.environ.get("RESOURCES_IMPORT_TOKEN", "")
EXPORT_CATALOG_DIR = os.environ.get("RECURSOS_EXPORT_DIR", "").strip()

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
    # Existing public installations must not restore demonstration entries during updates.
    if os.environ.get("RECURSOS_SEED", "1") == "0":
        return
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
book_publications = Publications(get_conn,
    os.environ.get("LLIBRES_PUBLIC_BASE_URL", config.APP_BASE_URL),
    config.EDUTICTAC_ID_APP_TOKEN)
app.include_router(book_publications.router())


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


# --- Sessió central (Commons) ---
#
# El Banc no valida les galetes centrals: les reenvia a l'id-api per la xarxa interna,
# amb el testimoni d'aplicació, i rep la identitat estable. El `sub` d'Authentik és el
# mateix en tots els proveïdors (`hashed_user_id`), així que `oidc:<sub>` coincidix
# amb les dades que el Banc ja tenia de cada docent.

_central_cache: dict[str, tuple[float, tuple[str, bool, str] | None]] = {}


def central_identity(request: Request) -> tuple[str, bool, str] | None:
    """(uid, admin, nom visible) de la sessió central, o None."""
    if not CENTRAL_SESSION or not EDUTICTAC_ID_API_URL or not config.EDUTICTAC_ID_APP_TOKEN:
        return None
    cookies = {name: request.cookies[name] for name in CENTRAL_COOKIES if request.cookies.get(name)}
    if not cookies:
        return None
    key = hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest()
    now = time.monotonic()
    cached = _central_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        response = httpx.get(
            f"{EDUTICTAC_ID_API_URL}/api/internal/session/resolve",
            headers={
                "Authorization": f"Bearer {config.EDUTICTAC_ID_APP_TOKEN}",
                "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
            },
            timeout=3,
        )
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        # Sense id-api, ningú queda identificat per la sessió central (mai a l'inrevés).
        logger.warning("central session unavailable: %s", exc.__class__.__name__)
        return None
    result = None
    if data.get("kind") == "oidc" and data.get("sub"):
        sub = str(data["sub"])
        admin = data.get("role") == "administrador" or oidc.is_admin_sub(sub)
        result = (f"oidc:{sub}", admin, str(data.get("display_name") or ""))
    elif data.get("kind") == "student" and data.get("id") and data.get("public_code"):
        public_code = re.sub(r"[^A-Z0-9]+", "", str(data["public_code"]).upper())[:12]
        result = (f"student:{public_code}:{data['id']}", False, "")
    if len(_central_cache) > 1000:
        _central_cache.clear()
    _central_cache[key] = (now + CENTRAL_CACHE_SECONDS, result)
    return result


def local_session(request: Request) -> tuple[str, bool]:
    """Sessió pròpia del Banc. Amb la sessió central només val la identitat anònima."""
    sess = parse_session(request.cookies.get(SESSION_COOKIE))
    if not sess or not sess.get("uid"):
        return "", False
    uid = sess["uid"]
    if CENTRAL_SESSION and (uid.startswith("oidc:") or uid.startswith("student:")):
        return "", False
    return uid, session_admin(uid, bool(sess.get("admin")))


def merge_anonymous(anonymous_uid: str, uid: str) -> None:
    """Passa favorits, valoracions i avisos anònims al compte, sense duplicar res.

    Si el compte ja tenia una valoració o un avís del mateix recurs, el seu queda i
    l'anònim es descompta del resum perquè les mitjanes continuen sent correctes.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (user_id, game_key, created_at) "
            "SELECT ?, game_key, created_at FROM favorites WHERE user_id = ?",
            (uid, anonymous_uid),
        )
        conn.execute("DELETE FROM favorites WHERE user_id = ?", (anonymous_uid,))
        duplicated = conn.execute(
            "SELECT a.game_key, a.value FROM ratings a JOIN ratings b "
            "ON b.game_key = a.game_key AND b.user_id = ? WHERE a.user_id = ?",
            (uid, anonymous_uid),
        ).fetchall()
        for game_key, value in duplicated:
            conn.execute(
                "UPDATE rating_summary SET sum = MAX(sum - ?, 0), count = MAX(count - 1, 0), "
                "avg = CASE WHEN count - 1 > 0 THEN (sum - ?) * 1.0 / (count - 1) ELSE 0 END "
                "WHERE game_key = ?",
                (value, value, game_key),
            )
        conn.execute(
            "INSERT OR IGNORE INTO ratings (user_id, game_key, value, updated_at) "
            "SELECT ?, game_key, value, updated_at FROM ratings WHERE user_id = ?",
            (uid, anonymous_uid),
        )
        conn.execute("DELETE FROM ratings WHERE user_id = ?", (anonymous_uid,))
        reported = conn.execute(
            "SELECT a.game_key FROM reports a JOIN reports b "
            "ON b.game_key = a.game_key AND b.user_id = ? WHERE a.user_id = ?",
            (uid, anonymous_uid),
        ).fetchall()
        for (game_key,) in reported:
            conn.execute(
                "UPDATE broken_reports SET count = MAX(count - 1, 0) WHERE game_key = ?", (game_key,)
            )
        conn.execute(
            "INSERT OR IGNORE INTO reports (user_id, game_key, reported_at) "
            "SELECT ?, game_key, reported_at FROM reports WHERE user_id = ?",
            (uid, anonymous_uid),
        )
        conn.execute("DELETE FROM reports WHERE user_id = ?", (anonymous_uid,))


def adopt_central(request: Request, response: Response | None) -> tuple[str, bool] | None:
    """Identitat central; si el navegador portava un uid anònim, el fusiona i l'oblida."""
    central = central_identity(request)
    if not central:
        if response is not None and CENTRAL_SESSION and request.cookies.get(SESSION_COOKIE) and not local_session(request)[0]:
            # Una sessió pròpia antiga de docent o alumne ja no val: s'esborra.
            delete_session_cookie(response)
        return None
    uid, admin, _name = central
    local_uid, _local_admin = local_session(request)
    if local_uid and not local_uid.startswith(("oidc:", "student:")):
        merge_anonymous(local_uid, uid)
        if response is not None:
            delete_session_cookie(response)
    elif response is not None and request.cookies.get(SESSION_COOKIE) and not local_uid:
        # Una sessió pròpia antiga de docent o alumne ja no val: s'esborra.
        delete_session_cookie(response)
    return uid, admin


def get_session(request: Request) -> tuple[str, bool]:
    """Devuelve (uid, admin): primer la sessió central, després la pròpia del Banc."""
    central = central_identity(request)
    if central:
        return central[0], central[1]
    return local_session(request)


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
    central = adopt_central(request, response)
    if central:
        return central
    local_uid, local_admin = local_session(request)
    if local_uid:
        return local_uid, local_admin
    uid = secrets.token_hex(16)
    set_session_cookie(response, make_session(uid, admin=False), 60 * 60 * 24 * 365)
    return uid, False


def resolve_community_identity(request: Request, response: Response) -> Identity:
    if request.method == "GET" and request.url.path.endswith("/preferences"):
        central = adopt_central(request, response)
        uid, admin = central if central else local_session(request)
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
        admin_unhide_path="/admin/resources/unhide",
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


def _teacher_upload_resource(row: sqlite3.Row) -> Resource:
    return Resource.from_row(row)


def _document_filename(request: Request) -> str:
    raw = request.headers.get("X-Upload-Filename", "documento")
    try:
        raw = urllib.parse.unquote(raw)
    except Exception:  # noqa: BLE001
        raw = "documento"
    return Path(raw).name[:180] or "documento"


def _document_url(document_id: str) -> str:
    return f"{config.APP_BASE_URL}/api/documents/{document_id}/download"


def _valid_document_proxy(request: Request) -> bool:
    expected = config.EDUTICTAC_ID_APP_TOKEN
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return bool(expected and token and compare_digest(token, expected))


@app.post("/api/teacher/documents", status_code=201)
async def upload_teacher_document(request: Request) -> dict:
    """Guarda un documento docente como recurso privado y descargable por enlace."""
    teacher_id = _require_teacher(request)
    filename = _document_filename(request)
    extension = Path(filename).suffix.lower()
    if extension not in config.DOCUMENTS_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="formato de documento no permitido")
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > config.DOCUMENTS_MAX_BYTES:
        raise HTTPException(status_code=413, detail="documento demasiado grande")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="documento vacío")
    if len(data) > config.DOCUMENTS_MAX_BYTES:
        raise HTTPException(status_code=413, detail="documento demasiado grande")

    document_id = secrets.token_urlsafe(16)
    title = (request.headers.get("X-Upload-Title") or Path(filename).stem).strip()[:200]
    subject = (request.headers.get("X-Upload-Subject") or "Documentos docentes").strip()[:120]
    language = (request.headers.get("X-Upload-Language") or "").strip()[:12]
    storage_dir = Path(config.DOCUMENTS_DIR)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_name = f"{document_id}{extension}"
    storage_path = storage_dir / storage_name
    storage_path.write_bytes(data)
    now = datetime.now(timezone.utc).isoformat()
    external_id = f"teacher-upload:{document_id}"
    metadata = {
        "owner_uid": teacher_id,
        "storage_name": storage_name,
        "original_name": filename,
        "size": len(data),
    }
    row = {
        "provider": "teacher-upload",
        "external_id": external_id,
        "title": title or filename,
        "description": "Documento privado del docente",
        "author": teacher_id,
        "language": json.dumps([language] if language else [], ensure_ascii=False),
        "resource_type": "document",
        "format": extension[1:],
        "subject": subject,
        "tags": "[]",
        "source_url": _document_url(document_id),
        "play_url": "",
        "download_url": _document_url(document_id),
        "reuse_url": _document_url(document_id),
        "thumbnail_url": "",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "created_at_source": now,
        "updated_at_source": now,
        "indexed_at": now,
        "last_synced_at": now,
        "active": 1,
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    with get_conn() as conn:
        conn.execute(f"INSERT INTO resources ({columns}) VALUES ({placeholders})", row)
        saved = conn.execute(
            "SELECT * FROM resources WHERE provider = ? AND external_id = ?",
            ("teacher-upload", external_id),
        ).fetchone()
    return _teacher_upload_resource(saved).to_dict()


@app.get("/api/documents/{document_id}/download")
def download_teacher_document(document_id: str, request: Request) -> FileResponse:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM resources WHERE provider = 'teacher-upload' AND external_id = ? AND active = 1",
            (f"teacher-upload:{document_id}",),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="documento no encontrado")
    metadata = json.loads(row["metadata_json"] or "{}")
    owner_uid = metadata.get("owner_uid", "")
    uid, _ = get_session(request)
    if not _valid_document_proxy(request) and uid != owner_uid:
        raise HTTPException(status_code=401, detail="autenticación requerida")
    filename = Path(metadata.get("original_name") or "documento").name
    path = Path(config.DOCUMENTS_DIR) / metadata.get("storage_name", "")
    if not path.is_file() or path.parent.resolve() != Path(config.DOCUMENTS_DIR).resolve():
        raise HTTPException(status_code=404, detail="documento no encontrado")
    return FileResponse(
        path,
        filename=filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


def _eduhoot_resource_matches(resource: Resource, q: str, provider: str, format: str,
                              subject: str, stage: str, language: str, license: str,
                              license_known: bool | None) -> bool:
    if provider and resource.provider != provider:
        return False
    if format and resource.format != format:
        return False
    if subject and resource.subject != subject:
        return False
    if stage and resource.educational_stage != stage:
        return False
    if language and language not in resource.language:
        return False
    if license and resource.license != license:
        return False
    if license_known is not None and resource.license_known != license_known:
        return False
    term = _norm(q).strip()
    if term:
        haystack = _norm(" ".join([
            resource.title, resource.title_ca, resource.description,
            resource.description_ca, resource.author, " ".join(resource.tags),
            resource.external_id,
        ]))
        if term not in haystack:
            return False
    return True


def _load_eduhoot_resources(teacher_sub: str) -> list[Resource]:
    if not config.EDUHOOT_RESOURCES_TOKEN:
        return []
    payload = get_json_with_headers(
        f"{config.EDUHOOT_RESOURCES_URL}/api/integrations/recursos/quizzes",
        {
            "Authorization": f"Bearer {config.EDUHOOT_RESOURCES_TOKEN}",
            **({"X-Teacher-Sub": teacher_sub} if teacher_sub else {}),
        },
    )
    if not isinstance(payload, list):
        raise ValueError("catálogo EduHoot no válido")
    provider = EduHootProvider()
    return [provider.normalize(raw) for raw in payload if provider._is_educational(raw)]


@app.get("/api/resources")
def list_resources(
    request: Request,
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
    # In a local Commons deployment, EduHoot IDs belong to the local database.
    # The downloaded index carries IDs/URLs from the public server instead.
    if os.environ.get("EDUHOOT_LOCAL_CATALOG") == "1":
        where.append("provider != 'eduhoot'")
    params: list = []
    uid, _ = get_session(request)
    teacher_sub = uid[len("oidc:"):] if uid.startswith("oidc:") else ""
    if teacher_sub:
        where.append("(provider != 'teacher-upload' OR json_extract(metadata_json, '$.owner_uid') = ?)")
        params.append(uid)
    else:
        where.append("provider != 'teacher-upload'")

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
            f"SELECT * FROM resources{where_sql} ORDER BY title",
            params,
        ).fetchall()

    indexed = [Resource.from_row(row) for row in rows]
    indexed_ids = {(resource.provider, resource.external_id) for resource in indexed}
    dynamic = []
    if not provider or provider == "eduhoot":
        try:
            dynamic = _load_eduhoot_resources(teacher_sub)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EduHoot catalog unavailable: %s", exc)
    for resource in dynamic:
        if (resource.provider, resource.external_id) in indexed_ids:
            continue
        if _eduhoot_resource_matches(resource, q, provider, format, subject, stage, language, license, license_known):
            indexed.append(resource)
    indexed.sort(key=lambda resource: resource.title.lower())
    page = indexed[offset:offset + limit]
    return {
        "total": len(indexed),
        "offset": offset,
        "limit": limit,
        "items": [resource.to_dict() for resource in page],
    }


@app.get("/api/teacher/favorites")
def list_teacher_favorites(request: Request, limit: int = 200) -> dict:
    """Resolve los favoritos del docente a recursos completos del índice."""
    uid = _require_teacher(request)
    limit = max(1, min(limit, 200))
    with get_conn() as conn:
        favorite_keys = [row["game_key"] for row in conn.execute(
            "SELECT game_key FROM favorites WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (uid, limit),
        ).fetchall()]
        rows = conn.execute(
            """
            SELECT r.*
            FROM favorites f
            JOIN resources r ON (
                r.external_id = f.game_key
                OR r.source_url = f.game_key
                OR r.play_url = f.game_key
                OR r.reuse_url = f.game_key
                OR r.download_url = f.game_key
            )
            WHERE f.user_id = ? AND r.active = 1
              AND (r.provider != 'teacher-upload' OR json_extract(r.metadata_json, '$.owner_uid') = ?)
            ORDER BY f.created_at DESC, r.title
            LIMIT ?
            """,
            (uid, uid, limit),
        ).fetchall()
    resources = [Resource.from_row(row) for row in rows]
    known = {resource.external_id for resource in resources}
    teacher_sub = uid[len("oidc:"):] if uid.startswith("oidc:") else ""
    try:
        dynamic = _load_eduhoot_resources(teacher_sub)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EduHoot favorites unavailable: %s", exc)
        dynamic = []
    favorite_key_set = set(favorite_keys)
    resources.extend(
        resource for resource in dynamic
        if resource.external_id not in known
        and (resource.external_id in favorite_key_set
             or resource.source_url in favorite_key_set
             or resource.play_url in favorite_key_set)
    )
    return {"total": len(resources), "items": [resource.to_dict() for resource in resources[:limit]]}


@app.post("/api/internal/import-catalog")
async def import_catalog_endpoint(request: Request) -> dict:
    """Ingesta del catálogo federado que Commons descarga y verifica por hash."""
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {RESOURCES_IMPORT_TOKEN}" if RESOURCES_IMPORT_TOKEN else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid import token")
    try:
        body = await request.json()
        result = import_catalog(body.get("catalog"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/api/internal/export-catalog")
async def export_catalog_endpoint(request: Request) -> dict:
    """Endpoint interno para regenerar games.json/games-home.json tras un import."""
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {RESOURCES_IMPORT_TOKEN}" if RESOURCES_IMPORT_TOKEN else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid import token")
    if not EXPORT_CATALOG_DIR:
        raise HTTPException(status_code=503, detail="export-catalog is not configured")
    return export_catalog(out_dir=EXPORT_CATALOG_DIR)


# --- Ingesta server-to-server de EduTicTac Play ---

PLAY_PROVIDER = "edutictac-play"
PLAY_TYPES = {"multiple-choice", "true-false", "memory", "matching", "classify", "ordering",
              "fill-blanks", "find-words", "drag-text", "mark-words", "dialog-cards", "crossword", "compound"}


def _require_play_ingest(request: Request) -> None:
    expected = config.PLAY_INGEST_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Play integration is not configured")
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token or not compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Play integration authentication required")


def _play_url(value: str, field: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port or (80 if parsed.scheme == "http" else 443)
        public = urllib.parse.urlsplit(config.PLAY_PUBLIC_URL)
        public_port = public.port or 80
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} is not a valid URL")
    configured_http = (
        parsed.scheme == public.scheme == "http"
        and bool(public.hostname)
        and hostname == public.hostname.lower()
        and port == public_port
        and not (public.username or public.password or public.query or public.fragment)
    )
    local_http = parsed.scheme == "http" and (
        hostname in {"localhost", "127.0.0.1"} or configured_http
    )
    if (parsed.scheme != "https" and not local_http) or parsed.username or parsed.password or not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"{field} must be an HTTPS URL")
    if hostname not in config.PLAY_ALLOWED_HOSTS:
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
    if CENTRAL_SESSION:
        # S'entra en Commons i es torna al Banc (o a la pàgina d'edutictac.es que ho demana).
        return_to = auth_next_url(next_url)
        if return_to == "/":
            return_to = f"{config.APP_BASE_URL}/"
        return RedirectResponse(
            f"{COMMONS_PUBLIC_URL}/api/auth/oidc/login?next={urllib.parse.quote(return_to, safe='')}"
        )
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
def auth_me(request: Request, response: Response) -> dict:
    central = adopt_central(request, response)
    uid, admin = central if central else local_session(request)
    logged_in = bool(uid.startswith("oidc:"))
    sub = uid[len("oidc:"):] if logged_in else ""
    return {
        "logged_in": logged_in,
        "admin": admin,
        "sub": sub,
        "admin_sub": sub,
        "central": CENTRAL_SESSION,
        "commons_url": COMMONS_PUBLIC_URL if CENTRAL_SESSION else "",
        **_student_payload(uid),
    }


@app.post("/api/student/login")
def student_login(payload: StudentLoginIn, request: Request, response: Response) -> dict:
    if CENTRAL_SESSION:
        # Amb la sessió central, l'alumnat entra amb codi i PIN en Commons.
        raise HTTPException(status_code=409, detail="use central login")
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
