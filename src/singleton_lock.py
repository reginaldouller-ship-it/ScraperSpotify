"""Trava de instância única via fcntl.flock (Unix).

WHY: o incidente 2026-06-19 foi causado por runs do sync EMPILHANDO. Quando uma
run passou a demorar mais que o intervalo do cron (catálogo cresceu ~12×), a
run do dia seguinte começava POR CIMA da anterior, e os processos Python órfãos
saturavam a VPS de 1 vCPU (CPU 100%, RAM no teto de OOM).

Esta trava faz a 2ª instância SAIR na hora, em vez de empilhar.

Detalhe importante: o lock do `flock` é amarrado ao processo (à open file
description). Se o dono morre — inclusive por OOM-kill ou SIGKILL — o sistema
operacional libera o lock automaticamente. Ou seja: nunca fica "preso" travado.
"""
from __future__ import annotations

import fcntl
from typing import IO, Optional


def acquire(path: str) -> Optional[IO]:
    """Tenta pegar um lock exclusivo NÃO-bloqueante em `path`.

    Retorna o objeto de arquivo (segure a referência enquanto quiser manter o
    lock) ou `None` se outro processo já o detém. Para liberar, use release().
    """
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Outro processo já segura o lock — não empilha.
        fh.close()
        return None
    return fh


def release(fh: Optional[IO]) -> None:
    """Libera o lock e fecha o arquivo. Idempotente (aceita None)."""
    if fh is None:
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
