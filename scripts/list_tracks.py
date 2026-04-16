"""
Lista todas as tracks monitoradas no DB com seus últimos playcounts.

Uso:
  python -m scripts.list_tracks                                   # tudo no DB
  python -m scripts.list_tracks --artist 7FNnA9vBm6EKceENgCGRMb   # só Anitta
  python -m scripts.list_tracks --artist <ID> --top 50            # top 50 mais tocadas
  python -m scripts.list_tracks --artist <ID> --sort daily        # ordena por daily_streams
  python -m scripts.list_tracks --artist <ID> --sort date         # ordena por data da última snapshot
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from rich.box import SIMPLE
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import Database  # noqa: E402

console = Console(width=140)

ARTIST_RE = re.compile(r"(?:open\.spotify\.com/)?artist/([A-Za-z0-9]+)")

SORT_OPTIONS = {
    "playcount": "ts.playcount DESC",
    "daily":     "ts.daily_streams DESC NULLS LAST",
    "date":      "ts.snapshot_date DESC, ts.playcount DESC",
    "name":      "mt.track_name ASC",
    "album":     "mt.album_name ASC, mt.track_name ASC",
}


def parse_artist(value: str) -> str:
    v = value.strip()
    if re.fullmatch(r"[A-Za-z0-9]{22}", v):
        return v
    m = ARTIST_RE.search(v)
    if m:
        return m.group(1)
    raise ValueError(f"Artist ID inválido: {value!r}")


def fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--artist", help="Filtrar por artist_id ou URL")
    p.add_argument("--top", type=int, help="Limitar aos N primeiros (default: todos)")
    p.add_argument("--sort", choices=list(SORT_OPTIONS), default="playcount",
                   help=f"Ordenação. Opções: {list(SORT_OPTIONS)}. Default: playcount")
    p.add_argument("--min-playcount", type=int, help="Filtrar playcount >= N")
    p.add_argument("--only-primary", action="store_true", help="Só tracks onde artista é principal")
    p.add_argument("--only-features", action="store_true", help="Só features do artista")
    args = p.parse_args()

    artist_id = parse_artist(args.artist) if args.artist else None

    db = Database()
    order_by = SORT_OPTIONS[args.sort]

    params: list = []

    # Quando artist_id é passado, usa o join via artist_tracks (N:N).
    # Sem artist_id, lista todas tracks monitoradas.
    if artist_id:
        role_filter = ""
        if args.only_primary and not args.only_features:
            role_filter = " AND at.is_primary = 1"
        elif args.only_features and not args.only_primary:
            role_filter = " AND at.is_primary = 0"
        where_clauses = [f"at.artist_id = ?{role_filter}"]
        params.append(artist_id)
        if args.min_playcount:
            where_clauses.append("ts.playcount >= ?")
            params.append(args.min_playcount)
        limit_clause = f"LIMIT {int(args.top)}" if args.top else ""
        query = f"""
            SELECT
                mt.track_id,
                mt.track_name,
                mt.artist_name,
                mt.album_name,
                at.is_primary,
                ts.playcount,
                ts.daily_streams,
                ts.snapshot_date,
                ts.source
            FROM artist_tracks at
            JOIN monitored_tracks mt ON mt.track_id = at.track_id
            LEFT JOIN track_snapshots ts ON ts.track_id = mt.track_id
                AND ts.snapshot_date = (
                    SELECT MAX(snapshot_date) FROM track_snapshots WHERE track_id = mt.track_id
                )
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {order_by}
            {limit_clause}
        """
    else:
        where_clauses = ["1=1"]
        if args.min_playcount:
            where_clauses.append("ts.playcount >= ?")
            params.append(args.min_playcount)
        limit_clause = f"LIMIT {int(args.top)}" if args.top else ""
        query = f"""
            SELECT
                mt.track_id,
                mt.track_name,
                mt.artist_name,
                mt.album_name,
                1 AS is_primary,
                ts.playcount,
                ts.daily_streams,
                ts.snapshot_date,
                ts.source
            FROM monitored_tracks mt
            LEFT JOIN track_snapshots ts ON ts.track_id = mt.track_id
                AND ts.snapshot_date = (
                    SELECT MAX(snapshot_date) FROM track_snapshots WHERE track_id = mt.track_id
                )
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {order_by}
            {limit_clause}
        """

    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()

    # Resumo
    total_playcount = sum((r["playcount"] or 0) for r in rows)
    total_daily = sum((r["daily_streams"] or 0) for r in rows if r["daily_streams"] is not None)
    distinct_albums = len({r["album_name"] for r in rows if r["album_name"]})
    distinct_artists = len({r["artist_name"] for r in rows if r["artist_name"]})
    n_primary = sum(1 for r in rows if r["is_primary"] == 1)
    n_feature = sum(1 for r in rows if r["is_primary"] == 0)

    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim", justify="right")
    header.add_column(style="bold")
    header.add_row("Tracks listadas:", f"[cyan]{len(rows)}[/]")
    if artist_id:
        header.add_row("Como PRIMARY:", f"[cyan]{n_primary}[/]")
        header.add_row("Como FEATURE:", f"[yellow]{n_feature}[/]")
    header.add_row("Álbuns distintos:", str(distinct_albums))
    if not artist_id:
        header.add_row("Artistas distintos:", str(distinct_artists))
    header.add_row("Soma dos playcounts:", f"[green]{fmt_int(total_playcount)}[/]")
    if total_daily > 0:
        header.add_row("Soma daily_streams:", f"[yellow]{fmt_int(total_daily)}[/]")
    header.add_row("Ordenação:", args.sort)
    console.print(header)

    if not rows:
        console.print("\n[yellow]Nenhuma track encontrada com esses filtros.[/]")
        return 0

    # Tabela
    t = Table(box=SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    t.add_column("#", width=4, justify="right")
    t.add_column("Track", no_wrap=False, ratio=3)
    if artist_id:
        t.add_column("Role", width=5)
    if not artist_id:
        t.add_column("Artista", no_wrap=False, ratio=2)
    t.add_column("Álbum", no_wrap=False, ratio=2, style="dim")
    t.add_column("Playcount", justify="right", ratio=1)
    t.add_column("Daily", justify="right", ratio=1)
    t.add_column("Data", width=11)

    for i, r in enumerate(rows, 1):
        daily = r["daily_streams"]
        daily_display = fmt_int(daily) if daily is not None else "[dim]—[/]"
        row = [
            str(i),
            r["track_name"] or "—",
        ]
        if artist_id:
            role = "[cyan]prim[/]" if r["is_primary"] == 1 else "[yellow]feat[/]"
            row.append(role)
        if not artist_id:
            row.append(r["artist_name"] or "—")
        row.extend([
            (r["album_name"] or "")[:40],
            fmt_int(r["playcount"]),
            daily_display,
            r["snapshot_date"] or "—",
        ])
        t.add_row(*row)

    console.print(t)

    # Dica no final
    console.print()
    if artist_id:
        console.print(f"[dim]Dica: export pra Excel com filtros:[/]")
        console.print(f"  [cyan]python -m scripts.export_csv --artist {artist_id} --output data/tracks.csv[/]")
    else:
        console.print("[dim]Dica: export pra Excel com filtros:[/]")
        console.print("  [cyan]python -m scripts.export_csv --output data/tudo.csv[/]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
