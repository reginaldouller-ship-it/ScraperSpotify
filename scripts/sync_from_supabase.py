"""
Lê IDs (tracks, artists) do Supabase, bate na Partner GraphQL com 20 workers
async, e upserta nas tabelas de snapshots:

  - spotify_track_snapshots             (playcount diário por track)
  - spotify_artist_snapshots            (+ monthly_listeners, world_rank)
  - spotify_artist_top_cities_snapshots (top 5 cidades por dia)
  - spotify_artist_discovered_on_snapshots (playlists impulsionando streams)

Fluxo:
  1. SELECT spotify_tracks (spotify_id, album_id) WHERE album_id IS NOT NULL
     → deduz albums_to_fetch (set de album_ids distintos)
  2. SELECT spotify_artists (spotify_id) → artists_to_fetch
  3. Fases paralelas, ambas com 20 workers:
       a) Albums: getAlbum (pega playcount de todas tracks do album)
       b) Artists: queryArtistOverview + queryArtistDiscoveredOn
  4. Upsert batched (500 linhas/request) em cada tabela

CLI:
  python -m scripts.sync_from_supabase --dry-run
  python -m scripts.sync_from_supabase --limit 10
  python -m scripts.sync_from_supabase                   # run completa
  python -m scripts.sync_from_supabase --workers 10      # override default 20
  python -m scripts.sync_from_supabase --snapshot-date 2026-04-19

Env necessárias:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.auth import SpotifyAuth  # noqa: E402
from src.supabase_client import SupabaseClient  # noqa: E402

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("sync_supabase")


DEFAULT_WORKERS = 20
UPSERT_BATCH_SIZE = 500


# ============================================================
# GraphQL helpers (async) — parsers copiados de src/graphql.py
# Duplicação consciente: o módulo síncrono faz o parse amarrado
# ao httpx.Client síncrono. Aqui temos AsyncClient. Refatorar
# depois se virar dor.
# ============================================================


def _uri_id(uri: str) -> str:
    """'spotify:track:XYZ' -> 'XYZ'."""
    if not uri:
        return ""
    return uri.rsplit(":", 1)[-1] if ":" in uri else uri


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


class TokenHolder:
    """Mantém o token anônimo. Refresh chamado on-demand quando 401 aparece."""

    def __init__(self, auth: SpotifyAuth):
        self._auth = auth
        self.token: str = auth.get_token()
        self._lock = asyncio.Lock()
        self.refresh_count = 0

    async def refresh(self, reason: str = "manual") -> None:
        async with self._lock:
            new_token = await asyncio.to_thread(self._auth.get_token, True)
            if new_token != self.token:
                self.token = new_token
                self.refresh_count += 1
                console.print(f"[cyan]Token refreshed ({reason}, count={self.refresh_count})[/]")


async def graphql_query(
    client: httpx.AsyncClient,
    holder: TokenHolder,
    operation_name: str,
    variables: dict,
    max_attempts: int = 3,
) -> dict:
    """Executa uma query GraphQL com retry em 401 (refresh token) e 429 (backoff)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        url = build_url(operation_name, variables)
        headers = build_headers(holder.token)
        try:
            resp = await client.get(url, headers=headers)
        except (httpx.TransportError, asyncio.TimeoutError) as e:
            last_exc = e
            await asyncio.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data and data["errors"]:
                raise RuntimeError(f"GraphQL errors em {operation_name}: {data['errors']}")
            return data.get("data", {})
        if resp.status_code == 401:
            await holder.refresh("401_received")
            continue
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            console.print(f"[yellow]429 em {operation_name} — pausando {retry_after}s[/]")
            await asyncio.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
            await asyncio.sleep(2 ** attempt)
            continue
        # 400 etc
        body = resp.text[:300]
        if "PersistedQueryNotFound" in body:
            raise RuntimeError(
                f"Hash desatualizado em {operation_name} — rodar discover_hashes.py --write"
            )
        raise RuntimeError(f"{resp.status_code} em {operation_name}: {body}")

    raise RuntimeError(f"{operation_name} falhou após {max_attempts} tentativas: {last_exc}")


# ---------- parsers ----------


