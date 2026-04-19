"""
Constrói um pool diverso de IDs (albums, tracks, artists) pro stress test assíncrono.

Estratégia:
  1. Parte de N artistas-semente (hardcoded, mistura gênero/região)
  2. Pra cada artista: busca discography_all -> extrai todos os album_ids
  3. Pra uma amostra de álbuns: busca getAlbum -> extrai track_ids
  4. Dedupe + salva em data/stress_pool.json

Rodar 1 vez antes do stress_test_async. Demora ~2-4 min dependendo da rede.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from config import settings
from src.auth import SpotifyAuth
from src.graphql import SpotifyGraphQL, SpotifyGraphQLError

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=True)],
)
logger = logging.getLogger("build_stress_pool")


# Artistas-semente — diversidade de popularidade, gênero e região.
# IDs extraídos do Spotify público.
SEED_ARTISTS = [
    ("0GAO7MNJFFL3ttIv2hRl2p", "Samuel Messias"),       # gospel BR
    ("06HL4z0CvFAxyc27GXpf02", "Taylor Swift"),          # pop US (mega)
    ("2YZyLoL8N0Wb9xBt1NhZWg", "Kendrick Lamar"),        # hip-hop US
    ("0k17h0D3J5VfsdmQ1iZtE9", "Pink Floyd"),            # classic rock UK
    ("4dpARuHxo51G3z768sgnrY", "Adele"),                 # pop UK (mega)
    ("4Z8W4fKeB5YxbusRsdQVPb", "Radiohead"),             # alt rock UK
    ("00FQb4jTyendYWaN8pK0wa", "Lana Del Rey"),          # alt pop US
    ("6qqNVTkY8uBg9cP3Jd7DAH", "Billie Eilish"),         # pop US
    ("4oLeXFyACqeem2VImYeBFe", "Fleetwood Mac"),         # classic rock
    ("3TVXtAsR1Inumwj472S9r4", "Drake"),                 # hip-hop CA (mega)
    ("6eUKZXaKkcviH0Ku9w2n3V", "Ed Sheeran"),            # pop UK
    ("7dGJo4pcD2V6oG8kP0tJRR", "Eminem"),                # hip-hop US
    ("49qiE8dj4JuNdpYGRPdKbF", "Djavan"),                # MPB BR
    ("7oPftvlwr6VrsViSDV7fJY", "Green Day"),             # punk rock US
    ("1Cs0zKBU1kc0i8ypK3B9ai", "David Guetta"),          # EDM FR
]

# Quantos álbuns de cada artista amostrar pra expandir tracks.
ALBUMS_PER_ARTIST_FOR_TRACKS = 3


def build_pool(
    graphql: SpotifyGraphQL,
    seed_artists: list[tuple[str, str]],
    albums_sample: int,
) -> dict:
    all_artist_ids: set[str] = set()
    all_album_ids: set[str] = set()
    all_track_ids: set[str] = set()
    per_artist_counts: dict[str, dict] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_disco = progress.add_task("discografia", total=len(seed_artists))

        for artist_id, artist_name in seed_artists:
            all_artist_ids.add(artist_id)
            try:
                disco = graphql.get_artist_discography_all(artist_id)
                releases = disco.get("releases") or []
                album_ids_here = [r["id"] for r in releases if r.get("id")]
                all_album_ids.update(album_ids_here)

                # sample de álbuns pra expandir tracks
                sample = random.sample(
                    album_ids_here,
                    min(albums_sample, len(album_ids_here)),
                )
                tracks_from_this_artist = 0
                for aid in sample:
                    try:
                        album = graphql.get_album(aid)
                        for t in album.get("tracks") or []:
                            tid = t.get("id")
                            if tid:
                                all_track_ids.add(tid)
                                tracks_from_this_artist += 1
                            # aproveita pra coletar artistas das tracks também
                            for a in t.get("artists") or []:
                                if a.get("id"):
                                    all_artist_ids.add(a["id"])
                    except SpotifyGraphQLError as e:
                        logger.warning("getAlbum falhou pra %s: %s", aid, e)

                per_artist_counts[artist_id] = {
                    "name": artist_name,
                    "releases": len(album_ids_here),
                    "tracks_sampled": tracks_from_this_artist,
                }
                console.print(
                    f"  [green]{artist_name:<20}[/] "
                    f"{len(album_ids_here):>4} releases / "
                    f"{tracks_from_this_artist:>4} tracks amostradas"
                )
            except SpotifyGraphQLError as e:
                logger.error("discography falhou pra %s (%s): %s", artist_name, artist_id, e)
                per_artist_counts[artist_id] = {"name": artist_name, "error": str(e)[:200]}

            progress.update(task_disco, advance=1)

    return {
        "generated_at": datetime.now().isoformat(),
        "seed_artists_count": len(seed_artists),
        "albums": sorted(all_album_ids),
        "tracks": sorted(all_track_ids),
        "artists": sorted(all_artist_ids),
        "stats": {
            "total_albums": len(all_album_ids),
            "total_tracks": len(all_track_ids),
            "total_artists": len(all_artist_ids),
            "per_artist": per_artist_counts,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--output",
        default="data/stress_pool.json",
        help="caminho de saída do pool (default: data/stress_pool.json)",
    )
    p.add_argument(
        "--albums-sample",
        type=int,
        default=ALBUMS_PER_ARTIST_FOR_TRACKS,
        help=f"quantos álbuns amostrar por artista pra extrair tracks (default {ALBUMS_PER_ARTIST_FOR_TRACKS})",
    )
    args = p.parse_args()

    console.print(f"[bold cyan]Build stress pool[/] — {len(SEED_ARTISTS)} artistas-semente")
    console.print(f"Saída: [yellow]{args.output}[/]\n")

    http = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
    auth = SpotifyAuth(client=http)
    graphql = SpotifyGraphQL(auth=auth, client=http)

    try:
        pool = build_pool(graphql, SEED_ARTISTS, args.albums_sample)
    finally:
        graphql.close()
        auth.close()
        http.close()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = pool["stats"]
    console.print(f"\n[bold green]Pool gerado:[/]")
    console.print(f"  {stats['total_albums']} álbuns únicos")
    console.print(f"  {stats['total_tracks']} tracks únicas")
    console.print(f"  {stats['total_artists']} artistas únicos")
    console.print(f"  Arquivo: [yellow]{out_path}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
