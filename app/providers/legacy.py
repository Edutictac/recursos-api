"""Proveedor legacy: importa el catálogo curado actual (data/games.json).

No es una fuente viva: es el JSON estático que hoy alimenta la PWA. Se importa
para unificar la fuente de verdad en el índice y poder re-exportar un catálogo
combinado (legacy + jclic + h5p + eduhoot + scorm).
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterator

from ..models import Resource
from ..taxonomy import language_codes
from .base import ResourceProvider


def _stage_from_levels(levels: list[str]) -> str:
    for level in levels or []:
        if "Infantil" in level:
            return "Infantil"
    for level in levels or []:
        if "Secundaria" in level:
            return "Secundaria"
    for level in levels or []:
        if "Primaria" in level:
            return "Primaria"
    return ""


class LegacyProvider(ResourceProvider):
    name = "legacy"
    format = "html5"

    def __init__(self, games_path: str) -> None:
        self.games_path = games_path

    def discover(self) -> Iterator[Resource]:
        with open(self.games_path, encoding="utf-8") as f:
            games = json.load(f)
        if not isinstance(games, list):
            raise ValueError("games.json no es una lista")
        for raw in games:
            yield self.normalize(raw)

    def normalize(self, raw: dict) -> Resource:
        url = raw.get("url", "") or ""
        raw_id = raw.get("id", "")
        external_id = str(raw_id) if raw_id else f"legacy-{hashlib.md5(url.encode()).hexdigest()[:12]}"

        language = raw.get("language", "") or raw.get("languages", "")
        languages = language if isinstance(language, list) else ([language] if language else [])

        levels = raw.get("levels", []) or (raw.get("level") and [raw["level"]]) or []
        is_flash = bool(raw.get("flash"))
        image = raw.get("image", "") or ""

        return Resource(
            provider=self.name,
            external_id=external_id,
            title=raw.get("title", "") or "",
            title_ca=raw.get("title_ca", "") or "",
            title_en=raw.get("title_en", "") or "",
            description=raw.get("notes", "") or "",
            description_ca=raw.get("notes_ca", "") or "",
            description_en=raw.get("notes_en", "") or "",
            author="",
            license="",
            license_known=False,
            language=language_codes(languages),
            resource_type="flash" if is_flash else "html5",
            format="flash" if is_flash else "html5",
            subject=raw.get("area", "") or "General",
            educational_stage=_stage_from_levels(levels),
            educational_level=[str(l) for l in levels],
            tags=[],
            source_url=url,
            play_url=url,
            thumbnail_url=image,
            metadata_json=dict(raw),
            created_at_source=raw.get("fetchedAt", "") or "",
            updated_at_source="",
        )