def parse_album(album_id: str, data: dict) -> dict:
    album = data.get("albumUnion") or data.get("album") or {}
    if not album:
        return {"id": album_id, "tracks": []}
    tracks_container = album.get("tracks") or album.get("tracksV2") or {}
    items = tracks_container.get("items") or []
    tracks: list[dict] = []
    for it in items:
        tr = it.get("track") if isinstance(it.get("track"), dict) else it
        if not tr:
            continue
        track_id = _uri_id(tr.get("uri", ""))
        if not track_id:
            continue
        # Distinguir "campo ausente" (None) de "valor 0" (track nova, ninguém tocou).
        # CLAUDE.md: NUNCA fazer `int(x or 0)` em playcount — mascara campo faltando.
        raw = tr.get("playcount")
        if raw is None:
            pc = None  # worker pula este snapshot
        else:
            try:
                pc = int(raw)
            except (TypeError, ValueError):
                pc = None
        tracks.append({"id": track_id, "playcount": pc})
    return {"id": album_id, "tracks": tracks}


def parse_artist_overview(artist_id: str, data: dict) -> dict:
    artist = data.get("artistUnion") or data.get("artist") or {}
    if not artist:
        return {"id": artist_id, "monthly_listeners": None, "world_rank": None, "top_cities": []}
    stats = artist.get("stats") or {}
    top_cities_raw = (stats.get("topCities") or {}).get("items") or []
    top_cities = [
        {
            "city": c.get("city"),
            "country": c.get("country"),
            "region": c.get("region"),
            "listeners": c.get("numberOfListeners"),
        }
        for c in top_cities_raw
    ]
    return {
        "id": artist_id,
        "monthly_listeners": stats.get("monthlyListeners"),
        "followers": stats.get("followers"),  # ignoramos no upsert (já temos da API oficial)
        "world_rank": stats.get("worldRank"),
        "top_cities": top_cities[:5],  # top 5 conforme decidido
    }


def parse_discovered_on(artist_id: str, data: dict) -> list[dict]:
    artist = data.get("artistUnion") or data.get("artist") or {}
    related = artist.get("relatedContent") or {}
    discovered = related.get("discoveredOnV2") or related.get("discoveredOn") or {}
    items = discovered.get("items") or []
    playlists: list[dict] = []
    position = 0  # conta só playlists válidas — não enumera sobre podcasts/skips
    for item in items:
        pl = item.get("data") or item
        uri = pl.get("uri", "")
        if not uri.startswith("spotify:playlist:"):
            continue  # podcasts e afins — não consomem posição
        position += 1
        owner_obj = pl.get("ownerV2") or pl.get("owner") or {}
        owner_data = owner_obj.get("data") if isinstance(owner_obj.get("data"), dict) else owner_obj
        images = (pl.get("images") or {}).get("items") or pl.get("imageUrls") or []
        image_url = None
        if images:
            first = images[0]
            if isinstance(first, dict):
                sources = first.get("sources") or []
                image_url = sources[0].get("url") if sources else first.get("url")
            elif isinstance(first, str):
                image_url = first
        playlists.append({
            "spotify_playlist_id": _uri_id(uri),
            "playlist_name": pl.get("name"),
            "owner_spotify_id": _uri_id((owner_data or {}).get("uri", "")),
            "owner_name": (owner_data or {}).get("name"),
            "image_url": image_url,
            "position": position,
        })
    return playlists


# ============================================================
# Stats
# ============================================================

@dataclass
class SyncStats:
    albums_ok: int = 0
    albums_failed: int = 0
    artists_ok: int = 0
    artists_failed: int = 0
    discovered_on_ok: int = 0
    discovered_on_failed: int = 0

    track_snapshots: int = 0
    tracks_skipped_not_in_db: int = 0  # tracks retornadas pelo getAlbum que não estão em spotify_tracks
    artist_snapshot_updates: int = 0
    top_cities_rows: int = 0
    discovered_on_rows: int = 0

    errors: list[str] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts if self.end_ts else 0.0


# ============================================================
# Workers
# ============================================================

async def album_worker(
    worker_id: int,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    holder: TokenHolder,
    stats: SyncStats,
    track_snap_rows: list[dict],
    snapshot_date_iso: str,
    known_track_ids: set[str],
) -> None:
    """
    Busca getAlbum e acumula snapshots APENAS de tracks que já estão em
    spotify_tracks (FK constraint). Tracks do álbum que não estão cadastradas
    são contadas em stats.tracks_skipped_not_in_db — popular spotify_tracks
    é responsabilidade do collector, não deste sync.
    """
    while True:
        try:
            album_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            variables = {"uri": f"spotify:album:{album_id}", "locale": "", "offset": 0, "limit": 300}
            data = await graphql_query(client, holder, "getAlbum", variables)
            parsed = parse_album(album_id, data)
            for tr in parsed["tracks"]:
                if tr["playcount"] is None:
                    continue
                if tr["id"] not in known_track_ids:
                    stats.tracks_skipped_not_in_db += 1
                    continue
                track_snap_rows.append({
                    "spotify_track_id": tr["id"],
                    "date": snapshot_date_iso,
                    "playcount": tr["playcount"],
                })
            stats.albums_ok += 1
        except Exception as e:
            stats.albums_failed += 1
            stats.errors.append(f"album {album_id}: {e}")
        finally:
            queue.task_done()


