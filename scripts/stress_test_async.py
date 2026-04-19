"""
Stress test ASSÍNCRONO — descobre o teto real de throughput do GraphQL Partner API
usando concorrência (asyncio.gather com N workers).

Diferenças vs stress_test.py (síncrono):
  - httpx.AsyncClient + N workers concorrentes por fase
  - Pool diverso (albums, tracks, artists) carregado de data/stress_pool.json
  - Mix de operações: getAlbum / queryArtistOverview / getTrack
  - Logging enriquecido: retry-after, headers de rate-limit, cf-ray, tamanho resposta
  - Mesma lógica de abort (5 × 429 consecutivos por fase, 10 global)

Uso:
  python -m scripts.build_stress_pool                    # 1 vez: gerar pool
  python -m scripts.stress_test_async --dry-run          # plano
  python -m scripts.stress_test_async                    # roda com defaults
  python -m scripts.stress_test_async --max-requests 5000 --workers 5,10,20

Artefatos em data/stress_test_async_<timestamp>/:
  - requests.jsonl      — linha por request (ts, op, status, latency, headers)
  - summary.json        — agregado por fase + veredito
  - stdout.log          — cópia do terminal
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.auth import SpotifyAuth  # noqa: E402

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("stress_async")


# ============================================================
# Fases default — concorrência crescente
# ============================================================

@dataclass
class Phase:
    name: str
    workers: int
    duration_s: int


DEFAULT_PHASES = [
    Phase("w5 × 30s",  workers=5,  duration_s=30),
    Phase("w10 × 30s", workers=10, duration_s=30),
    Phase("w20 × 30s", workers=20, duration_s=30),
    Phase("w40 × 30s", workers=40, duration_s=30),
    Phase("w80 × 20s", workers=80, duration_s=20),
]
COOLDOWN_BETWEEN_PHASES_S = 30

# Mix de operações (soma = 1.0)
OP_WEIGHTS = {
    "getAlbum": 0.60,
    "queryArtistOverview": 0.20,
    "getTrack": 0.20,
}
OP_POOL_KEY = {
    "getAlbum": "albums",
    "queryArtistOverview": "artists",
    "getTrack": "tracks",
}

# Aborts
ABORT_CONSECUTIVE_429_PHASE = 5
ABORT_CONSECUTIVE_429_TEST  = 10
ABORT_CONSECUTIVE_5XX       = 5
DEFAULT_MAX_REQUESTS        = 15_000


# ============================================================
# Estado global compartilhado entre workers (asyncio single-thread, sem lock)
# ============================================================

@dataclass
class PhaseStats:
    name: str
    workers: int
    duration_s: int
    total: int = 0
    ok: int = 0
    status_429: int = 0
    status_401: int = 0
    status_403: int = 0
    status_5xx: int = 0
    status_network_err: int = 0
    status_other: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    first_429_ts: Optional[str] = None
    first_429_elapsed_s: Optional[float] = None
    start_ts: float = 0.0
    end_ts: float = 0.0
    aborted: bool = False
    abort_reason: str = ""
    rate_limit_headers_seen: list[dict] = field(default_factory=list)

    @property
    def duration_real_s(self) -> float:
        return self.end_ts - self.start_ts if self.end_ts else 0.0

    @property
    def actual_rps(self) -> float:
        d = self.duration_real_s
        return self.total / d if d > 0 else 0.0

    @property
    def effective_rps(self) -> float:
        d = self.duration_real_s
        return self.ok / d if d > 0 else 0.0

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


@dataclass
class GlobalState:
    total_requests: int = 0
    total_429: int = 0
    total_403: int = 0
    total_5xx: int = 0
    total_network_err: int = 0
    consecutive_429: int = 0
    consecutive_5xx: int = 0
    max_requests: int = DEFAULT_MAX_REQUESTS
    abort_reason: Optional[str] = None


# ============================================================
# Token holder — refresh em background pra soaks > 1h
# ============================================================

class TokenHolder:
    """Mantém o access token atualizado entre workers.
    Atributo `.token` é lido pelos workers sem lock (asyncio single-thread).
    Refresh roda numa task separada que chama auth sync via asyncio.to_thread.
    """

    def __init__(self, auth: SpotifyAuth):
        self._auth = auth
        self.token: str = auth.get_token()
        self._lock = asyncio.Lock()
        self.refresh_count = 0
        self.last_refresh_ts: Optional[str] = datetime.now().isoformat(timespec="seconds")

    async def refresh(self, reason: str = "periodic") -> None:
        async with self._lock:
            try:
                new_token = await asyncio.to_thread(self._auth.get_token, True)
                if new_token != self.token:
                    self.token = new_token
                    self.refresh_count += 1
                    self.last_refresh_ts = datetime.now().isoformat(timespec="seconds")
                    console.print(f"[cyan]Token refreshed ({reason}, count={self.refresh_count})[/]")
            except Exception as e:
                logger.error("Token refresh falhou (%s): %s", reason, e)


async def token_refresh_task(
    holder: TokenHolder,
    interval_s: int,
    stop_event: asyncio.Event,
) -> None:
    """Refresh proativo a cada interval_s segundos."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return  # stop_event foi setado
        except asyncio.TimeoutError:
            await holder.refresh("periodic")


