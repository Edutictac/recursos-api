"""Sincronización incremental: upsert por (provider, external_id) + SyncRun."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import get_conn
from .models import Resource, SyncRun
from .providers.base import ResourceProvider

# Campos que NO participan en la comparación updated/unchanged.
# metadata_json puede contener datos volátiles de la fuente (contadores de
# jugadas, valoraciones, ...); thumbnail_url puede variar sin que el recurso
# cambie en esencia (p. ej. la coverImage aleatoria del endpoint de EduHoot).
_VOLATILE = {"indexed_at", "last_synced_at", "active", "metadata_json", "thumbnail_url"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(resource: Resource) -> dict:
    row = resource.to_row()
    for key in _VOLATILE:
        row.pop(key, None)
    return row


def run_sync(provider: ResourceProvider) -> SyncRun:
    run = SyncRun(provider=provider.name)
    run.start()
    started_at = run.started_at

    try:
        with get_conn() as conn:
            existing = {
                row["external_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM resources WHERE provider = ?", (provider.name,)
                )
            }

            for resource in provider.discover():
                run.fetched += 1
                try:
                    resource.indexed_at = _now()
                    resource.last_synced_at = _now()
                    row = resource.to_row()
                    prev = existing.get(resource.external_id)
                    if prev is None:
                        conn.execute(_insert_sql(), row)
                        run.created += 1
                        # Marcar como visto: la fuente puede repetir un external_id
                        # dentro del mismo run (p. ej. paginación con solapamiento).
                        existing[resource.external_id] = dict(row)
                    else:
                        prev_resource = Resource.from_row(prev)
                        if _signature(resource) != _signature(prev_resource):
                            conn.execute(_update_sql(), row)
                            run.updated += 1
                        else:
                            conn.execute(
                                "UPDATE resources SET last_synced_at = ?, active = 1 "
                                "WHERE provider = ? AND external_id = ?",
                                (resource.last_synced_at, provider.name, resource.external_id),
                            )
                            run.unchanged += 1
                except Exception as exc:  # noqa: BLE001 — un recurso roto no detiene el sync
                    run.add_error(f"{getattr(resource, 'external_id', '?')}: {exc}")

            # Recursos del proveedor que ya no aparecen en la fuente → inactivos.
            conn.execute(
                "UPDATE resources SET active = 0 "
                "WHERE provider = ? AND active = 1 AND last_synced_at < ?",
                (provider.name, started_at),
            )

        run.finish("ok" if run.errors == 0 else "partial")
    except Exception as exc:  # noqa: BLE001 — fuente completa inaccesible
        run.add_error(f"fuente {provider.name}: {exc}")
        run.finish("error")

    with get_conn() as conn:
        conn.execute(_insert_run_sql(), run.to_row())

    return run


def _insert_sql() -> str:
    cols = [
        "provider", "external_id", "title", "title_ca", "title_en", "description",
        "description_ca", "description_en",
        "author", "license", "license_known", "language", "resource_type", "format",
        "subject", "educational_stage", "educational_level", "tags", "source_url",
        "play_url", "download_url", "reuse_url", "thumbnail_url", "metadata_json",
        "created_at_source", "updated_at_source", "indexed_at", "last_synced_at", "active",
    ]
    placeholders = ", ".join(":" + c for c in cols)
    return f"INSERT INTO resources ({', '.join(cols)}) VALUES ({placeholders})"


def _update_sql() -> str:
    cols = [
        "title", "title_ca", "title_en", "description", "description_ca", "description_en",
        "author", "license",
        "license_known", "language", "resource_type", "format", "subject",
        "educational_stage", "educational_level", "tags", "source_url", "play_url",
        "download_url", "reuse_url", "thumbnail_url", "metadata_json",
        "created_at_source", "updated_at_source", "indexed_at", "last_synced_at",
        "active",
    ]
    sets = ", ".join(f"{c} = :{c}" for c in cols)
    return (
        f"UPDATE resources SET {sets} "
        "WHERE provider = :provider AND external_id = :external_id"
    )


def _insert_run_sql() -> str:
    cols = ["provider", "started_at", "finished_at", "fetched", "created", "updated",
            "unchanged", "errors", "status", "error_log"]
    placeholders = ", ".join(":" + c for c in cols)
    return f"INSERT INTO sync_runs ({', '.join(cols)}) VALUES ({placeholders})"
