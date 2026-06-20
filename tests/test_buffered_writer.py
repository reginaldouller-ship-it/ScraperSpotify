"""
Testes do writer com buffer + resiliência por-linha.

Intuito: provar (a) que o flush incremental dispara ao passar do limite e grava
tudo, (b) que uma linha ruim (4xx) num lote NÃO derruba o lote inteiro — só ela é
pulada, (c) que 5xx (infra) propaga (não engole), e (d) que o dedup roda por flush.
"""
import asyncio
import unittest

from src.supabase_client import SupabaseError
from src.buffered_writer import resilient_upsert, BufferedUpserter
from src.snapshot_dedup import dedupe_track_snapshots


class FakeSB:
    """sb falso: um lote contendo um id 'ruim' falha INTEIRO com 4xx (igual ao
    PostgREST, que é atômico por request). Linhas boas isoladas passam."""

    def __init__(self, bad_ids=()):
        self.bad_ids = set(bad_ids)
        self.written_rows = []
        self.calls = 0

    async def upsert(self, table, rows, batch_size=500, on_conflict=None):
        self.calls += 1
        if any(r.get("spotify_track_id") in self.bad_ids for r in rows):
            raise SupabaseError("UPSERT falhou 409: fk", status_code=409)
        self.written_rows.extend(rows)
        return len(rows)


def _row(tid, pc=10, date="2026-06-20"):
    return {"spotify_track_id": tid, "date": date, "playcount": pc}


def run(coro):
    return asyncio.run(coro)


class TestResilientUpsert(unittest.TestCase):
    def test_tudo_bom_grava_tudo(self):
        sb = FakeSB()
        res = run(resilient_upsert(sb, "t", [_row("a"), _row("b")], on_conflict="spotify_track_id,date"))
        self.assertEqual(res.written, 2)
        self.assertEqual(res.skipped, 0)
        self.assertEqual(len(sb.written_rows), 2)

    def test_linha_ruim_nao_derruba_o_lote(self):
        # 'x' é ruim → o lote de 3 falha 4xx → isola linha-a-linha → grava a,b, pula x
        sb = FakeSB(bad_ids={"x"})
        res = run(resilient_upsert(sb, "t", [_row("a"), _row("x"), _row("b")], on_conflict="spotify_track_id,date"))
        self.assertEqual(res.written, 2)
        self.assertEqual(res.skipped, 1)
        self.assertEqual(res.bad_rows[0][0]["spotify_track_id"], "x")
        ids = {r["spotify_track_id"] for r in sb.written_rows}
        self.assertEqual(ids, {"a", "b"})

    def test_5xx_persistente_propaga(self):
        # erro sem status 4xx (ex: 503) NÃO deve ser engolido — é infra
        class SB5xx:
            async def upsert(self, *a, **k):
                raise SupabaseError("503", status_code=503)
        with self.assertRaises(SupabaseError):
            run(resilient_upsert(SB5xx(), "t", [_row("a")], on_conflict="x"))

    def test_dedupe_roda_antes_de_gravar(self):
        sb = FakeSB()
        rows = [_row("a", 90), _row("a", 100)]  # dup: fica o MAIOR
        res = run(resilient_upsert(sb, "t", rows, on_conflict="spotify_track_id,date", dedupe=dedupe_track_snapshots))
        self.assertEqual(res.written, 1)
        self.assertEqual(sb.written_rows[0]["playcount"], 100)


class TestBufferedUpserter(unittest.TestCase):
    def test_flush_incremental_dispara_no_limite(self):
        sb = FakeSB()
        w = BufferedUpserter(sb, "t", on_conflict="spotify_track_id,date", flush_at=3)

        async def scenario():
            await w.add([_row("a"), _row("b")])   # 2 < 3 → não flusha
            self.assertEqual(w.written, 0)
            await w.add([_row("c")])              # 3 >= 3 → flusha o lote
            self.assertEqual(w.written, 3)
            await w.add([_row("d")])              # 1 fica no buffer
            await w.flush()                       # flush final do resto
            self.assertEqual(w.written, 4)

        run(scenario())
        self.assertEqual(len(sb.written_rows), 4)

    def test_buffer_vazio_flush_e_noop(self):
        sb = FakeSB()
        w = BufferedUpserter(sb, "t", on_conflict="x", flush_at=10)
        run(w.flush())
        self.assertEqual(w.written, 0)
        self.assertEqual(sb.calls, 0)

    def test_resiliencia_propaga_pro_buffer(self):
        sb = FakeSB(bad_ids={"x"})
        w = BufferedUpserter(sb, "t", on_conflict="spotify_track_id,date", flush_at=100)

        async def scenario():
            await w.add([_row("a"), _row("x"), _row("b")])
            await w.flush()

        run(scenario())
        self.assertEqual(w.written, 2)
        self.assertEqual(w.skipped, 1)


if __name__ == "__main__":
    unittest.main()