# ============================================================
# HTTP call
# ============================================================

def build_url(operation_name: str, variables: dict) -> str:
    sha = settings.GRAPHQL_HASHES[operation_name]
    return settings.GRAPHQL_URL + "?" + urlencode({
        "operationName": operation_name,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            separators=(",", ":"),
        ),
    })


def build_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": random.choice(settings.USER_AGENTS),
        "Accept": "application/json",
        "App-Platform": "WebPlayer",
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "Spotify-App-Version": "1.2.52.442",
    }


def build_variables(op: str, item_id: str) -> dict:
    if op == "getAlbum":
        return {"uri": f"spotify:album:{item_id}", "locale": "", "offset": 0, "limit": 50}
    if op == "queryArtistOverview":
        return {"uri": f"spotify:artist:{item_id}", "locale": "", "includePrerelease": True}
    if op == "getTrack":
        return {"uri": f"spotify:track:{item_id}"}
    raise ValueError(f"op desconhecida: {op}")


# Headers interessantes pra extrair (rate limiting + debug)
RATE_HEADER_PREFIXES = ("x-ratelimit", "x-rate-limit", "ratelimit", "retry-after", "cf-")


def extract_signals(resp: httpx.Response) -> dict:
    sig = {}
    for k, v in resp.headers.items():
        kl = k.lower()
        if any(kl.startswith(p) for p in RATE_HEADER_PREFIXES):
            sig[kl] = v
    return sig


async def do_request(
    client: httpx.AsyncClient,
    token: str,
    op: str,
    item_id: str,
) -> dict:
    url = build_url(op, build_variables(op, item_id))
    headers = build_headers(token)
    start = time.monotonic()
    try:
        resp = await client.get(url, headers=headers, timeout=15.0)
        latency_ms = (time.monotonic() - start) * 1000
        content = resp.content  # materializa body
        signals = extract_signals(resp)
        return {
            "status": resp.status_code,
            "latency_ms": round(latency_ms, 1),
            "resp_bytes": len(content),
            "signals": signals,
            "error": None,
        }
    except (httpx.TransportError, asyncio.TimeoutError) as e:
        latency_ms = (time.monotonic() - start) * 1000
        return {
            "status": 0,
            "latency_ms": round(latency_ms, 1),
            "resp_bytes": 0,
            "signals": {},
            "error": f"{type(e).__name__}: {str(e)[:150]}",
        }


# ============================================================
# Escolha ponderada de operação
# ============================================================

def choose_op() -> str:
    r = random.random()
    acc = 0.0
    for op, w in OP_WEIGHTS.items():
        acc += w
        if r < acc:
            return op
    return next(iter(OP_WEIGHTS))


# ============================================================
# Worker loop
# ============================================================