async def artist_worker(
    worker_id: int,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    holder: TokenHolder,
    stats: SyncStats,
    artist_snap_updates: list[dict],
    top_cities_rows: list[dict],
    discovered_on_rows: list[dict],
    snapshot_date_iso: str,
) -> None:
    while True:
        try:
            artist_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            # 1. Overview
            ov_data = await graphql_query(
                client, holder, "queryArtistOverview",
                {"uri": f"spotify:artist:{artist_id}", "locale": "", "includePrerelease": True},
            )
            overview = parse_artist_overview(artist_id, ov_data)
            artist_snap_updates.append({
                "spotify_artist_id": artist_id,
                "date": snapshot_date_iso,
                "monthly_listeners": overview["monthly_listeners"],
                "world_rank": overview["world_rank"],
            })
            for rank, c in enumerate(overview["top_cities"], start=1):
                if not c.get("city"):
                    continue
                top_cities_rows.append({
                    "spotify_artist_id": artist_id,
                    "date": snapshot_date_iso,
                    "rank": rank,
                    "city": c.get("city"),
                    "country": c.get("country"),
                    "region": c.get("region"),
                    "listeners": c.get("listeners"),
                })
            stats.artists_ok += 1

            # 2. Discovered on (request separado)
            try:
                do_data = await graphql_query(
                    client, holder, "queryArtistDiscoveredOn",
                    {"uri": f"spotify:artist:{artist_id}"},
                )
                for pl in parse_discovered_on(artist_id, do_data):
                    discovered_on_rows.append({
                        "spotify_artist_id": artist_id,
                        "date": snapshot_date_iso,
                        **pl,
                    })
                stats.discovered_on_ok += 1
            except Exception as e:
                stats.discovered_on_failed += 1
                stats.errors.append(f"discovered_on {artist_id}: {e}")
        except Exception as e:
            stats.artists_failed += 1
            stats.errors.append(f"artist {artist_id}: {e}")
        finally:
            queue.task_done()


# ============================================================
# Flush final pro Supabase
# ============================================================


async def flush_to_supabase(
    sb: SupabaseClient,
    stats: SyncStats,
    track_snap_rows: list[dict],
    artist_snap_updates: list[dict],
    top_cities_rows: list[dict],
    discovered_on_rows: list[dict],
    snapshot_date_iso: str,
    artist_ids_in_run: set[str],
) -> None:
    # 1. track_snapshots — simples upsert
    if track_snap_rows:
        # dedup por (spotify_track_id, date) mantendo última entrada
        seen: dict[tuple[str, str], dict] = {}
        for r in track_snap_rows:
            seen[(r["spotify_track_id"], r["date"])] = r
        deduped = list(seen.values())
        console.print(f"  Enviando [cyan]{len(deduped)}[/] linhas → spotify_track_snapshots")
        stats.track_snapshots = await sb.upsert(
            "spotify_track_snapshots", deduped, batch_size=UPSERT_BATCH_SIZE,
        )

    # 2. artist_snapshots — aqui tem uma sutileza: a tabela já pode ter linha do dia
    # com popularity/follower_count da API oficial. Fazer UPSERT com só
    # monthly_listeners/world_rank pode sobrescrever colunas. Solução: merge-duplicates
    # do PostgREST só substitui as COLUNAS PRESENTES no payload. Então funciona.
    if artist_snap_updates:
        console.print(f"  Enviando [cyan]{len(artist_snap_updates)}[/] upserts → spotify_artist_snapshots")
        stats.artist_snapshot_updates = await sb.upsert(
            "spotify_artist_snapshots", artist_snap_updates, batch_size=UPSERT_BATCH_SIZE,
        )

    # 3. top_cities — deletar e reinserir (rank pode mudar de ordem)
    # Só deletamos do snapshot de hoje dos artistas que rodaram nesta run.
    if artist_ids_in_run:
        # PostgREST IN filter precisa ser escapado como string CSV entre parens
        # Em volumes grandes (> ~500 IDs), quebrar em chunks pra não estourar query string
        artist_id_list = sorted(artist_ids_in_run)
        chunk_size = 200
        for i in range(0, len(artist_id_list), chunk_size):
            chunk = artist_id_list[i : i + chunk_size]
            in_clause = "(" + ",".join(f'"{aid}"' for aid in chunk) + ")"
            where = f"spotify_artist_id=in.{in_clause}&date=eq.{snapshot_date_iso}"
            await sb.delete_where("spotify_artist_top_cities_snapshots", where)
            await sb.delete_where("spotify_artist_discovered_on_snapshots", where)

    if top_cities_rows:
        console.print(f"  Enviando [cyan]{len(top_cities_rows)}[/] linhas → spotify_artist_top_cities_snapshots")
        stats.top_cities_rows = await sb.upsert(
            "spotify_artist_top_cities_snapshots", top_cities_rows, batch_size=UPSERT_BATCH_SIZE,
        )

    if discovered_on_rows:
        console.print(f"  Enviando [cyan]{len(discovered_on_rows)}[/] linhas → spotify_artist_discovered_on_snapshots")
        stats.discovered_on_rows = await sb.upsert(
            "spotify_artist_discovered_on_snapshots", discovered_on_rows, batch_size=UPSERT_BATCH_SIZE,
        )


