"""Cliente HTTP con timeouts, retries, rate limiting y User-Agent identificable.

Respeta a los servidores externos: sin scraping agresivo, con reintentos
razonables y espaciado entre peticiones.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from . import config


def _client(timeout: float | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout or config.HTTP_TIMEOUT,
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET + parse JSON con reintentos y rate limiting.

    Lanza httpx.HTTPError si tras los reintentos no hay respuesta 2xx.
    """
    params = params or {}
    last_exc: Exception | None = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            with _client() as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt < config.HTTP_RETRIES:
                time.sleep(attempt * config.HTTP_RATE_DELAY * 2)
    raise last_exc if last_exc else RuntimeError(f"GET {url} failed")


def get_text(url: str) -> str:
    """GET + texto (UTF-8) con reintentos, para fuentes XML/HTML."""
    last_exc: Exception | None = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            with httpx.Client(
                timeout=config.HTTP_TIMEOUT,
                headers={"User-Agent": config.USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < config.HTTP_RETRIES:
                time.sleep(attempt * config.HTTP_RATE_DELAY * 2)
    raise last_exc if last_exc else RuntimeError(f"GET {url} failed")


def get_bytes(url: str, params: dict[str, Any] | None = None) -> bytes:
    """GET binario (para descargar paquetes .h5p/.zip) con reintentos."""
    params = params or {}
    last_exc: Exception | None = None
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            with _client() as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < config.HTTP_RETRIES:
                time.sleep(attempt * config.HTTP_RATE_DELAY * 2)
    raise last_exc if last_exc else RuntimeError(f"GET {url} failed")