async def worker(
    worker_id: int,
    phase: Phase,
    client: httpx.AsyncClient,
    token_holder: "TokenHolder",
    pool: dict,
    stats: PhaseStats,
    gstate: GlobalState,
    deadline: float,
    jsonl_fh,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        if time.monotonic() >= deadline:
            return
        if gstate.total_requests >= gstate.max_requests:
            gstate.abort_reason = "max_requests global cap atingido"
            stop_event.set()
            return

        op = choose_op()
        pool_key = OP_POOL_KEY[op]
        items = pool.get(pool_key) or []
        if not items:
            logger.warning("pool '%s' vazio — pulando", pool_key)
            await asyncio.sleep(0.01)
            continue
        item_id = random.choice(items)

        result = await do_request(client, token_holder.token, op, item_id)

        # log jsonl (linha única — write+flush não bloqueia em asyncio pra arquivo local pequeno)
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "phase": phase.name,
            "worker": worker_id,
            "op": op,
            "item_id": item_id,
            "status": result["status"],
            "latency_ms": result["latency_ms"],
            "resp_bytes": result["resp_bytes"],
            "signals": result["signals"],
            "error": result["error"],
        }
        jsonl_fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        # update counters
        stats.total += 1
        stats.latencies_ms.append(result["latency_ms"])
        gstate.total_requests += 1

        s = result["status"]
        if s == 200:
            stats.ok += 1
            gstate.consecutive_429 = 0
            gstate.consecutive_5xx = 0
        elif s == 429:
            stats.status_429 += 1
            gstate.total_429 += 1
            gstate.consecutive_429 += 1
            gstate.consecutive_5xx = 0
            if stats.first_429_ts is None:
                stats.first_429_ts = record["ts"]
                stats.first_429_elapsed_s = round(time.monotonic() - stats.start_ts, 2)
            if result["signals"]:
                stats.rate_limit_headers_seen.append(result["signals"])
            if gstate.consecutive_429 >= ABORT_CONSECUTIVE_429_PHASE:
                gstate.abort_reason = f"{ABORT_CONSECUTIVE_429_PHASE} × 429 consecutivos (fase)"
                stats.aborted = True
                stats.abort_reason = gstate.abort_reason
                stop_event.set()
                return
            if gstate.total_429 >= ABORT_CONSECUTIVE_429_TEST:
                gstate.abort_reason = f"{ABORT_CONSECUTIVE_429_TEST} × 429 global — teste encerrado"
                stats.aborted = True
                stats.abort_reason = gstate.abort_reason
                stop_event.set()
                return
        elif s == 401:
            stats.status_401 += 1
            # token expirou ou foi invalidado — força refresh (lock interno evita corrida)
            await token_holder.refresh("401_received")
        elif s == 403:
            stats.status_403 += 1
            gstate.total_403 += 1
            gstate.abort_reason = "403 Forbidden — possível bloqueio de IP"
            stats.aborted = True
            stats.abort_reason = gstate.abort_reason
            stop_event.set()
            return
        elif 500 <= s < 600:
            stats.status_5xx += 1
            gstate.total_5xx += 1
            gstate.consecutive_5xx += 1
            gstate.consecutive_429 = 0
            if gstate.consecutive_5xx >= ABORT_CONSECUTIVE_5XX:
                gstate.abort_reason = f"{ABORT_CONSECUTIVE_5XX} × 5xx consecutivos"
                stats.aborted = True
                stats.abort_reason = gstate.abort_reason
                stop_event.set()
                return
        elif s == 0:
            stats.status_network_err += 1
            gstate.total_network_err += 1
        else:
            stats.status_other += 1


# ============================================================
# Fase
# ============================================================

async def run_phase(
    phase: Phase,
    client: httpx.AsyncClient,
    token_holder: "TokenHolder",
    pool: dict,
    gstate: GlobalState,
    jsonl_fh,
) -> PhaseStats:
    stats = PhaseStats(name=phase.name, workers=phase.workers, duration_s=phase.duration_s)
    stats.start_ts = time.monotonic()
    deadline = stats.start_ts + phase.duration_s
    stop_event = asyncio.Event()

    console.print(f"[bold]━━━ {phase.name}  ({phase.workers} workers, {phase.duration_s}s) ━━━[/]")

    tasks = [
        asyncio.create_task(worker(
            i, phase, client, token_holder, pool,
            stats, gstate, deadline, jsonl_fh, stop_event,
        ))
        for i in range(phase.workers)
    ]

    # Progress tick a cada 1s
    async def tick():
        last_total = 0
        while not stop_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(1)
            delta = stats.total - last_total
            last_total = stats.total
            elapsed = time.monotonic() - stats.start_ts
            console.print(
                f"  [dim]t={elapsed:4.0f}s | req={stats.total:>5} (+{delta:>3}/s) | "
                f"ok={stats.ok} | 429={stats.status_429} | 5xx={stats.status_5xx} | "
                f"neterr={stats.status_network_err} | "
                f"global_429={gstate.total_429}[/]"
            )

    tick_task = asyncio.create_task(tick())
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop_event.set()
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass

    stats.end_ts = time.monotonic()

    console.print(
        f"  → total={stats.total}  ok={stats.ok}  429={stats.status_429}  "
        f"5xx={stats.status_5xx}  neterr={stats.status_network_err}  "
        f"rps_real={stats.actual_rps:.1f}  err%={stats.error_rate * 100:.1f}"
    )
    if stats.aborted:
        console.print(f"  [red]ABORT:[/] {stats.abort_reason}")
    return stats


# ============================================================
# Summary
# ============================================================

