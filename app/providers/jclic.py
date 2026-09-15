"""Proveedor JClic: indexa el catálogo oficial (clic.xtec.cat/projects/projects.json).

Fuente estructurada, sin scraping. Cada proyecto tiene title, author, date,
langCodes, levelCodes, areaCodes, mainFile, cover/thumbnail.

Además descarga el fichero .jclic de cada proyecto para extraer la descripción
(principal + multilingüe) y la licencia, con caché en disco para no repetir la
descarga en cada sync.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from defusedxml import ElementTree as DET

from .. import config, taxonomy
from ..httpclient import get_json, get_text
from ..models import Resource
from .base import ResourceProvider

JCLIC_BASE = "https://clic.xtec.cat"

# TTL de la caché de descripciones (30 días) y pausa mínima entre peticiones .jclic.
_CACHE_TTL = 30 * 24 * 3600
_RATE_INTERVAL = 0.25
_SAVE_EVERY = 50

_cache: dict[str, dict] | None = None
_last_fetch = 0.0
_fetch_since_save = 0


def _extract_description(xml_text: str) -> dict[str, str]:
    """Extrae descripción (principal + multilingüe) y licencia de un .jclic."""
    result = {"description": "", "description_ca": "", "description_en": "", "license": ""}
    try:
        root = DET.fromstring(xml_text)
    except Exception:
        return result
    settings = root.find(".//settings")
    if settings is None:
        return result

    def _texts(el) -> str:
        if el is None:
            return ""
        parts = [("".join(p.itertext())).strip() for p in el.findall(".//p")]
        if not parts:
            parts = [(el.text or "").strip()]
        return " ".join(p for p in parts if p).strip()

    primary = _texts(settings.find("description"))
    multi: dict[str, str] = {}
    descs = settings.find("descriptions")
    if descs is not None:
        for d in descs.findall("description"):
            lang = (d.get("language") or "").strip().lower()
            if lang:
                multi[lang] = _texts(d)

    result["description"] = multi.get("es") or primary
    result["description_ca"] = multi.get("ca") or ""
    result["description_en"] = multi.get("en") or ""

    lic = settings.find("license")
    if lic is not None:
        result["license"] = (lic.get("type") or "").strip()

    return result


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    path = Path(config.JCLIC_DESCRIPTIONS_CACHE)
    try:
        if path.exists():
            _cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    path = Path(config.JCLIC_DESCRIPTIONS_CACHE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _throttle() -> None:
    global _last_fetch
    now = time.monotonic()
    wait = _last_fetch + _RATE_INTERVAL - now
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.monotonic()


def _project_description(jclic_file_url: str) -> dict[str, str]:
    """Devuelve {description, description_ca, description_en, license} con caché."""
    cache = _load_cache()
    now = time.time()
    cached = cache.get(jclic_file_url)
    if isinstance(cached, dict) and now - float(cached.get("fetched_at", 0)) < _CACHE_TTL:
        return cached
    _throttle()
    try:
        extracted = _extract_description(get_text(jclic_file_url))
    except Exception:
        # Fallo de red/parseo: sin descripción, no detiene el sync.
        return {"description": "", "description_ca": "", "description_en": "", "license": ""}
    extracted["fetched_at"] = now
    cache[jclic_file_url] = extracted

    global _fetch_since_save
    _fetch_since_save += 1
    if _fetch_since_save >= _SAVE_EVERY:
        _save_cache()
        _fetch_since_save = 0
    return extracted


class JClicProvider(ResourceProvider):
    name = "jclic"
    format = "jclic"

    def discover(self) -> Iterator[Resource]:
        data = get_json(config.JCLIC_PROJECTS_URL)
        if not isinstance(data, list):
            raise ValueError("projects.json no es una lista")
        for raw in data:
            yield self.normalize(raw)
        _save_cache()

    def normalize(self, raw: dict) -> Resource:
        path = raw.get("path", "")
        project_id = raw.get("id", path)
        title = raw.get("title", "")
        author = raw.get("author", "")
        date = raw.get("date", "")
        lang_codes = raw.get("langCodes", [])
        level_codes = raw.get("levelCodes", [])
        area_codes = raw.get("areaCodes", [])
        main_file = raw.get("mainFile", "")

        # Proyecto remoto (clic.xtec.cat) reproducido por el visor local.
        project_url = f"{JCLIC_BASE}/projects/{path}/{main_file}" if path and main_file else ""
        play_url = (
            f"{config.APP_BASE_URL}/jclic.html"
            f"?project={quote(project_url)}&title={quote(title)}"
            if project_url
            else ""
        )
        cover = raw.get("coverWebp") or raw.get("cover") or raw.get("thumbnail") or ""
        thumbnail = f"{JCLIC_BASE}/projects/{path}/{cover}" if path and cover else ""

        language = taxonomy.jclic_language(lang_codes)
        levels = taxonomy.jclic_levels(level_codes)

        description = ""
        description_ca = ""
        description_en = ""
        license_type = ""
        if project_url:
            desc = _project_description(project_url)
            description = desc.get("description", "")
            description_ca = desc.get("description_ca", "")
            description_en = desc.get("description_en", "")
            license_type = desc.get("license", "")

        return Resource(
            provider=self.name,
            external_id=str(project_id),
            title=title,
            author=author,
            language=language,
            resource_type="jclic",
            format=self.format,
            subject=taxonomy.jclic_subject(area_codes),
            educational_stage=taxonomy.jclic_stage(level_codes),
            educational_level=levels,
            tags=[taxonomy.normalize_tag(title)] if title else [],
            source_url=f"{JCLIC_BASE}/projects/{path}/" if path else "",
            play_url=play_url,
            thumbnail_url=thumbnail,
            metadata_json=dict(raw),
            created_at_source=date,
            license=license_type,
            license_known=bool(license_type),
            description=description,
            description_ca=description_ca,
            description_en=description_en,
        )
