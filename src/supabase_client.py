"""
Cliente assíncrono minimalista para PostgREST (Supabase REST).

Feito pra ler IDs em lote de spotify_tracks/spotify_artists e fazer
upsert em batches nas tabelas de snapshots. Evita adicionar a dep
`supabase-py` porque só precisamos de um subset bem pequeno.

Autenticação: service_role key. NUNCA commitar. Lê de env:
  - SUPABASE_URL
  - SUPABASE_SERVICE_ROLE_KEY

Uso típico:
    async with SupabaseClient() as sb:
        tracks = await sb.select_all(
            "spotify_tracks",
            columns="spotify_id,album_id,primary_artist_spotify_id",
            page_size=1000,
        )
        await sb.upsert("spotify_track_snapshots", rows, batch=500)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class SupabaseError(Exception):
    pass


async def _retry_on_5xx(coro_fn, *, max_attempts: int = 4, op: str = "request"):
    """
    Wrapper assíncrono de retry com backoff exponencial + jitter para
    erros transientes (5xx / TransportError / TimeoutError).
    coro_fn: callable que retorna a coroutine a executar (async lambda).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except SupabaseError as e:
            msg = str(e)
            # Só retenta se for 5xx — 4xx (FK violation, 409, etc) é dado, não rede.
            is_5xx = any(code in msg for code in ("500", "502", "503", "504"))
            if not is_5xx or attempt == max_attempts - 1:
                raise
            last_exc = e
        except (httpx.TransportError, asyncio.TimeoutError) as e:
            if attempt == max_attempts - 1:
                raise
            last_exc = e
        delay = (2 ** attempt) + random.uniform(0, 0.5)
        logger.warning("%s falhou (attempt %d/%d): %s — retry em %.1fs",
                       op, attempt + 1, max_attempts, last_exc, delay)
        await asyncio.sleep(delay)
    # inalcançável em teoria
    raise last_exc or SupabaseError(f"{op} falhou sem exceção capturada")


class SupabaseClient:
    def __init__(
        self,
        url: Optional[str] = None,
        service_role_key: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.key = service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise SupabaseError(
                "SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY precisam estar no .env"
            )
        self._rest = f"{self.url}/rest/v1"
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "SupabaseClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    # ---------- SELECT ----------

    async def _select_page(self, url: str, headers: dict) -> httpx.Response:
        assert self._client is not None
        resp = await self._client.get(url, headers=headers)
        if resp.status_code not in (200, 206):
            raise SupabaseError(
                f"SELECT falhou {resp.status_code}: {resp.text[:300]}"
            )
        return resp

    async def select_all(
        self,
        table: str,
        columns: str = "*",
        where: Optional[str] = None,
        page_size: int = 1000,
    ) -> list[dict]:
        """
        SELECT paginado com header Range. Retorna todas as linhas.
        where = filtro no formato PostgREST (ex: 'album_id=not.is.null').
        Retry automático em 5xx/rede.
        """
        rows: list[dict] = []
        offset = 0
        while True:
            url = f"{self._rest}/{table}?select={columns}"
            if where:
                url += f"&{where}"
            headers = {
                "Range-Unit": "items",
                "Range": f"{offset}-{offset + page_size - 1}",
                "Prefer": "count=exact",
            }
            resp = await _retry_on_5xx(
                lambda: self._select_page(url, headers),
                op=f"SELECT {table} offset={offset}",
            )
            batch = resp.json()
            rows.extend(batch)
            cr = resp.headers.get("content-range", "")
            total = None
            if "/" in cr:
                try:
                    total = int(cr.split("/")[-1])
                except ValueError:
                    total = None
            if total is not None:
                if len(rows) >= total:
                    break
            elif len(batch) < page_size:
                break
            offset += page_size
        return rows

    # ---------- UPSERT ----------

    async def _upsert_batch(self, url: str, chunk: list[dict], headers: dict, label: str) -> None:
        assert self._client is not None
        resp = await self._client.post(url, json=chunk, headers=headers)
        if resp.status_code not in (200, 201, 204):
            raise SupabaseError(
                f"UPSERT {label} falhou {resp.status_code}: {resp.text[:500]}"
            )

    async def upsert(
        self,
        table: str,
        rows: list[dict],
        batch_size: int = 500,
        on_conflict: Optional[str] = None,
    ) -> int:
        """
        UPSERT em batches via Prefer: resolution=merge-duplicates.
        `on_conflict` opcional — nome das colunas PK/UK se não for o PK default.
        Retry automático em 5xx/rede (cada batch independente).
        Retorna total de linhas enviadas.
        """
        if not rows:
            return 0
        total = 0
        headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
        url = f"{self._rest}/{table}"
        if on_conflict:
            url += f"?on_conflict={on_conflict}"

        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            await _retry_on_5xx(
                lambda c=chunk: self._upsert_batch(url, c, headers, f"{table} batch {i}-{i+len(c)}"),
                op=f"UPSERT {table}",
            )
            total += len(chunk)
        return total

    # ---------- DELETE (pra partial overwrite tipo top_cities) ----------

    async def _delete_req(self, url: str, label: str) -> None:
        assert self._client is not None
        resp = await self._client.delete(url, headers={"Prefer": "return=minimal"})
        if resp.status_code not in (200, 204):
            raise SupabaseError(
                f"DELETE {label} falhou {resp.status_code}: {resp.text[:300]}"
            )

    async def delete_where(self, table: str, where: str) -> None:
        """DELETE com filtro PostgREST. Usado pra limpar rows antes de reinserir
        (útil em top_cities/discovered_on onde a ordem/conjunto pode mudar).
        Retry automático em 5xx/rede."""
        url = f"{self._rest}/{table}?{where}"
        await _retry_on_5xx(
            lambda: self._delete_req(url, table),
            op=f"DELETE {table}",
        )