def print_summary(all_stats: list[PhaseStats], gstate: GlobalState) -> None:
    t = Table(title="[bold]STRESS TEST ASYNC — throughput concorrente[/]", header_style="bold cyan")
    t.add_column("Fase")
    t.add_column("W", justify="right")
    t.add_column("Req", justify="right")
    t.add_column("OK", justify="right")
    t.add_column("429", justify="right", style="yellow")
    t.add_column("5xx", justify="right", style="red")
    t.add_column("401", justify="right")
    t.add_column("403", justify="right", style="red")
    t.add_column("NetErr", justify="right")
    t.add_column("RPS real", justify="right", style="green")
    t.add_column("Err%", justify="right")
    t.add_column("p50 ms", justify="right")
    t.add_column("p95 ms", justify="right")
    t.add_column("p99 ms", justify="right")
    t.add_column("Status")

    for s in all_stats:
        p50 = s.latency_pct(0.50)
        p95 = s.latency_pct(0.95)
        p99 = s.latency_pct(0.99)
        status = "[red]ABORT[/]" if s.aborted else "[green]OK[/]"
        t.add_row(
            s.name,
            str(s.workers),
            str(s.total),
            str(s.ok),
            str(s.status_429),
            str(s.status_5xx),
            str(s.status_401),
            str(s.status_403),
            str(s.status_network_err),
            f"{s.actual_rps:.1f}",
            f"{s.error_rate * 100:.1f}%",
            f"{p50:.0f}" if p50 else "-",
            f"{p95:.0f}" if p95 else "-",
            f"{p99:.0f}" if p99 else "-",
            status,
        )
    console.print(t)

    # Veredito
    sustainable = None
    for s in all_stats:
        if s.error_rate == 0 and s.total >= 30:
            sustainable = s
    first_429 = next((s for s in all_stats if s.status_429 > 0), None)

    console.print("\n[bold]Veredito:[/]")
    if sustainable:
        console.print(
            f"  Maior fase 100% OK: [green]{sustainable.name}[/] → "
            f"{sustainable.actual_rps:.1f} req/s com {sustainable.workers} workers"
        )
    else:
        console.print("  [yellow]Nenhuma fase 100% OK — gargalo muito antes do esperado[/]")

    if first_429:
        console.print(
            f"  Primeiro 429 em: [yellow]{first_429.name}[/] "
            f"(elapsed {first_429.first_429_elapsed_s}s, ts {first_429.first_429_ts})"
        )
        if first_429.rate_limit_headers_seen:
            console.print(f"  Headers de rate-limit capturados (primeira ocorrência):")
            console.print(f"  [dim]{json.dumps(first_429.rate_limit_headers_seen[0], indent=2)}[/]")
    else:
        console.print("  [green]Nenhum 429 observado em todo o teste[/]")

    if gstate.abort_reason:
        console.print(f"  Motivo do abort global: [red]{gstate.abort_reason}[/]")


# ============================================================
# Main
# ============================================================

