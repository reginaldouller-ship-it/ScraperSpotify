"""
Testes do dedup de track snapshots.

Intuito: garantir que, quando a mesma (track, date) aparece mais de uma vez numa
run, fica gravado o MAIOR playcount — nunca o menor (que rebaixaria o
latest_playcount do Miner, ver src/snapshot_dedup.py).
"""
import unittest

from src.snapshot_dedup import dedupe_track_snapshots


def _row(track: str, date: str, pc):
    return {"spotify_track_id": track, "date": date, "playcount": pc}


class TestDedupeTrackSnapshots(unittest.TestCase):
    # --- casos de borda primeiro ---

    def test_lista_vazia_retorna_vazia(self):
        self.assertEqual(dedupe_track_snapshots([]), [])

    def test_none_perde_para_inteiro(self):
        # None defensivo: se por algum motivo um None escapar, perde do valor real
        rows = [_row("a", "2026-06-20", None), _row("a", "2026-06-20", 5)]
        out = dedupe_track_snapshots(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["playcount"], 5)

    # --- caso feliz ---

    def test_sem_duplicata_preserva_todas(self):
        rows = [_row("a", "2026-06-20", 10), _row("b", "2026-06-20", 20)]
        out = dedupe_track_snapshots(rows)
        self.assertEqual(len(out), 2)

    # --- variações: mantém o maior independente da ordem ---

    def test_duplicata_mantem_o_maior(self):
        rows = [_row("a", "2026-06-20", 100), _row("a", "2026-06-20", 90)]
        out = dedupe_track_snapshots(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["playcount"], 100)

    def test_duplicata_ordem_inversa_mantem_o_maior(self):
        # o maior chegando por último também deve vencer
        rows = [_row("a", "2026-06-20", 90), _row("a", "2026-06-20", 100)]
        out = dedupe_track_snapshots(rows)
        self.assertEqual(out[0]["playcount"], 100)

    def test_mesma_track_datas_diferentes_nao_colapsa(self):
        # a date faz parte da chave — dias diferentes são linhas diferentes
        rows = [_row("a", "2026-06-19", 90), _row("a", "2026-06-20", 100)]
        out = dedupe_track_snapshots(rows)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
