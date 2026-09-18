"""Importador del catálogo federado publicado por Commons."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .db import get_conn
from .models import Resource, SyncRun
from .sync import _insert_run_sql, _insert_sql, _signature, _update_sql

_RESOURCE_FIELDS = frozenset({
    "provider", "external_id", "title", "title_ca", "title_en", "description",
    "description_ca", "description_en", "author", "license", "license_known",
    "language", "resource_type", "format", "subject", "educational_stage",
    "educational_level", "tags", "source_url", "play_url", "download_url",
    "reuse_url", "thumbnail_url", "metadata_json", "created_at_source",
    "updated_at_source", "active",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resource(item: dict[str, Any], now: str) -> Resource:
    provider = str(item.get("provider", "")).strip()
    external_id = str(item.get("external_id", "")).strip()
    if not provider or not external_id:
        raise ValueError("provider y external_id son obligatorios")
    values = {key: item.get(key) for key in _RESOURCE_FIELDS if key in item}
    values["provider"] = provider
    values["external_id"] = external_id
    for key in ("language", "educational_level", "tags"):
        if not isinstance(values.get(key), list):
            values[key] = []
    if not isinstance(values.get("metadata_json"), dict):
        values["metadata_json"] = {}
    values["license_known"] = bool(values.get("license_known", False))
    values["active"] = bool(values.get("active", True))
    values["indexed_at"] = now
    values["last_synced_at"] = now
    return Resource(**values)


def import_catalog(catalog: dict[str, Any]) -> dict[str, int]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise ValueError("el catálogo debe contener un array items")

    grouped = defaultdict(list)
    errors = 0
    now = _now()
    for item in catalog["items"]:
        try:
            if not isinstance(item, dict):
                raise ValueError("cada elemento debe ser un objeto")
            resource = _resource(item, now)
            grouped[resource.provider].append(resource)
        except (TypeError, ValueError):
            errors += 1

    created = updated = unchanged = deactivated = 0
    with get_conn() as conn:
        for provider, resources in grouped.items():
            run = SyncRun(provider=provider)
            run.start()
            existing = {
                row["external_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM resources WHERE provider = ?", (provider,)
                )
            }
            for resource in resources:
                run.fetched += 1
                row = resource.to_row()
                previous = existing.get(resource.external_id)
                if previous is None:
                    conn.execute(_insert_sql(), row)
                    created += 1
                    run.created += 1
                elif _signature(resource) != _signature(Resource.from_row(previous)):
                    conn.execute(_update_sql(), row)
                    updated += 1
                    run.updated += 1
                else:
                    conn.execute(
                        "UPDATE resources SET last_synced_at = ?, active = ? "
                        "WHERE provider = ? AND external_id = ?",
                        (resource.last_synced_at, row["active"], provider, resource.external_id),
                    )
                    unchanged += 1
                    run.unchanged += 1
                existing[resource.external_id] = row

            result = conn.execute(
                "UPDATE resources SET active = 0 "
                "WHERE provider = ? AND active = 1 AND last_synced_at < ?",
                (provider, now),
            )
            deactivated += result.rowcount
            run.finish("ok")
            conn.execute(_insert_run_sql(), run.to_row())

    return {
        "total": len(catalog["items"]),
        "providers": len(grouped),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated": deactivated,
        "errors": errors,
    }
