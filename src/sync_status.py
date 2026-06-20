"""Lógica pura de decisão do status / exit-code de uma run do sync.

Separado de scripts/sync_from_supabase.py de propósito: é lógica PURA
(entrada → saída, sem efeitos colaterais, sem rede/IO). Assim dá pra testar
isoladamente em tests/test_sync_status.py sem precisar de rich, httpx ou
credenciais do Supabase.

WHY existe: antes o sync saía com exit code 1 se QUALQUER álbum/artista
falhasse. Com ~450 mil itens por run, um único blip de rede já marcava a run
inteira como "failed" no Coolify — mesmo tendo gravado 99,9% dos dados. Esse
era metade do mistério "falha todo dia mas o dado chega". Aqui distinguimos
ruído transitório (aceitável) de falha real (que merece alerta).
"""
from __future__ import annotations


def _failure_rate(ok: int, failed: int) -> float:
    """Fração de falhas numa categoria. 0.0 quando não houve trabalho."""
    total = ok + failed
    if total <= 0:
        return 0.0
    return failed / total


def decide_run_status(
    albums_ok: int,
    albums_failed: int,
    artists_ok: int,
    artists_failed: int,
    discovered_on_ok: int = 0,
    discovered_on_failed: int = 0,
    threshold: float = 0.01,
) -> tuple[str, int]:
    """Decide o rótulo de status e o exit code da run.

    Retorna (status, exit_code):
      - "completed" / 0 : zero falhas.
      - "partial"   / 0 : houve falhas, mas TODAS as taxas <= threshold
                          (ruído transitório aceitável). Coolify vê sucesso.
      - "degraded"  / 1 : alguma taxa de falha passou do threshold (algo
                          realmente quebrado: hash, rede, rate limit).
                          Coolify alerta de verdade.

    threshold = fração máxima tolerável de falhas POR categoria (default 1%).
    Usamos `>` (não `>=`): exatamente no threshold ainda conta como aceitável.
    """
    rates = [
        _failure_rate(albums_ok, albums_failed),
        _failure_rate(artists_ok, artists_failed),
        _failure_rate(discovered_on_ok, discovered_on_failed),
    ]
    any_failure = (albums_failed + artists_failed + discovered_on_failed) > 0
    if any(r > threshold for r in rates):
        return ("degraded", 1)
    if any_failure:
        return ("partial", 0)
    return ("completed", 0)
