"""Testes da trava de instância única (src/singleton_lock.py).

Rodar (da raiz do repo):
    python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

from src.singleton_lock import acquire, release


class TestSingletonLock(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="synclock_test_")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def test_segunda_aquisicao_falha_enquanto_primeira_segura(self):
        # Simula o incidente: uma run já rodando (1ª) e o cron disparando a 2ª.
        first = acquire(self.path)
        self.assertIsNotNone(first)  # 1ª pega o lock

        second = acquire(self.path)
        self.assertIsNone(second)  # 2ª NÃO empilha — sai

        # Quando a 1ª termina e libera, uma nova run consegue de novo.
        release(first)
        third = acquire(self.path)
        self.assertIsNotNone(third)
        release(third)

    def test_release_none_e_seguro(self):
        # release(None) não pode levantar exceção (caminho do --dry-run).
        release(None)


if __name__ == "__main__":
    unittest.main()
