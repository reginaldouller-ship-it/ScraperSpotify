"""
Report completo de um artista: GraphQL ao vivo + snapshots do DB local.

Uso:
  python -m scripts.artist_report                        # Samuel Messias default
  python -m scripts.artist_report --artist <URL_ou_ID>
  python -m scripts.artist_report --artist <ID> --json   # também salva JSON em data/
  python -m scripts.artist_report --no-related           # pula seção pesada (1 request)
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Força UTF-8 no Windows pra não explodir em emojis
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import httpx
from rich.box import HEAVY, ROUNDED, SIMPLE
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.auth import SpotifyAuth  # noqa: E402
from src.db import Database  # noqa: E402
from src.graphql import SpotifyGraphQL  # noqa: E402

console = Console(width=130)

DEFAULT_ARTIST = "5cFlGTfDoYwRGZrtEO92MJ"  # Samuel Messias


ARTIST_URL_RE = re.compile(r"(?:open\.spotify\.com/)?artist/([A-Za-z0-9]+)")


def extract_artist_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]{22}", value.strip()):
        return value.strip()
    m = ARTIST_URL_RE.search(value)
    if m:
        return m.group(1)
    raise ValueError(f"Não consegui extrair artist_id de: {value!r}")


def fmt_int(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", ".")


def fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%Y-%m-%d")
    except Exception:
        return str(iso)[:10]


# ============================================================
# Seções do report
# ============================================================

def section_profile(overview: dict) -> Panel:
    """Card compacto com métricas-chave."""
    name = overview.get("name") or "(artista)"
    monthly = fmt_int(overview.get("monthly_listeners"))
    followers = fmt_int(overview.get("followers"))
    world_rank = overview.get("world_rank")
    rank_str = f"#{world_rank:,}".replace(",", ".") if world_rank else "—"
    popularity = overview.get("popularity")
    pop_str = f"{popularity}/100" if popularity is not None else "—"

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="dim")
    t.add_column(justify="left", style="bold")
    t.add_row("Monthly listeners:", f"[green]{monthly}[/]")
    t.add_row("Followers:", f"[cyan]{followers}[/]")
    t.add_row("World rank:", f"[magenta]{rank_str}[/]")
    t.add_row("Popularity:", f"[yellow]{pop_str}[/]")

    return Panel(
        t,
        title=f"[bold white on blue] {name} [/]",
        border_style="blue",
        box=HEAVY,
        padding=(1, 2),
    )


def section_top_cities(overview: dict, top_n: int = 10) -> Panel:
    cities = overview.get("top_cities") or []
    if not cities:
        return Panel("[dim]Sem dados de top cities[/]", title="Top cities", border_style="dim")

    max_lis = max((c.get("listeners") or 0) for c in cities) or 1

    t = Table(box=SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    t.add_column("#", width=3, justify="right")
    t.add_column("Cidade", no_wrap=True)
    t.add_column("País", width=5)
    t.add_column("Listeners", justify="right")
    t.add_column("", width=30)  # bar

    for i, c in enumerate(cities[:top_n], 1):
        lis = c.get("listeners") or 0
        bar_len = int(25 * lis / max_lis)
        bar = "█" * bar_len + "░" * (25 - bar_len)
        t.add_row(
            str(i),
            c.get("city") or "—",
            c.get("country") or "—",
            fmt_int(lis),
            f"[green]{bar}[/]",
        )

    return Panel(t, title=f"Top {min(top_n, len(cities))} cidades", border_style="green", box=ROUNDED)


def section_discography(disco: dict, top_n: int = 12) -> Panel:
    releases = disco.get("releases") or []
    total = disco.get("total_count") or len(releases)

    # agregados
    from collections import Counter
    by_type = Counter(r.get("type", "?") for r in releases)

    summary_t = Table.grid(padding=(0, 2))
    summary_t.add_column(justify="right", style="dim")
    summary_t.add_column(style="bold")
    summary_t.add_row("Total:", f"[bold]{total}[/] releases")
    for rtype in ("album", "ep", "single", "compilation", "appears_on"):
        if rtype in by_type:
            summary_t.add_row(f"  {rtype}:", str(by_type[rtype]))

    # tabela dos mais recentes
    t = Table(box=SIMPLE, show_header=True, header_style="bold cyan")
    t.add_column("Tipo", width=12)
    t.add_column("Release", no_wrap=False)
    t.add_column("Tracks", justify="right", width=7)
    t.add_column("Lançamento", width=12)

    # releases já vêm ordenados por data desc no response do Spotify
    for r in releases[:top_n]:
        rtype = r.get("type", "?")
        rtype_colored = {
            "album": "[bold magenta]album[/]",
            "ep": "[bold yellow]ep[/]",
            "single": "[cyan]single[/]",
            "compilation": "[dim]comp[/]",
            "appears_on": "[dim]feat[/]",
        }.get(rtype, rtype)
        t.add_row(
            rtype_colored,
            r.get("name") or "—",
            str(r.get("total_tracks") or "—"),
            fmt_date(r.get("release_date")),
        )

    body = Group(summary_t, Rule(style="dim"), t)
    return Panel(body, title=f"Discografia (top {min(top_n, len(releases))} mais recentes)", border_style="magenta", box=ROUNDED)


def section_discovered_on(disc_on: dict, top_n: int = 20) -> Panel:
    playlists = disc_on.get("playlists") or []
    if not playlists:
        return Panel("[dim]Sem playlists no 'Descoberto em'[/]", title="Descoberto em", border_style="dim")

    t = Table(box=SIMPLE, show_header=True, header_style="bold cyan")
    t.add_column("#", width=3, justify="right")
    t.add_column("Playlist")
    t.add_column("Curador")
    t.add_column("Playlist ID", style="dim")

    for i, p in enumerate(playlists[:top_n], 1):
        owner = p.get("owner") or {}
        owner_name = owner.get("name") or "—"
        # destaca editoriais oficiais do Spotify
        if owner_name.lower() == "spotify":
            owner_display = f"[bold green]{owner_name}[/] ⭐"
        else:
            owner_display = owner_name
        t.add_row(
            str(i),
            p.get("name") or "—",
            owner_display,
            p.get("id") or "—",
        )

    return Panel(
        t,
        title=f"Descoberto em ({len(playlists)} playlists — top {min(top_n, len(playlists))})",
        border_style="yellow",
        box=ROUNDED,
    )


def section_related(rel: dict, top_n: int = 12) -> Panel:
    artists = rel.get("related") or []
    if not artists:
        return Panel("[dim]Sem artistas relacionados[/]", title="Artistas relacionados", border_style="dim")

    # grid com 3 colunas
    cols = []
    for a in artists[:top_n]:
        name = a.get("name") or "—"
        aid = a.get("id") or ""
        cols.append(Panel(
            f"[bold]{name}[/]\n[dim]{aid}[/]",
            padding=(0, 1),
            border_style="cyan",
        ))

    return Panel(
        Columns(cols, equal=True, expand=True),
        title=f"Artistas relacionados ({len(artists)} total — top {min(top_n, len(artists))})",
        border_style="cyan",
        box=ROUNDED,
    )


def section_biography(overview: dict) -> Optional[Panel]:
    bio = overview.get("biography")
    if not bio:
        return None
    # remove tags HTML básicas (bio costuma vir como texto puro mas por via das dúvidas)
    clean = re.sub(r"<[^>]+>", "", bio).strip()
    if not clean:
        return None
    # corta em ~800 chars
    if len(clean) > 800:
        clean = clean[:800].rsplit(" ", 1)[0] + " [dim]…[/]"
    return Panel(clean, title="Biografia", border_style="white", box=ROUNDED)


def section_db_snapshots(db: Database, artist_id: str) -> Panel:
    """Dados do DB local: tracks monitoradas + últimos snapshots."""
    with db.connect() as conn:
        tracks = conn.execute("""
            SELECT mt.track_id, mt.track_name, mt.album_name,
                   ts.playcount, ts.daily_streams, ts.snapshot_date
            FROM monitored_tracks mt
            LEFT JOIN track_snapshots ts ON ts.track_id = mt.track_id
              AND ts.snapshot_date = (
                SELECT MAX(snapshot_date) FROM track_snapshots WHERE track_id = mt.track_id
              )
            WHERE mt.artist_id = ?
            ORDER BY ts.playcount DESC NULLS LAST
        """, (artist_id,)).fetchall()

        artist_snap = conn.execute("""
            SELECT monthly_listeners, followers, world_rank, snapshot_date
            FROM artist_snapshots
            WHERE artist_id = ?
            ORDER BY snapshot_date DESC LIMIT 1
        """, (artist_id,)).fetchone()

    if not tracks and not artist_snap:
        return Panel(
            "[dim]Nenhum dado local ainda. Rode:[/]\n"
            "  [cyan]python -m scripts.add_tracks --album <URL>[/]\n"
            "  [cyan]python -m scripts.run_daily[/]",
            title="DB local",
            border_style="dim",
        )

    parts = []

    if artist_snap:
        header = Table.grid(padding=(0, 2))
        header.add_column(justify="right", style="dim")
        header.add_column(style="bold")
        header.add_row("Último snapshot:", f"[yellow]{artist_snap['snapshot_date']}[/]")
        header.add_row("Monthly listeners (histórico):", f"[green]{fmt_int(artist_snap['monthly_listeners'])}[/]")
        header.add_row("Followers (histórico):", f"[cyan]{fmt_int(artist_snap['followers'])}[/]")
        header.add_row("World rank (histórico):", str(artist_snap["world_rank"]) if artist_snap["world_rank"] else "—")
        parts.append(header)

    if tracks:
        t = Table(box=SIMPLE, show_header=True, header_style="bold cyan")
        t.add_column("#", width=3, justify="right")
        t.add_column("Track")
        t.add_column("Álbum", style="dim")
        t.add_column("Playcount", justify="right")
        t.add_column("Daily", justify="right")
        t.add_column("Snapshot", width=12)
        for i, r in enumerate(tracks, 1):
            daily = r["daily_streams"]
            daily_str = fmt_int(daily) if daily is not None else "[dim]—[/]"
            t.add_row(
                str(i),
                r["track_name"] or "—",
                (r["album_name"] or "")[:35],
                fmt_int(r["playcount"]),
                daily_str,
                r["snapshot_date"] or "—",
            )
        if parts:
            parts.extend([Rule(style="dim"), t])
        else:
            parts.append(t)

    return Panel(Group(*parts), title="DB local (snapshots)", border_style="green", box=ROUNDED)


# ============================================================
# Main
# ============================================================

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--artist", default=DEFAULT_ARTIST, help="URL ou ID do artista")
    p.add_argument("--json", action="store_true", help="salvar JSON bruto em data/")
    p.add_argument("--no-related", action="store_true", help="pular query de artistas relacionados")
    p.add_argument("--no-disco", action="store_true", help="pular discografia (query paginada mais pesada)")
    args = p.parse_args()

    artist_id = extract_artist_id(args.artist)
    console.rule(f"[bold cyan]Coletando dados do artista {artist_id}…[/]")

    http = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
    auth = SpotifyAuth(client=http)
    gql = SpotifyGraphQL(auth, client=http)
    db = Database()

    raw: dict[str, Any] = {"artist_id": artist_id}

    try:
        # 1. overview (headline)
        console.print("[dim]1/4[/] queryArtistOverview…")
        overview = gql.get_artist_overview(artist_id)
        raw["overview"] = overview

        # 2. discografia (paginada)
        disco: dict[str, Any] = {}
        if not args.no_disco:
            console.print("[dim]2/4[/] queryArtistDiscographyAll…")
            disco = gql.get_artist_discography_all(artist_id, limit=50)
            raw["discography"] = disco

        # 3. discovered on
        console.print("[dim]3/4[/] queryArtistDiscoveredOn…")
        disc_on = gql.get_artist_discovered_on(artist_id)
        raw["discovered_on"] = disc_on

        # 4. related (opcional)
        rel: dict[str, Any] = {}
        if not args.no_related:
            console.print("[dim]4/4[/] queryArtistRelated…")
            rel = gql.get_artist_related(artist_id)
            raw["related"] = rel

    finally:
        gql.close()
        auth.close()
        http.close()

    # ====== Render ======
    console.print()
    console.print(section_profile(overview))

    if overview.get("top_cities"):
        console.print(section_top_cities(overview))

    if disco:
        console.print(section_discography(disco))

    console.print(section_discovered_on(disc_on))

    if rel:
        console.print(section_related(rel))

    bio_panel = section_biography(overview)
    if bio_panel:
        console.print(bio_panel)

    console.print(section_db_snapshots(db, artist_id))

    # JSON dump
    if args.json:
        out = Path("data") / f"artist_report_{artist_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"\n[green][OK][/] JSON salvo em [cyan]{out}[/]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