# ============================================================
# Main
# ============================================================


async def main_async(args) -> int:
    snapshot_date = args.snapshot_date or date.today()
    snapshot_date_iso = snapshot_date.isoformat()
    console.print(f"[bold cyan]Sync from Supabase[/]  snapshot_date=[yellow]{snapshot_date_iso}[/]")
    console.print(f"Workers: [yellow]{args.workers}[/]  Dry-run: [yellow]{args.dry_run}[/]  Limit: [yellow]{args.limit or 'sem'}[/]\n")

    stats = SyncStats()
    stats.start_ts = time.monotonic()

    async with SupabaseClient() as sb:
        # ---- 1. Load IDs
        console.print("[bold]1. Carregando IDs do Supabase...[/]")
        tracks = await sb.select_all(
            "spotify_tracks",
            columns="spotify_id,album_id",
            where="album_id=not.is.null",
        )
        artists = await sb.select_all("spotify_artists", columns="spotify_id")
        console.print(f"  tracks: [cyan]{len(tracks)}[/], artists: [cyan]{len(artists)}[/]")

        albums_set: set[str] = {t["album_id"] for t in tracks if t.get("album_id")}
        artists_set: set[str] = {a["spotify_id"] for a in artists if a.get("spotify_id")}
        known_track_ids: set[str] = {t["spotify_id"] for t in tracks if t.get("spotify_id")}
        console.print(f"  albums distintos: [cyan]{len(albums_set)}[/], artistas: [cyan]{len(artists_set)}[/]")

        if args.limit:
            albums_set = set(list(albums_set)[: args.limit])
            artists_set = set(list(artists_set)[: args.limit])
            console.print(f"  [yellow]--limit {args.limit}[/] aplicado")

        if args.dry_run:
            console.print(
                f"\n[yellow]DRY RUN[/] — faria {len(albums_set)} getAlbum + "
                f"{len(artists_set)}×2 chamadas de artista = "
                f"[bold]{len(albums_set) + 2*len(artists_set)}[/] requests totais.\n"
                f"Sem flush ao Supabase."
            )
            return 0

        # ---- 2. Setup GraphQL async
        console.print("\n[bold]2. Obtendo token anônimo...[/]")
        http_sync = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
        auth = SpotifyAuth(client=http_sync)
        holder = TokenHolder(auth)
        console.print(f"  token OK ({len(holder.token)} chars)")

        # ---- 3. Workers
        album_queue: asyncio.Queue = asyncio.Queue()
        for a in albums_set:
            album_queue.put_nowait(a)
        artist_queue: asyncio.Queue = asyncio.Queue()
        for a in artists_set:
            artist_queue.put_nowait(a)

        track_snap_rows: list[dict] = []
        artist_snap_updates: list[dict] = []
        top_cities_rows: list[dict] = []
        discovered_on_rows: list[dict] = []

        limits = httpx.Limits(max_connections=50, max_keepalive_connections=25)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=limits,
            follow_redirects=True,
        ) as client:

            console.print(f"\n[bold]3. Buscando {len(albums_set)} albums com {args.workers} workers...[/]")
            album_tasks = [
                asyncio.create_task(album_worker(
                    i, album_queue, client, holder, stats,
                    track_snap_rows, snapshot_date_iso, known_track_ids,
                ))
                for i in range(args.workers)
            ]
            await asyncio.gather(*album_tasks, return_exceptions=True)
            console.print(
                f"  albums OK=[green]{stats.albums_ok}[/] FAIL=[red]{stats.albums_failed}[/]  "
                f"track_snapshots=[cyan]{len(track_snap_rows)}[/] "
                f"skipped (não em spotify_tracks)=[yellow]{stats.tracks_skipped_not_in_db}[/]"
            )

            console.print(f"\n[bold]4. Buscando {len(artists_set)} artists (overview + discovered_on)...[/]")
            artist_tasks = [
                asyncio.create_task(artist_worker(
                    i, artist_queue, client, holder, stats,
                    artist_snap_updates, top_cities_rows, discovered_on_rows,
                    snapshot_date_iso,
                ))
                for i in range(args.workers)
            ]
            await asyncio.gather(*artist_tasks, return_exceptions=True)
            console.print(
                f"  artists OK=[green]{stats.artists_ok}[/] FAIL=[red]{stats.artists_failed}[/] "
                f"discovered_on OK=[green]{stats.discovered_on_ok}[/] FAIL=[red]{stats.discovered_on_failed}[/]  "
                f"top_cities=[cyan]{len(top_cities_rows)}[/] discovered_on_rows=[cyan]{len(discovered_on_rows)}[/]"
            )

        auth.close()
        http_sync.close()

        # ---- 5. Flush
        console.print("\n[bold]5. Flushing para Supabase...[/]")
        await flush_to_supabase(
            sb, stats,
            track_snap_rows, artist_snap_updates, top_cities_rows, discovered_on_rows,
            snapshot_date_iso,
            artists_set,
        )

    stats.end_ts = time.monotonic()

    # ---- 6. Summary
    t = Table(title=f"[bold]Resumo da run — {snapshot_date_iso}[/]", header_style="bold cyan")
    t.add_column("Métrica")
    t.add_column("Valor", justify="right")
    t.add_row("Duração", f"{stats.duration_s:.1f}s")
    t.add_row("Albums OK / FAIL", f"{stats.albums_ok} / {stats.albums_failed}")
    t.add_row("Artists OK / FAIL", f"{stats.artists_ok} / {stats.artists_failed}")
    t.add_row("DiscoveredOn OK / FAIL", f"{stats.discovered_on_ok} / {stats.discovered_on_failed}")
    t.add_row("track_snapshots gravados", str(stats.track_snapshots))
    t.add_row("tracks skipped (não em spotify_tracks)", str(stats.tracks_skipped_not_in_db))
    t.add_row("artist_snapshots upserts", str(stats.artist_snapshot_updates))
    t.add_row("top_cities rows", str(stats.top_cities_rows))
    t.add_row("discovered_on rows", str(stats.discovered_on_rows))
    console.print(t)

    if stats.errors:
        console.print(f"\n[red]Erros ({len(stats.errors)} primeiros 10):[/]")
        for e in stats.errors[:10]:
            console.print(f"  [red]•[/] {e}")

    # ---- 7. Log estruturado em arquivo (pra auditoria em runs unattended)
    runs_dir = Path("data") / "sync_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{snapshot_date_iso}.json"
    log_payload = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_date": snapshot_date_iso,
        "workers": args.workers,
        "limit": args.limit or None,
        "duration_s": round(stats.duration_s, 2),
        "albums": {"ok": stats.albums_ok, "failed": stats.albums_failed},
        "artists": {"ok": stats.artists_ok, "failed": stats.artists_failed},
        "discovered_on": {"ok": stats.discovered_on_ok, "failed": stats.discovered_on_failed},
        "writes": {
            "track_snapshots": stats.track_snapshots,
            "tracks_skipped_not_in_db": stats.tracks_skipped_not_in_db,
            "artist_snapshots": stats.artist_snapshot_updates,
            "top_cities_rows": stats.top_cities_rows,
            "discovered_on_rows": stats.discovered_on_rows,
        },
        "errors": stats.errors[:50],  # capa: no máx 50 erros pra não explodir o arquivo
        "errors_count": len(stats.errors),
        "token_refreshes": holder.refresh_count,
    }
    log_path.write_text(json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\n[dim]Log: {log_path}[/]")

    return 0 if (stats.albums_failed + stats.artists_failed) == 0 else 1


def parse_iso_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Mostra o que faria sem escrever")
    p.add_argument("--limit", type=int, default=0, help="Limita N albums + N artists (smoke test)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--snapshot-date", type=parse_iso_date, default=None,
                   help="ISO date (YYYY-MM-DD). Default: hoje")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
