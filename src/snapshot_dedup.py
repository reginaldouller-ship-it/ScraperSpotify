"""
Dedup de snapshots de track antes do upsert.

WHY (contrato com o Miner, rule [4]): o guard que propaga o playcount pra
spotify_tracks.latest_playcount é por DATA (latest_playcount_date <= date), NÃO
por valor. Se duas linhas da mesma (track, date) chegarem com playcounts
diferentes — track repetida em 2 álbuns/compilações, ou uma re-run no mesmo dia —
gravar a MENOR rebaixaria o latest_playcount (e isso vaza pra busca/UI).

playcount é monotônico (só cresce ao longo do tempo), então manter o MAIOR por
(track, date) é sempre o valor mais recente/correto.
"""
from __future__ import annotations


def _pc_key(pc) -> int:
    """Chave de ordenação de playcount. None vira -1 pra sempre PERDER de
    qualquer inteiro (o CHECK garante playcount >= 0)."""
    return pc if isinstance(pc, int) else -1


def dedupe_track_snapshots(rows: list[dict]) -> list[dict]:
    """Colapsa linhas duplicadas de (spotify_track_id, date) mantendo a de MAIOR
    playcount. Espera que cada row tenha as chaves spotify_track_id, date e
    playcount (o worker já pula os playcount None antes de chegar aqui; mesmo
    assim tratamos None defensivamente como o menor valor possível)."""
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["spotify_track_id"], r["date"])
        cur = best.get(key)
        if cur is None or _pc_key(r.get("playcount")) > _pc_key(cur.get("playcount")):
            best[key] = r
    return list(best.values())
