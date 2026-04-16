"""
Adicionar tracks/álbuns/artistas ao monitoramento.

Uso:
  python -m scripts.add_tracks --album <URL_ou_ID>
  python -m scripts.add_tracks --track <URL_ou_ID>
  python -m scripts.add_tracks --csv tracks.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

# garante que rodar como script funcione
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scraper import SpotifyScraper  # noqa: E402

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, markup=True)],
)
logger = logging.getLogger("add_tracks")


URL_ID_RE = re.compile(r"(?:open\.spotify\.com/)?(?:embed/)?(track|album|artist|playlist)/([A-Za-z0-9]+)")


def extract_id(kind: str, value: str) -> str:
    """Extrai ID de URL do Spotify ou aceita ID puro."""
    value = value.strip()
    m = URL_ID_RE.search(value)
    if m:
        if m.group(1) != kind:
            raise ValueError(f"URL é do tipo '{m.group(1)}', esperado '{kind}': {value}")
        return m.group(2)
    # assume ID puro
    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        return value
    raise ValueError(f"Não foi possível extrair {kind} ID de: {value}")


def main() -> int:
    p = argparse.ArgumentParser(description="Adicionar tracks ao monitoramento")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--album", help="URL ou ID de álbum Spotify")
    g.add_argument("--track", help="URL ou ID de track Spotify")
    g.add_argument("--csv", help="CSV com colunas track_id,album_id,artist_id")
    args = p.parse_args()

    with SpotifyScraper() as scraper:
        if args.album:
            album_id = extract_id("album", args.album)
            console.print(f"[cyan]Adicionando álbum[/] {album_id}")
            n = scraper.add_album(album_id)
            console.print(f"[green]OK[/] {n} tracks registradas")

        elif args.track:
            track_id = extract_id("track", args.track)
            # pega metadata via embed e registra como track individual;
            # se tiver album_id, usa add_album para pegar todas (mais eficiente)
            console.print(f"[cyan]Lookup track[/] {track_id}")
            try:
                tr = scraper.embed.get_track(track_id)
            except Exception as e:
                console.print(f"[red]Erro[/] buscando track: {e}")
                return 1
            album_id = tr.get("album", {}).get("id")
            if album_id:
                console.print(f"[cyan]Track pertence ao álbum[/] {album_id} — registrando álbum inteiro")
                n = scraper.add_album(album_id)
                console.print(f"[green]OK[/] {n} tracks registradas")
            else:
                from src.models import MonitoredTrack
                artists = tr.get("artists") or []
                primary = artists[0] if artists else {"id": "", "name": ""}
                mt = MonitoredTrack(
                    track_id=track_id,
                    track_name=tr.get("name", ""),
                    artist_id=primary.get("id", ""),
                    artist_name=primary.get("name", ""),
                    album_id="",
                    album_name="",
                )
                scraper.db.upsert_monitored_track(mt)
                console.print(f"[green]OK[/] track registrada sem álbum")

        elif args.csv:
            path = Path(args.csv)
            if not path.exists():
                console.print(f"[red]CSV não encontrado:[/] {path}")
                return 1
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                albums_seen: set[str] = set()
                for row in reader:
                    aid = (row.get("album_id") or "").strip()
                    tid = (row.get("track_id") or "").strip()
                    artist_id = (row.get("artist_id") or "").strip()
                    if aid and aid not in albums_seen:
                        albums_seen.add(aid)
                        try:
                            n = scraper.add_album(aid)
                            console.print(f"[green]+[/] álbum {aid}: {n} tracks")
                        except Exception as e:
                            console.print(f"[red]Erro[/] álbum {aid}: {e}")
                    elif tid and not aid:
                        try:
                            tr = scraper.embed.get_track(tid)
                            sub_aid = tr.get("album", {}).get("id")
                            if sub_aid and sub_aid not in albums_seen:
                                albums_seen.add(sub_aid)
                                n = scraper.add_album(sub_aid)
                                console.print(f"[green]+[/] álbum {sub_aid} (via track {tid}): {n} tracks")
                        except Exception as e:
                            console.print(f"[red]Erro[/] track {tid}: {e}")
                    elif artist_id:
                        console.print(f"[yellow]Aviso:[/] artist_id puro ainda não suportado no MVP: {artist_id}")

        status = scraper.db.status()
        console.print("\n[bold]Status:[/]", status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
