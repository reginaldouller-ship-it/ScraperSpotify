"""
Popula o DB com todas as tracks de um artista (como principal E como feature)
e salva snapshot (playcount do dia) de cada uma.

Por default:
  - INCLUI: album, ep, single (como artista principal)
  - INCLUI: appears_on (como feature, limitado a 200 releases mais recentes)
  - EXCLUI: compilation (álbuns do tipo "best of" que replicam músicas)

A tabela `artist_tracks(artist_id, track_id, is_primary)` é populada com as
relações, permitindo filtrar depois por role.

Uso:
  python -m scripts.populate_from_artist --artist 7FNnA9vBm6EKceENgCGRMb
  python -m scripts.populate_from_artist --artist 7FNnA9vBm6EKceENgCGRMb --include-compilations
  python -m scripts.populate_from_artist --artist 7FNnA9vBm6EKceENgCGRMb --no-features
  python -m scripts.populate_from_artist --artist 7FNnA9vBm6EKceENgCGRMb --features-limit 500
  python -m scripts.populate_from_artist --artist 7FNnA9vBm6EKceENgCGRMb --dry-run
"""
from __future__ import annotations

import argparse
import io
import json as _json
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.models import ArtistSnapshot, MonitoredArtist, MonitoredTrack, TrackSnapshot  # noqa: E402
from src.scraper import SpotifyScraper  # noqa: E402

console = Console()

ARTIST_RE = re.compile(r"(?:open\.spotify\.com/)?artist/([A-Za-z0-9]+)")
OWN_RELEASE_TYPES = {"album", "ep", "single"}       # artista é principal
# "compilation" → ignorado por default (muitos duplicatas de músicas próprias)
# "appears_on" → tratado via queryArtistAppearsOn, não discography.all
DEFAULT_FEATURES_LIMIT = 200


