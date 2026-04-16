"""
Stress test — Fase 1: threshold de rate limit do GraphQL.

Escada progressiva de req/s até o Spotify começar a retornar 429.
Aborta automaticamente em 5 consecutivos 429 (fim de fase) ou 10 no total (fim do teste).

Uso:
  python -m scripts.stress_test --dry-run            # imprime plano sem hitar Spotify
  python -m scripts.stress_test                      # roda Fase 1 (default)
  python -m scripts.stress_test --max-requests 300   # cap customizado
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.auth import SpotifyAuth  # noqa: E402
from src.db import Database  # noqa: E402

console = Console()
logging.basicConfig(
    level=logging.WARNING,  # só warning+ no terminal, requests detalhados vão pro JSONL
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("stress_test")

# ============================================================
# Configuração da fase 1
# ============================================================

@dataclass
class Phase:
    name: str
    duration_s: int
    delay_s: float  # alvo entre requests
    expected_rps: float


PHASES = [
    Phase("F1 @ 0.5 req/s", duration_s=60, delay_s=2.0,  expected_rps=0.5),
    Phase("F2 @ 1.0 req/s", duration_s=60, delay_s=1.0,  expected_rps=1.0),
    Phase("F3 @ 2.0 req/s", duration_s=60, delay_s=0.5,  expected_rps=2.0),
    Phase("F4 @ 5.0 req/s", duration_s=30, delay_s=0.2,  expected_rps=5.0),
    Phase("F5 @ 10  req/s", duration_s=30, delay_s=0.1,  expected_rps=10.0),
]

COOLDOWN_BETWEEN_PHASES_S = 30

ABORT_CONSECUTIVE_429_PHASE = 5   # termina a fase atual
ABORT_CONSECUTIVE_429_TEST  = 10  # termina o teste
ABORT_CONSECUTIVE_5XX       = 3
DEFAULT_MAX_REQUESTS        = 700

# Pool de álbuns: mistura de conhecidos estáveis + os que o usuário já monitora
# (pra não martelar o mesmo álbum 660x e parecer bot). Se algum ID 404ar, é
# removido do pool na validação inicial.
HARDCODED_POOL = [
    "4LH4d3cOWNNsVw41Gqt2kv",  # Pink Floyd — Dark Side of the Moon
    "1FYY6MlQ0LmGY7aO8JEpG3",  # Samuel Messias — Ainda Tem Promessa (Ao Vivo) [já monitorado]
    "1DFixLWuPkv3KT3TnV35m3",  # Radiohead — In Rainbows
    "6X1x82kppWZmDzlXXK3y4A",  # Radiohead — OK Computer
    "5zi7WsKlIiUXv09tbGLKsE",  # Adele — 21
    "4m2880jivSbbyEGAKfITCa",  # Daft Punk — Random Access Memories
    "2Kh43m04B1UkVcpcRa1Zug",  # Kendrick Lamar — To Pimp a Butterfly
    "2noRn2Aes5aoNVsU6iWThc",  # Kendrick Lamar — good kid, m.A.A.d city
    "7fRrTyKvE4Skh93v97gtcU",  # Amy Winehouse — Back to Black
    "6mUdeDZCsExyJLMdAfDuwh",  # Lana Del Rey — Born to Die
]


# ============================================================
# Estado e logging
# ============================================================

@dataclass
class PhaseStats:
    name: str
    expected_rps: float
    delay_s: float
    total: int = 0
    ok: int = 0
    status_429: int = 0
    status_5xx: int = 0
    status_other: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    aborted: bool = False
    abort_reason: str = ""

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts if self.end_ts else 0.0

    @property
    def actual_rps(self) -> float:
        return self.total / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def effective_rps(self) -> float:
        return self.ok / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return (self.total - self.ok) / self.total if self.total else 0.0

    def latency_pct(self, p: float) -> Optional[float]:
        if not self.latencies_ms:
            return None
        s = sorted(self.latencies_ms)
        k = int(len(s) * p)
        k = min(max(k, 0), len(s) - 1)
        return s[k]


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("w", encoding="utf-8")

    def write(self, record: dict) -> None:
        self.f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


# ============================================================
# Cliente GraphQL simplificado (só getAlbum, sem retry, sem backoff)
# Queremos OBSERVAR os erros, não mascarar com retry.
# ============================================================

def query_get_album(client: httpx.Client, token: str, album_id: str) -> tuple[int, float]:
    """Retorna (status_code, latency_ms)."""
    from urllib.parse import urlencode

    sha = settings.GRAPHQL_HASHES["getAlbum"]
    variables = {"uri": f"spotify:album:{album_id}", "locale": "", "offset": 0, "limit": 50}
    url = settings.GRAPHQL_URL + "?" + urlencode({
        "operationName": "getAlbum",
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            separators=(",", ":"),
        ),
    })
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": random.choice(settings.USER_AGENTS),
        "Accept": "application/json",
        "App-Platform": "WebPlayer",
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "Spotify-App-Version": "1.2.52.442",
    }
    start = time.monotonic()
    try:
        resp = client.get(url, headers=headers, timeout=15)
        latency_ms = (time.monotonic() - start) * 1000
        return resp.status_code, latency_ms
    except httpx.TransportError:
        latency_ms = (time.monotonic() - start) * 1000
        return 0, latency_ms  # status 0 = erro de rede


# ============================================================
# Validação do pool: remove IDs inválidos antes de começar
# ============================================================

def validate_pool(client: httpx.Client, token: str, pool: list[str]) -> list[str]:
    console.print("[cyan]Validando pool de álbuns...[/]")
    valid = []
    for aid in pool:
        status, _ = query_get_album(client, token, aid)
        if status == 200:
            valid.append(aid)
            console.print(f"  [green]✓[/] {aid}")
        else:
            console.print(f"  [yellow]✗[/] {aid} (status {status}) — removido do pool")
        time.sleep(1.5)  # ritmo calmo
    return valid


# ============================================================
# Run da fase
# ============================================================

class AbortTest(Exception):
    pass


def run_phase(
    phase: Phase,
    client: httpx.Client,
    auth: SpotifyAuth,
    pool: list[str],
    jsonl: JsonlLogger,
    state: dict,
) -> PhaseStats:
    stats = PhaseStats(name=phase.name, expected_rps=phase.expected_rps, delay_s=phase.delay_s)
    stats.start_ts = time.monotonic()
    consecutive_429 = 0
    consecutive_5xx = 0

    token = auth.get_token()

    def render_status() -> Table:
        elapsed = time.monotonic() - stats.start_ts
        remaining = max(0, phase.duration_s - elapsed)
        t = Table(show_header=False, box=None)
        t.add_row("Fase:", f"[bold]{phase.name}[/]")
        t.add_row("Elapsed:", f"{elapsed:5.1f}s / {phase.duration_s}s  ({remaining:.0f}s restantes)")
        t.add_row("Requests:", f"{stats.total}  (ok={stats.ok}  429={stats.status_429}  5xx={stats.status_5xx}  other={stats.status_other})")
        t.add_row("RPS:", f"esperado {phase.expected_rps:.1f}  real {stats.actual_rps:.2f}")
        t.add_row("Consec 429:", str(consecutive_429))
        t.add_row("Total requests:", f"{state['total_requests']} / {state['max_requests']}")
        return t

    with Live(render_status(), console=console, refresh_per_second=2, transient=False) as live:
        while True:
            elapsed = time.monotonic() - stats.start_ts
            if elapsed >= phase.duration_s:
                break
            if state["total_requests"] >= state["max_requests"]:
                stats.aborted = True
                stats.abort_reason = "hard cap global de requests atingido"
                live.update(render_status())
                raise AbortTest(stats.abort_reason)

            album_id = pool[state["total_requests"] % len(pool)]
            req_start = time.monotonic()
            status, latency_ms = query_get_album(client, token, album_id)

            record = {
                "ts": datetime.now().isoformat(),
                "phase": phase.name,
                "album_id": album_id,
                "status": status,
                "latency_ms": round(latency_ms, 1),
                "delay_target_s": phase.delay_s,
                "elapsed_phase_s": round(elapsed, 2),
            }
            jsonl.write(record)

            stats.total += 1
            state["total_requests"] += 1
            stats.latencies_ms.append(latency_ms)

            if status == 200:
                stats.ok += 1
                consecutive_429 = 0
                consecutive_5xx = 0
            elif status == 429:
                stats.status_429 += 1
                state["total_429"] += 1
                consecutive_429 += 1
                consecutive_5xx = 0
                if consecutive_429 >= ABORT_CONSECUTIVE_429_PHASE:
                    stats.aborted = True
                    stats.abort_reason = f"{ABORT_CONSECUTIVE_429_PHASE} consecutive 429 — fase encerrada"
                    live.update(render_status())
                    break
                if state["total_429"] >= ABORT_CONSECUTIVE_429_TEST:
                    stats.aborted = True
                    stats.abort_reason = f"{ABORT_CONSECUTIVE_429_TEST} 429 no total — TESTE ABORTADO"
                    live.update(render_status())
                    raise AbortTest(stats.abort_reason)
            elif 500 <= status < 600:
                stats.status_5xx += 1
                consecutive_5xx += 1
                consecutive_429 = 0
                if consecutive_5xx >= ABORT_CONSECUTIVE_5XX:
                    stats.aborted = True
                    stats.abort_reason = f"{ABORT_CONSECUTIVE_5XX} 5xx consecutivos — TESTE ABORTADO"
                    live.update(render_status())
                    raise AbortTest(stats.abort_reason)
            elif status == 401:
                # token morreu durante o teste — renova e continua (não conta como erro)
                auth.invalidate()
                token = auth.get_token(force_refresh=True)
                stats.total -= 1
                state["total_requests"] -= 1
                stats.latencies_ms.pop()
                continue
            else:
                stats.status_other += 1
                consecutive_429 = 0
                consecutive_5xx = 0

            # Respeita delay alvo, descontando latência da request
            req_elapsed = time.monotonic() - req_start
            sleep_for = phase.delay_s - req_elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

            live.update(render_status())

    stats.end_ts = time.monotonic()
    return stats


# ============================================================
# Summary
# ============================================================

def compute_verdict(all_stats: list[PhaseStats]) -> dict:
    first_429_phase = None
    sustainable_rps = None
    for s in all_stats:
        if s.status_429 > 0 and first_429_phase is None:
            first_429_phase = s.name
        if s.error_rate == 0.0 and s.total >= 10:
            sustainable_rps = s.expected_rps
    return {
        "first_429_phase": first_429_phase,
        "sustainable_rps": sustainable_rps,
        "recommendation": _recommend(sustainable_rps, first_429_phase),
    }


def _recommend(sust: Optional[float], first_429: Optional[str]) -> str:
    if sust is None:
        return "nenhuma fase foi 100% sucesso — investigar conexão ou tokens"
    if first_429 is None:
        return f"não foi observado 429 até {sust} req/s — considere testar mais alto (phase 4/5 com mais duração)"
    return (
        f"1 IP sustenta ~{sust} req/s com 0 erros. "
        f"Para {100_000} tracks → {50_000} requests/dia, "
        f"precisa de ~{max(1, int(50_000 / (sust * 86_400) + 0.99))} IPs para rodar em 24h, "
        f"ou ~{max(1, int(50_000 / (sust * 14_400) + 0.99))} IPs para rodar em 4h."
    )


def print_summary(all_stats: list[PhaseStats], verdict: dict, output_dir: Path) -> None:
    t = Table(title="[bold]STRESS TEST — Fase 1: Rate Limit Threshold[/]", header_style="bold cyan")
    t.add_column("Fase")
    t.add_column("Req", justify="right")
    t.add_column("OK", justify="right")
    t.add_column("429", justify="right", style="yellow")
    t.add_column("5xx", justify="right", style="red")
    t.add_column("Other", justify="right")
    t.add_column("RPS real", justify="right")
    t.add_column("RPS efetivo", justify="right", style="green")
    t.add_column("Err%", justify="right")
    t.add_column("p50 ms", justify="right")
    t.add_column("p99 ms", justify="right")
    t.add_column("Status")

    for s in all_stats:
        p50 = s.latency_pct(0.50)
        p99 = s.latency_pct(0.99)
        status = "[red]ABORTADA[/]" if s.aborted else "[green]OK[/]"
        t.add_row(
            s.name,
            str(s.total),
            str(s.ok),
            str(s.status_429),
            str(s.status_5xx),
            str(s.status_other),
            f"{s.actual_rps:.2f}",
            f"{s.effective_rps:.2f}",
            f"{s.error_rate * 100:.1f}%",
            f"{p50:.0f}" if p50 else "-",
            f"{p99:.0f}" if p99 else "-",
            status,
        )
    console.print(t)

    console.print("\n[bold]Veredito:[/]")
    console.print(f"  Primeiro 429 em: [yellow]{verdict['first_429_phase'] or 'nenhuma fase'}[/]")
    console.print(f"  RPS sustentável (0 erros): [green]{verdict['sustainable_rps'] or 'n/d'}[/]")
    console.print(f"  → {verdict['recommendation']}")
    console.print(f"\nArtefatos salvos em: [cyan]{output_dir}[/]")


# ============================================================
# Main
# ============================================================

def build_pool() -> list[str]:
    db = Database()
    db_albums = [aid for aid, _ in db.list_monitored_albums()]
    pool = list(dict.fromkeys(db_albums + HARDCODED_POOL))  # dedup preservando ordem
    return pool


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="imprime plano sem hitar Spotify")
    p.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS, help=f"teto global (default {DEFAULT_MAX_REQUESTS})")
    args = p.parse_args()

    pool = build_pool()

    if args.dry_run:
        total_est = sum(int(ph.duration_s * ph.expected_rps) for ph in PHASES)
        console.print("[bold]DRY RUN — plano:[/]")
        console.print(f"  Álbuns no pool: {len(pool)}")
        for aid in pool:
            console.print(f"    - {aid}")
        console.print()
        for ph in PHASES:
            est = int(ph.duration_s * ph.expected_rps)
            console.print(f"  {ph.name}: {ph.duration_s}s × {ph.expected_rps} req/s = ~{est} req")
        console.print(f"\n  Cooldown entre fases: {COOLDOWN_BETWEEN_PHASES_S}s")
        console.print(f"  Total estimado: ~{total_est} requests em ~{sum(ph.duration_s for ph in PHASES) + COOLDOWN_BETWEEN_PHASES_S * (len(PHASES) - 1)}s")
        console.print(f"  Hard cap: {args.max_requests}")
        console.print(f"  Abort por fase: {ABORT_CONSECUTIVE_429_PHASE} 429 consecutivos")
        console.print(f"  Abort global: {ABORT_CONSECUTIVE_429_TEST} 429 total")
        return 0

    # Output dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path("data") / f"stress_test_{ts}"
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "phase1_rate_limit.jsonl"
    jsonl = JsonlLogger(jsonl_path)

    console.print(f"[bold cyan]Stress test — Fase 1 (rate limit threshold)[/]")
    console.print(f"Output: [yellow]{output_dir}[/]")
    console.print(f"Hard cap: [yellow]{args.max_requests}[/] requests\n")

    # Token único pra toda a fase 1
    http = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
    auth = SpotifyAuth(client=http)

    all_stats: list[PhaseStats] = []
    state = {"total_requests": 0, "total_429": 0, "max_requests": args.max_requests}
    aborted_globally = False

    # Signal handler pra Ctrl+C graceful
    interrupted = {"flag": False}
    def handler(signum, frame):
        interrupted["flag"] = True
        console.print("\n[yellow]Ctrl+C recebido — finalizando fase atual e salvando resultados...[/]")
    signal.signal(signal.SIGINT, handler)

    try:
        token = auth.get_token()
        pool = validate_pool(http, token, pool)
        if len(pool) < 3:
            console.print(f"[red]Pool muito pequeno após validação ({len(pool)} álbuns). Abortando.[/]")
            return 2
        console.print(f"[green]Pool final: {len(pool)} álbuns válidos[/]\n")

        for i, phase in enumerate(PHASES):
            if interrupted["flag"]:
                break
            console.print(f"[bold]━━━ Iniciando {phase.name} ━━━[/]")
            try:
                stats = run_phase(phase, http, auth, pool, jsonl, state)
            except AbortTest as e:
                console.print(f"[red]ABORT:[/] {e}")
                aborted_globally = True
                # stats parciais já foram coletados no loop; pegamos o último
                # (garantimos via construção: o phase modifica um objeto local mas se levantou,
                # o objeto pode não estar atribuído; vamos reconstruir vazio)
                break
            all_stats.append(stats)
            console.print(
                f"  → {stats.total} req, {stats.ok} ok, {stats.status_429} × 429, "
                f"err={stats.error_rate * 100:.1f}%, rps_efetivo={stats.effective_rps:.2f}"
            )
            if stats.aborted and "ABORTADO" not in stats.abort_reason:
                # fase terminou antes por 429 consecutivos — cooldown mais longo antes de subir
                console.print(f"  [yellow]Fase encerrada antes do tempo:[/] {stats.abort_reason}")
            if i < len(PHASES) - 1 and not interrupted["flag"] and not aborted_globally:
                console.print(f"  [dim]Cooldown {COOLDOWN_BETWEEN_PHASES_S}s...[/]\n")
                for _ in range(COOLDOWN_BETWEEN_PHASES_S):
                    if interrupted["flag"]:
                        break
                    time.sleep(1)
    finally:
        jsonl.close()
        auth.close()
        http.close()

    # Summary
    if not all_stats:
        console.print("[red]Nenhuma fase completou — sem dados para análise.[/]")
        return 2

    verdict = compute_verdict(all_stats)
    summary = {
        "timestamp": ts,
        "total_requests": state["total_requests"],
        "total_429": state["total_429"],
        "aborted_globally": aborted_globally,
        "interrupted_by_user": interrupted["flag"],
        "verdict": verdict,
        "phases": [
            {
                "name": s.name,
                "expected_rps": s.expected_rps,
                "delay_s": s.delay_s,
                "total": s.total,
                "ok": s.ok,
                "status_429": s.status_429,
                "status_5xx": s.status_5xx,
                "status_other": s.status_other,
                "actual_rps": s.actual_rps,
                "effective_rps": s.effective_rps,
                "error_rate": s.error_rate,
                "p50_ms": s.latency_pct(0.50),
                "p99_ms": s.latency_pct(0.99),
                "duration_s": s.duration_s,
                "aborted": s.aborted,
                "abort_reason": s.abort_reason,
            }
            for s in all_stats
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print_summary(all_stats, verdict, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
