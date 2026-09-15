"""Exporta el catálogo unificado (legacy + proveedores) al formato del frontend.

Genera `games.json` y `games-home.json` (mismo formato que hoy consume la PWA)
a partir de la tabla `resources`, para no tocar el frontend en esta fase.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlparse

from . import config
from .db import get_conn, init_index_schema
from .models import Resource
from .providers.legacy import LegacyProvider
from .sync import run_sync

PROVIDER_PRIORITY = {"legacy": 0, "jclic": 1, "h5p": 2, "eduhoot": 3, "scorm": 4}

# Los jclic legacy de clic.xtec.cat/projects/ están cubiertos (con mejores
# metadatos) por el provider jclic; se excluyen para no duplicar.
LEGACY_JCLIC_PREFIX = "https://clic.xtec.cat/projects/"


def _thumb_url(url: str) -> str:
    """Miniaturas remotas de hosts permitidos → proxy local (evita hotlink)."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    host = urlparse(url).hostname or ""
    if host in config.THUMB_ALLOWED_HOSTS:
        return f"{config.APP_BASE_URL}/api/thumb?url={quote(url)}"
    return url


def _to_game(r: Resource) -> dict:
    langs = [str(l) for l in r.language if l]
    game: dict = {
        "id": r.external_id,
        "title": r.title or "",
        "area": r.subject or "General",
        "url": r.play_url or r.source_url or "",
        "notes": r.description or "",
        "levels": [str(l) for l in r.educational_level],
        "source": r.provider,
        "format": r.format,
    }
    if langs:
        game["language"] = langs[0] if len(langs) == 1 else langs
    else:
        game["language"] = ""
    if r.title_ca:
        game["title_ca"] = r.title_ca
    if r.title_en:
        game["title_en"] = r.title_en
    if r.description_ca:
        game["notes_ca"] = r.description_ca
    if r.description_en:
        game["notes_en"] = r.description_en
    if r.thumbnail_url:
        game["image"] = _thumb_url(r.thumbnail_url)
    if r.format == "flash":
        game["flash"] = True
    return game


def export_catalog(
    games_path: str,
    out_dir: str,
    home_size: int = 48,
    exclude_legacy_jclic: bool = True,
) -> dict:
    init_index_schema()

    run_sync(LegacyProvider(games_path))

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM resources WHERE active = 1").fetchall()

    entries = [(r["id"], Resource.from_row(r)) for r in rows]
    if exclude_legacy_jclic:
        entries = [
            (rid, res)
            for rid, res in entries
            if not (res.provider == "legacy" and res.source_url.startswith(LEGACY_JCLIC_PREFIX))
        ]

    entries.sort(key=lambda t: (PROVIDER_PRIORITY.get(t[1].provider, 9), t[0]))
    games = [_to_game(res) for _, res in entries]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "games.json").write_text(
        json.dumps(games, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    home = [g for g in games if g.get("image")][:home_size]
    (out / "games-home.json").write_text(
        json.dumps(home, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    by_provider: dict[str, int] = {}
    for _, res in entries:
        by_provider[res.provider] = by_provider.get(res.provider, 0) + 1
    return {"total": len(games), "home": len(home), "by_provider": by_provider}
