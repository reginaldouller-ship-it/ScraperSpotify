"""
Executar snapshot diário.

Uso:
  python -m scripts.run_daily
  python -m scripts.run_daily --status    # só mostra status do DB
  python -m scripts.run_daily --date 2026-04-15
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scraper import SpotifyScraper  # noqa: E402

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=False, markup=True, show_path=False)],
)
logger = logging.getLogger("run_daily")


def print_status(status: dict) -> None:
    table = Table(title="DB Status", show_header=True, header_style="bold cyan")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    for k, v in status.items():
        table.add_row(k, str(v))
    console.print(table)


def main() -> int:
    p = argparse.ArgumentParser(description="Snapshot diário do Spotify")
    p.add_argument("--status", action="store_true", help="Só mostra status e sai")
    p.add_argument("--date", help="Data do snapshot (YYYY-MM-DD). Default: hoje")
    args = p.parse_args()

    snapshot_date = date.today()
    if args.date:
        snapshot_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    with SpotifyScraper() as scraper:
        if args.status:
            print_status(scraper.db.status())
            return 0

        console.print(f"[bold cyan]Iniciando snapshot[/] de [yellow]{snapshot_date}[/]")
        status_before = scraper.db.status()
        if status_before["monitored_tracks"] == 0:
            console.print("[red]Nenhuma track monitorada.[/] Adicione com: [green]python -m scripts.add_tracks --album <URL>[/]")
            return 1

        result = scraper.run_daily(snapshot_date=snapshot_date)

        # Relatório
        table = Table(title="Resultado do snapshot", show_header=True, header_style="bold green")
        table.add_column("Métrica")
        table.add_column("Valor", justify="right")
        table.add_row("Tracks processadas", str(result.tracks_processed))
        table.add_row("Tracks sucesso", str(result.tracks_success))
        table.add_row("Tracks falharam", str(result.tracks_failed))
        table.add_row("Artistas processados", str(result.artists_processed))
        table.add_row("Artistas sucesso", str(result.artists_success))
        table.add_row("Artistas falharam", str(result.artists_failed))
        table.add_row("GraphQL requests", str(result.graphql_requests))
        table.add_row("Embed requests", str(result.embed_requests))
        table.add_row("Rate limit hits", str(result.rate_limit_hits))
        table.add_row("Duração (s)", f"{result.duration_seconds:.1f}")
        console.print(table)

        if result.errors:
            console.print(f"\n[red]{len(result.errors)} erros:[/]")
            for err in result.errors[:20]:
                console.print(f"  • {err}")
            if len(result.errors) > 20:
                console.print(f"  [dim]... e mais {len(result.errors) - 20}[/]")

        print_status(scraper.db.status())
        return 0 if result.tracks_failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
