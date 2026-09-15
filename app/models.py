"""Modelo común de recurso y registro de sincronización."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


@dataclass
class Resource:
    """Entidad normalizada EduTicTac. No pierde el metadato original (metadata_json)."""

    provider: str
    external_id: str
    title: str = ""
    title_ca: str = ""
    title_en: str = ""
    description: str = ""
    description_ca: str = ""
    description_en: str = ""
    author: str = ""
    license: str = ""
    license_known: bool = False
    language: list[str] = field(default_factory=list)
    resource_type: str = ""
    format: str = ""
    subject: str = ""
    educational_stage: str = ""
    educational_level: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_url: str = ""
    play_url: str = ""
    download_url: str = ""
    reuse_url: str = ""
    thumbnail_url: str = ""
    metadata_json: dict = field(default_factory=dict)
    created_at_source: str = ""
    updated_at_source: str = ""
    indexed_at: str = ""
    last_synced_at: str = ""
    active: bool = True

    def to_row(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "title": self.title,
            "title_ca": self.title_ca,
            "title_en": self.title_en,
            "description": self.description,
            "description_ca": self.description_ca,
            "description_en": self.description_en,
            "author": self.author,
            "license": self.license,
            "license_known": 1 if self.license_known else 0,
            "language": _dumps(self.language),
            "resource_type": self.resource_type,
            "format": self.format,
            "subject": self.subject,
            "educational_stage": self.educational_stage,
            "educational_level": _dumps(self.educational_level),
            "tags": _dumps(self.tags),
            "source_url": self.source_url,
            "play_url": self.play_url,
            "download_url": self.download_url,
            "reuse_url": self.reuse_url,
            "thumbnail_url": self.thumbnail_url,
            "metadata_json": json.dumps(self.metadata_json, ensure_ascii=False),
            "created_at_source": self.created_at_source,
            "updated_at_source": self.updated_at_source,
            "indexed_at": self.indexed_at,
            "last_synced_at": self.last_synced_at,
            "active": 1 if self.active else 0,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Resource":
        return cls(
            provider=row["provider"],
            external_id=row["external_id"],
            title=row["title"] or "",
            title_ca=row["title_ca"] or "",
            title_en=row["title_en"] or "",
            description=row["description"] or "",
            description_ca=row["description_ca"] or "",
            description_en=row["description_en"] or "",
            author=row["author"] or "",
            license=row["license"] or "",
            license_known=bool(row["license_known"]),
            language=_loads(row["language"], []),
            resource_type=row["resource_type"] or "",
            format=row["format"] or "",
            subject=row["subject"] or "",
            educational_stage=row["educational_stage"] or "",
            educational_level=_loads(row["educational_level"], []),
            tags=_loads(row["tags"], []),
            source_url=row["source_url"] or "",
            play_url=row["play_url"] or "",
            download_url=row["download_url"] or "",
            reuse_url=row["reuse_url"] or "",
            thumbnail_url=row["thumbnail_url"] or "",
            metadata_json=_loads(row["metadata_json"], {}),
            created_at_source=row["created_at_source"] or "",
            updated_at_source=row["updated_at_source"] or "",
            indexed_at=row["indexed_at"] or "",
            last_synced_at=row["last_synced_at"] or "",
            active=bool(row["active"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Representación JSON para la API."""
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "title": self.title,
            "title_ca": self.title_ca,
            "title_en": self.title_en,
            "description": self.description,
            "description_ca": self.description_ca,
            "description_en": self.description_en,
            "author": self.author,
            "license": self.license,
            "license_known": self.license_known,
            "language": self.language,
            "resource_type": self.resource_type,
            "format": self.format,
            "subject": self.subject,
            "educational_stage": self.educational_stage,
            "educational_level": self.educational_level,
            "tags": self.tags,
            "source_url": self.source_url,
            "play_url": self.play_url,
            "download_url": self.download_url,
            "reuse_url": self.reuse_url,
            "thumbnail_url": self.thumbnail_url,
            "metadata_json": self.metadata_json,
            "created_at_source": self.created_at_source,
            "updated_at_source": self.updated_at_source,
            "indexed_at": self.indexed_at,
            "last_synced_at": self.last_synced_at,
            "active": self.active,
        }


@dataclass
class SyncRun:
    provider: str
    started_at: str = ""
    finished_at: str = ""
    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    status: str = "running"
    error_log: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.started_at = _now()
        self.status = "running"

    def finish(self, status: str | None = None) -> None:
        self.finished_at = _now()
        if status:
            self.status = status
        elif self.status == "running":
            self.status = "ok"

    def add_error(self, message: str) -> None:
        self.errors += 1
        self.error_log.append(message[:2000])

    def to_row(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "status": self.status,
            "error_log": _dumps(self.error_log),
        }