def parse_workers(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def load_pool(path: Path) -> dict:
    if not path.exists():
        console.print(
            f"[red]Pool não existe em {path}.[/] Rode antes:\n"
            f"  python -m scripts.build_stress_pool"
        )
        sys.exit(2)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("albums") or not data.get("tracks") or not data.get("artists"):
        console.print(f"[red]Pool incompleto em {path}[/]")
        sys.exit(2)
    return data


async def main_async(args) -> int:
    pool_path = Path(args.pool)
    pool = load_pool(pool_path)
    console.print(
        f"[bold cyan]Pool carregado:[/] "
        f"{len(pool['albums'])} álbuns, {len(pool['tracks'])} tracks, {len(pool['artists'])} artistas"
    )

    # Fases customizadas via --workers + --duration
    if args.workers:
        worker_list = parse_workers(args.workers)
        phases = [
            Phase(name=f"w{w} × {args.duration}s", workers=w, duration_s=args.duration)
            for w in worker_list
        ]
    else:
        phases = list(DEFAULT_PHASES)

    if args.dry_run:
        console.print("[bold]DRY RUN — plano:[/]")
        for ph in phases:
            console.print(f"  {ph.name}: {ph.workers} workers × {ph.duration_s}s")
        console.print(f"  Cooldown entre fases: {COOLDOWN_BETWEEN_PHASES_S}s")
        console.print(f"  Hard cap: {args.max_requests} requests")
        console.print(f"  Mix ops: {OP_WEIGHTS}")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("data") / f"stress_test_async_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "requests.jsonl"
    summary_path = out_dir / "summary.json"

    console.print(f"[bold cyan]Stress test async[/] — output: [yellow]{out_dir}[/]")
    console.print(f"Hard cap: [yellow]{args.max_requests}[/] requests\n")

    # Token holder com refresh periódico em background (importante pra soaks > 50min)
    http_sync = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
    auth = SpotifyAuth(client=http_sync)
    token_holder = TokenHolder(auth)
    console.print(f"[green]Token inicial obtido ({len(token_holder.token)} chars)[/]")
    console.print(f"[dim]Refresh proativo a cada {args.token_refresh_interval // 60}min[/]\n")

    gstate = GlobalState(max_requests=args.max_requests)
    all_stats: list[PhaseStats] = []
    interrupted = False

    # Ctrl+C handler
    def handler(sig, frame):
        nonlocal interrupted
        interrupted = True
        console.print("\n[yellow]Ctrl+C — encerrando fase atual...[/]")
    signal.signal(signal.SIGINT, handler)

    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    jsonl_fh = jsonl_path.open("w", encoding="utf-8")
    refresh_stop = asyncio.Event()
    refresh_task = asyncio.create_task(
        token_refresh_task(token_holder, args.token_refresh_interval, refresh_stop)
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=limits,
            follow_redirects=True,
            http2=False,
        ) as client:
            for i, phase in enumerate(phases):
                if interrupted or gstate.abort_reason:
                    break
                stats = await run_phase(phase, client, token_holder, pool, gstate, jsonl_fh)
                all_stats.append(stats)
                if gstate.abort_reason:
                    break
                if i < len(phases) - 1:
                    console.print(f"[dim]Cooldown {COOLDOWN_BETWEEN_PHASES_S}s...[/]\n")
                    for _ in range(COOLDOWN_BETWEEN_PHASES_S):
                        if interrupted:
                            break
                        await asyncio.sleep(1)
    finally:
        refresh_stop.set()
        await asyncio.gather(refresh_task, return_exceptions=True)
        jsonl_fh.close()

    auth.close()
    http_sync.close()

    # Summary
    print_summary(all_stats, gstate)

    summary = {
        "timestamp": ts,
        "pool_path": str(pool_path),
        "pool_sizes": {
            "albums": len(pool["albums"]),
            "tracks": len(pool["tracks"]),
            "artists": len(pool["artists"]),
        },
        "op_weights": OP_WEIGHTS,
        "max_requests": args.max_requests,
        "global_state": {
            "total_requests": gstate.total_requests,
            "total_429": gstate.total_429,
            "total_403": gstate.total_403,
            "total_5xx": gstate.total_5xx,
            "total_network_err": gstate.total_network_err,
            "token_refreshes": token_holder.refresh_count,
            "last_token_refresh_ts": token_holder.last_refresh_ts,
            "abort_reason": gstate.abort_reason,
        },
        "interrupted_by_user": interrupted,
        "phases": [
            {
                "name": s.name,
                "workers": s.workers,
                "target_duration_s": s.duration_s,
                "real_duration_s": s.duration_real_s,
                "total": s.total,
                "ok": s.ok,
                "status_429": s.status_429,
                "status_401": s.status_401,
                "status_403": s.status_403,
                "status_5xx": s.status_5xx,
                "status_network_err": s.status_network_err,
                "status_other": s.status_other,
                "actual_rps": s.actual_rps,
                "effective_rps": s.effective_rps,
                "error_rate": s.error_rate,
                "p50_ms": s.latency_pct(0.50),
                "p95_ms": s.latency_pct(0.95),
                "p99_ms": s.latency_pct(0.99),
                "first_429_ts": s.first_429_ts,
                "first_429_elapsed_s": s.first_429_elapsed_s,
                "rate_limit_headers_seen": s.rate_limit_headers_seen[:10],
                "aborted": s.aborted,
                "abort_reason": s.abort_reason,
            }
            for s in all_stats
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\nArtefatos: [cyan]{out_dir}[/]")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pool", default="data/stress_pool.json", help="caminho do pool (build_stress_pool.py)")
    p.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    p.add_argument("--workers", type=str, default="", help="ex: 5,10,20 — override das fases default")
    p.add_argument("--duration", type=int, default=30, help="duração de cada fase custom (default 30s)")
    p.add_argument(
        "--token-refresh-interval",
        type=int,
        default=45 * 60,
        help="refresh proativo do token a cada N segundos (default 2700 = 45min; token Spotify dura ~60min)",
    )
    args = p.parse_args()

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
