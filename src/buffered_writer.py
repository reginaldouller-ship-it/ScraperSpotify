"""
Writer com buffer + flush incremental + resiliência por-linha.

WHY (incidente 2026-06-19 + contrato com o Miner):
- RAM: acumular ~3M linhas de track e gravar tudo no FIM encostava no teto de
  OOM da VPS de 1 vCPU/4GB. O flush incremental grava em lotes pequenos DURANTE
  a run → RAM cai de GBs pra MBs, e run interrompida não perde o lote inteiro.
- Resiliência (SS-5): um único valor fora-de-range (CHECK) ou FK órfã (23503)
  num lote de 500 fazia o PostgREST abortar o lote INTEIRO e derrubar a run.
  Aqui, em falha 4xx de lote, reenviamos linha-a-linha pulando só as ruins.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.supabase_client import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

MAX_BAD_ROWS_LOGGED = 50


@dataclass
class WriteResult:
    written: int = 0
    skipped: int = 0  # linhas ruins puladas (4xx persistente, linha-a-linha)
    bad_rows: list = field(default_factory=list)  # [(row, erro)], capado em MAX_BAD_ROWS_LOGGED


def _is_client_error(exc: SupabaseError) -> bool:
    """True se for erro 4xx (dado ruim: FK, CHECK, validação) — não rede/infra."""
    return exc.status_code is not None and 400 <= exc.status_code < 500


async def resilient_upsert(
    sb: SupabaseClient,
    table: str,
    rows: list[dict],
    *,
    on_conflict: str,
    batch_size: int = 500,
    dedupe: Optional[Callable[[list[dict]], list[dict]]] = None,
) -> WriteResult:
    """Upsert em lotes. Em falha 4xx de um lote (uma linha ruim derruba o lote
    inteiro — o PostgREST é atômico por request), reenvia o lote LINHA-A-LINHA,
    pulando só as que falham, em vez de abortar a run. Erros 5xx/rede já foram
    retentados dentro de sb.upsert; se chegarem aqui, propagam (infra real)."""
    result = WriteResult()
    if dedupe:
        rows = dedupe(rows)
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        try:
            result.written += await sb.upsert(
                table, chunk, batch_size=len(chunk), on_conflict=on_conflict
            )
        except SupabaseError as e:
            if not _is_client_error(e):
                raise  # 5xx persistente / infra → não engolir
            # 4xx: o lote é atômico (nada foi gravado) → isola linha-a-linha
            logger.warning(
                "UPSERT %s lote 4xx (%s) — isolando %d linhas",
                table, e.status_code, len(chunk),
            )
            for row in chunk:
                try:
                    result.written += await sb.upsert(
                        table, [row], batch_size=1, on_conflict=on_conflict
                    )
                except SupabaseError as e2:
                    if not _is_client_error(e2):
                        raise
                    result.skipped += 1
                    if len(result.bad_rows) < MAX_BAD_ROWS_LOGGED:
                        result.bad_rows.append((row, str(e2)[:200]))
    return result


class BufferedUpserter:
    """Acumula linhas e faz flush incremental quando o buffer passa de `flush_at`.
    Seguro pra vários workers async: usa take-and-release (solta o lock ANTES do
    I/O de rede), então um flush em andamento não trava os produtores."""

    def __init__(
        self,
        sb: SupabaseClient,
        table: str,
        *,
        on_conflict: str,
        flush_at: int = 5000,
        batch_size: int = 500,
        dedupe: Optional[Callable[[list[dict]], list[dict]]] = None,
    ):
        self._sb = sb
        self._table = table
        self._on_conflict = on_conflict
        self._flush_at = flush_at
        self._batch_size = batch_size
        self._dedupe = dedupe
        self._buf: list[dict] = []
        # Lock criado lazy (na 1ª chamada async): asyncio.Lock() no __init__ se
        # liga ao event loop atual, o que quebra se o objeto for construído fora
        # de um loop rodando (ex: em teste síncrono). Lazy = seguro em qualquer caso.
        self._lock: Optional[asyncio.Lock] = None
        self.written = 0
        self.skipped = 0
        self.bad_rows: list = []

    def _get_lock(self) -> asyncio.Lock:
        # check-and-set sem await no meio → atômico no asyncio single-thread
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def add(self, rows: list[dict]) -> None:
        to_flush: Optional[list[dict]] = None
        async with self._get_lock():
            self._buf.extend(rows)
            if len(self._buf) >= self._flush_at:
                to_flush = self._buf
                self._buf = []
        if to_flush:
            await self._write(to_flush)

    async def flush(self) -> None:
        async with self._get_lock():
            to_flush = self._buf
            self._buf = []
        if to_flush:
            await self._write(to_flush)

    async def _write(self, rows: list[dict]) -> None:
        res = await resilient_upsert(
            self._sb, self._table, rows,
            on_conflict=self._on_conflict, batch_size=self._batch_size, dedupe=self._dedupe,
        )
        async with self._get_lock():
            self.written += res.written
            self.skipped += res.skipped
            room = MAX_BAD_ROWS_LOGGED - len(self.bad_rows)
            if room > 0:
                self.bad_rows.extend(res.bad_rows[:room])
