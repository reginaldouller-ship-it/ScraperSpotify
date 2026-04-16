"""
Exportar daily streams para CSV.

Uso:
  python -m scripts.export_csv --output data/tudo.csv
  python -m scripts.export_csv --artist 7FNnA9vBm6EKceENgCGRMb --output data/anitta.csv
  python -m scripts.export_csv --from 2026-04-01 --to 2026-04-15 --output data/abril.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import Database  # noqa: E402

console = Console()

COLUMNS = [
    "snapshot_date",
    "track_id",
    "track_name",
    "artist_name",
    "primary_artist_id",
    "role",
    "album_name",
    "total_streams",
    "daily_streams",
    "daily_change_pct",
    "monthly_listeners",
    "world_rank",
    "source",
]

ARTIST_RE = re.compile(r"(?:open\.spotify\.com/)?artist/([A-Za-z0-9]+)")


def parse_artist(value: str) -> str:
    v = value.strip()
    if re.fullmatch(r"[A-Za-z0-9]{22}", v):
        return v
    m = ARTIST_RE.search(v)
    if m:
        return m.group(1)
    raise ValueError(f"Artist ID inválido: {value!r}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from", help="Data inicial (YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", help="Data final (YYYY-MM-DD)")
    p.add_argument("--artist", help="Filtrar por artist_id ou URL")
    p.add_argument("--only-primary", action="store_true", help="Só tracks onde artista é principal")
    p.add_argument("--only-features", action="store_true", help="Só features do artista")
    p.add_argument("--output", required=True, help="Arquivo CSV de saída")
    args = p.parse_args()

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else None
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else None
    artist_id = parse_artist(args.artist) if args.artist else None

    db = Database()
    rows = db.export_daily_streams(
        date_from=date_from,
        date_to=date_to,
        artist_id=artist_id,
        only_primary=args.only_primary,
        only_features=args.only_features,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            row = {c: r[c] if c in r.keys() else "" for c in COLUMNS}
            # Campo "role" é derivado de role_primary
            if "role_primary" in r.keys():
                row["role"] = "primary" if r["role_primary"] == 1 else "feature"
            else:
                row["role"] = "primary"
            writer.writerow(row)

    filt = []
    if artist_id:
        filt.append(f"artist_id={artist_id}")
    if date_from:
        filt.append(f"from={date_from}")
    if date_to:
        filt.append(f"to={date_to}")
    filt_str = f" (filtros: {', '.join(filt)})" if filt else ""
    console.print(f"[green]OK[/] {len(rows)} linhas exportadas para [yellow]{out}[/]{filt_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
