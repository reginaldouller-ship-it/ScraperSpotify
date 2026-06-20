"""Testes da lógica pura de status/exit-code do sync.

Rodar (da raiz do repo):
    python -m unittest discover -s tests -v

Comentários em pt-BR explicando o intuito de cada caso.
"""
import unittest

from src.sync_status import decide_run_status


class TestDecideRunStatus(unittest.TestCase):
    # --- casos de borda primeiro ---

    def test_tudo_zero_nao_e_erro(self):
        # Nada pra processar (0 álbuns, 0 artistas) NÃO deve marcar erro.
        self.assertEqual(decide_run_status(0, 0, 0, 0), ("completed", 0))

    def test_sem_falhas(self):
        # Run perfeita → completed, exit 0.
        self.assertEqual(decide_run_status(100, 0, 50, 0), ("completed", 0))

    # --- o coração do fix: ruído transitório não pode marcar "failed" ---

    def test_falhas_abaixo_do_threshold_e_partial_exit_0(self):
        # 5 falhas em 1000 álbuns = 0,5% < 1% → partial, mas exit 0.
        self.assertEqual(decide_run_status(995, 5, 50, 0), ("partial", 0))

    def test_exatamente_no_threshold_ainda_e_partial(self):
        # 10 falhas em 1000 = 1,0% == threshold. Usamos `>` então NÃO é degraded.
        self.assertEqual(decide_run_status(990, 10, 50, 0), ("partial", 0))

    # --- falha real (acima do threshold) tem que alertar (exit 1) ---

    def test_falhas_acima_do_threshold_e_degraded_exit_1(self):
        # 20 falhas em 1000 = 2% > 1% → degraded, exit 1.
        self.assertEqual(decide_run_status(980, 20, 50, 0), ("degraded", 1))

    def test_degraded_por_artistas(self):
        # Álbuns perfeitos, mas metade dos artistas falhou → degraded.
        self.assertEqual(decide_run_status(1000, 0, 50, 50), ("degraded", 1))

    def test_degraded_por_discovered_on(self):
        # discovered_on quebrado em massa (ex: hash rotacionou) → degraded.
        self.assertEqual(decide_run_status(1000, 0, 100, 0, 0, 100), ("degraded", 1))

    def test_discovered_on_ruido_e_partial(self):
        # Poucas falhas de discovered_on, abaixo do limite → partial/exit 0.
        self.assertEqual(decide_run_status(1000, 0, 1000, 0, 995, 5), ("partial", 0))


if __name__ == "__main__":
    unittest.main()