def parse_artist(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        return value
    m = ARTIST_RE.search(value)
    if m:
        return m.group(1)
    raise ValueError(f"Não consegui extrair artist_id de: {value!r}")


def fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def build_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--artist", required=True, help="URL ou ID do artista no Spotify")
    p.add_argument("--include-compilations", action="store_true", help="Incluir álbuns tipo 'compilation'")
    p.add_argument("--no-features", action="store_true", help="NÃO buscar appears_on (features)")
    p.add_argument("--features-limit", type=int, default=DEFAULT_FEATURES_LIMIT,
                   help=f"Limite de releases de features a processar (default: {DEFAULT_FEATURES_LIMIT}; use 0 pra tudo)")
    p.add_argument("--limit", type=int, help="Processar só N primeiros releases próprios (pra teste)")
    p.add_argument("--workers", type=int, default=settings.DEFAULT_WORKERS,
                   help=f"Workers paralelos pra fetch de álbuns (default: {settings.DEFAULT_WORKERS}). Use 1 pra sequencial.")
    p.add_argument("--dry-run", action="store_true", help="Lista o que seria processado e sai")
    p.add_argument("--skip-snapshot", action="store_true", help="Só registra, não cria snapshot diário")
    p.add_argument("--skip-artist-snapshot", action="store_true", help="Não busca overview do artista")
    args = p.parse_args()

    artist_id = parse_artist(args.artist)
    snapshot_date = date.today()

    allowed_types = set(OWN_RELEASE_TYPES)
    if args.include_compilations:
        allowed_types.add("compilation")

    console.rule(f"[bold cyan]Populando tracks de {artist_id}[/]")

    with SpotifyScraper() as scraper:
        # ========== 1. Discografia própria ==========
        console.print("[cyan]Buscando discografia própria…[/]")
        try:
            disco = scraper.graphql.get_artist_discography_all(artist_id, limit=50)
        except Exception as e:
            console.print(f"[red]Falha ao buscar discografia:[/] {e}")
            return 1

        own_releases = disco.get("releases") or []
        total_disco = disco.get("total_count") or len(own_releases)
        console.print(f"[green][OK][/] {len(own_releases)}/{total_disco} releases na discografia")

        # Filtra tipos
        before = len(own_releases)
        own_releases = [r for r in own_releases if r.get("type") in allowed_types]
        if before != len(own_releases):
            console.print(f"[dim]Tipos permitidos {allowed_types}: {before} → {len(own_releases)} releases[/]")

        if args.limit and args.limit < len(own_releases):
            own_releases = own_releases[: args.limit]
            console.print(f"[dim]--limit aplicado: {args.limit} primeiros[/]")

        # ========== 2. Appears on (features) ==========
        features_releases: list[dict] = []
        total_appears_on: int | None = None
        if not args.no_features:
            console.print("[cyan]Buscando appears_on (features)…[/]")
            try:
                fmax = args.features_limit if args.features_limit and args.features_limit > 0 else None
                appears = scraper.graphql.get_artist_appears_on(artist_id, limit=50, max_items=fmax)
                features_releases = appears.get("releases") or []
                total_appears_on = appears.get("total_count")
                console.print(f"[green][OK][/] {len(features_releases)}/{total_appears_on} releases de features")
            except Exception as e:
                console.print(f"[yellow]Features indisponíveis ({e}). Prosseguindo sem.[/]")

        # Resumo
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="dim", justify="right")
        summary.add_column(style="bold")
        by_type = Counter(r.get("type", "?") for r in own_releases)
        for t_name in ("album", "ep", "single", "compilation"):
            if t_name in by_type:
                summary.add_row(f"{t_name} (próprio):", str(by_type[t_name]))
        summary.add_row("features (appears_on):", str(len(features_releases)))
        summary.add_row("[bold]total releases a processar:[/]", f"[bold]{len(own_releases) + len(features_releases)}[/]")
        console.print(summary)

        if args.dry_run:
            console.print("\n[yellow]Dry-run — nenhum álbum fetched.[/]")
            console.print("\nReleases próprios (primeiros 10):")
            for r in own_releases[:10]:
                console.print(f"  - [{r.get('type','?'):11}] {r.get('name','')} ({r.get('release_date') or ''})")
            if features_releases:
                console.print("\nFeatures (primeiros 10):")
                for r in features_releases[:10]:
                    primary = ", ".join(a["name"] for a in (r.get("primary_artists") or [])[:2])
                    console.print(f"  - {r.get('name','')} — [dim]por {primary}[/]")
            return 0

        if not own_releases and not features_releases:
            console.print("[red]Nenhum release para processar.[/]")
            return 1

        # ========== 3. Processamento ==========
        stats = {
            "primary": 0,
            "feature": 0,
            "snapshots": 0,
            "no_playcount": 0,
            "albums_ok": 0,
        }
        albums_failed: list[tuple[str, str, str]] = []
        stats_lock = threading.Lock()

        def process_release(release: dict, role: str) -> None:
            """role = 'primary' | 'feature'. Thread-safe."""
            release_id = release["id"]
            release_name = release.get("name", "")[:40]

            try:
                album = scraper._fetch_album(release_id)
            except Exception as e:
                with stats_lock:
                    albums_failed.append((release_id, release_name, str(e)[:100]))
                return

            album_name = album.get("name", "")
            album_artists = album.get("artists") or []
            album_primary = album_artists[0] if album_artists else {"id": "", "name": ""}

            local_primary = 0
            local_feature = 0
            local_snapshots = 0
            local_no_pc = 0

            for tr in album.get("tracks", []):
                tid = tr["id"]
                t_artists = tr.get("artists") or []
                t_artist_ids = [a.get("id", "") for a in t_artists]
                t_primary = t_artists[0] if t_artists else album_primary

                is_our_track = False
                is_primary_role = False
                if role == "primary":
                    is_our_track = True
                    is_primary_role = (artist_id in t_artist_ids and t_artist_ids[0] == artist_id)
                    if not is_primary_role and artist_id not in t_artist_ids:
                        is_primary_role = True  # álbum é dele, track sem créditos explícitos
                else:  # feature
                    if artist_id in t_artist_ids:
                        is_our_track = True
                        is_primary_role = (t_artist_ids[0] == artist_id)

                if not is_our_track:
                    continue

                # DB writes — SQLite com WAL + connection por chamada é thread-safe
                scraper.db.upsert_monitored_track(MonitoredTrack(
                    track_id=tid,
                    track_name=tr.get("name", ""),
                    artist_id=t_primary.get("id", ""),
                    artist_name=t_primary.get("name", ""),
                    album_id=release_id,
                    album_name=album_name,
                ))
                scraper.db.upsert_artist_track(artist_id=artist_id, track_id=tid, is_primary=is_primary_role)

                if is_primary_role:
                    local_primary += 1
                else:
                    local_feature += 1

                if not args.skip_snapshot:
                    playcount = tr.get("playcount")
                    if playcount is None:
                        local_no_pc += 1
                    else:
                        try:
                            scraper.db.upsert_track_snapshot(TrackSnapshot(
                                track_id=tid,
                                track_name=tr.get("name", ""),
                                artist_id=t_primary.get("id", ""),
                                artist_name=t_primary.get("name", ""),
                                album_id=release_id,
                                album_name=album_name,
                                playcount=int(playcount),
                                snapshot_date=snapshot_date,
                                source=tr.get("source", "graphql"),
                                duration_ms=tr.get("duration_ms"),
                                explicit=tr.get("explicit"),
                            ))
                            local_snapshots += 1
                        except Exception as e:
                            with stats_lock:
                                albums_failed.append((tid, "snapshot", str(e)[:100]))

            with stats_lock:
                stats["primary"] += local_primary
                stats["feature"] += local_feature
                stats["snapshots"] += local_snapshots
                stats["no_playcount"] += local_no_pc
                stats["albums_ok"] += 1

        total_to_process = len(own_releases) + len(features_releases)
        tasks_to_run: list[tuple[dict, str]] = [(r, "primary") for r in own_releases]
        tasks_to_run.extend((r, "feature") for r in features_releases)

        workers = max(1, args.workers)
        if workers > 1:
            console.print(f"[dim]Usando {workers} workers paralelos. Teste de stress foi com 1 worker até 4.4 req/s sem 429. Se aparecer rate limit, reduzir com --workers 1 ou 2.[/]")

        start_time = time.monotonic()
        progress = build_progress()
        with progress:
            task = progress.add_task(f"Processando {total_to_process} releases com {workers} workers…", total=total_to_process)

            if workers == 1:
                for release, role in tasks_to_run:
                    progress.update(task, description=f"[cyan]({role[:1]}) {release.get('name','')[:40]}[/]")
                    process_release(release, role)
                    progress.advance(task)
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {
                        ex.submit(process_release, release, role): (release, role)
                        for release, role in tasks_to_run
                    }
                    for fut in as_completed(futures):
                        release, role = futures[fut]
                        progress.update(task, description=f"[cyan]({role[:1]}) {release.get('name','')[:40]}[/]")
                        try:
                            fut.result()
                        except Exception as e:
                            with stats_lock:
                                albums_failed.append((release["id"], release.get("name","")[:40], str(e)[:100]))
                        progress.advance(task)

        elapsed = time.monotonic() - start_time
        actual_rps = total_to_process / elapsed if elapsed > 0 else 0

        tracks_registered_primary = stats["primary"]
        tracks_registered_feature = stats["feature"]
        snapshots_saved = stats["snapshots"]
        tracks_no_playcount = stats["no_playcount"]
        albums_ok = stats["albums_ok"]

        # ========== 4. Artist snapshot ==========
        if not args.skip_artist_snapshot:
            console.print("\n[cyan]Buscando overview do artista…[/]")
            try:
                overview = scraper.graphql.get_artist_overview(artist_id)
                scraper.db.upsert_artist_snapshot(ArtistSnapshot(
                    artist_id=artist_id,
                    artist_name=overview.get("name", ""),
                    monthly_listeners=overview.get("monthly_listeners"),
                    followers=overview.get("followers"),
                    world_rank=overview.get("world_rank"),
                    popularity=overview.get("popularity"),
                    top_cities_json=_json.dumps(overview.get("top_cities") or [], ensure_ascii=False),
                    biography=overview.get("biography"),
                    snapshot_date=snapshot_date,
                ))
                scraper.db.upsert_monitored_artist(MonitoredArtist(
                    artist_id=artist_id,
                    artist_name=overview.get("name", ""),
                ))
                console.print(f"[green][OK][/] Snapshot do artista salvo ({overview.get('name')})")
                console.print(f"         monthly_listeners = [green]{fmt_int(overview.get('monthly_listeners'))}[/]")
                console.print(f"         followers         = [cyan]{fmt_int(overview.get('followers'))}[/]")
                if overview.get("world_rank"):
                    rk = f"{overview['world_rank']:,}".replace(",", ".")
                    console.print(f"         world_rank        = [magenta]#{rk}[/]")
            except Exception as e:
                console.print(f"[red]Falha buscando overview:[/] {e}")

        # ========== 5. Relatório ==========
        console.rule("[bold green]Concluído[/]")
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style="dim", justify="right")
        t.add_column(style="bold")
        t.add_row("Releases processados:", f"[green]{albums_ok}[/]")
        if albums_failed:
            t.add_row("Releases falhados:", f"[red]{len(albums_failed)}[/]")
        t.add_row("Tracks como PRIMARY:", f"[cyan]{tracks_registered_primary}[/]")
        t.add_row("Tracks como FEATURE:", f"[yellow]{tracks_registered_feature}[/]")
        t.add_row("Snapshots salvos:", f"[green]{snapshots_saved}[/]")
        if tracks_no_playcount:
            t.add_row("Tracks sem playcount:", f"[yellow]{tracks_no_playcount}[/]")
        t.add_row("Data do snapshot:", str(snapshot_date))
        t.add_row("Tempo de fetch:", f"[cyan]{elapsed:.1f}s[/] ([green]{actual_rps:.1f} req/s[/])")
        console.print(t)

        if albums_failed:
            console.print("\n[red]Releases que falharam:[/]")
            for aid, name, err in albums_failed[:10]:
                console.print(f"  [dim]{aid}[/] {name}: {err}")
            if len(albums_failed) > 10:
                console.print(f"  [dim]… e mais {len(albums_failed) - 10}[/]")

        status = scraper.db.status()
        console.print(f"\n[dim]Status do DB: {status}[/]")
        console.print(f"\n[dim]Ver tudo do artista:[/]")
        console.print(f"  [cyan]python -m scripts.list_tracks --artist {artist_id}[/]")
        console.print(f"  [cyan]python -m scripts.list_tracks --artist {artist_id} --only-features[/]")
        console.print(f"  [cyan]python -m scripts.export_csv --artist {artist_id} --output data/{artist_id}.csv[/]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
