"""
Teste integrado dos novos métodos GraphQL + credits + track_versions.

Exercita todos contra Samuel Messias (artist_id = 5cFlGTfDoYwRGZrtEO92MJ)
que já está no DB desde o MVP.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

# Forçar UTF-8 em stdout no Windows para não quebrar em caracteres como ⭐
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import httpx
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from src.auth import SpotifyAuth  # noqa: E402
from src.credits import SpotifyCredits  # noqa: E402
from src.graphql import SpotifyGraphQL  # noqa: E402
from src.track_versions import TrackVariant, find_duplicates, normalize_title  # noqa: E402

console = Console()

ARTIST_ID = "5cFlGTfDoYwRGZrtEO92MJ"  # Samuel Messias
TRACK_ID = "2T7eQvDCMB3u0nuNNm13yw"  # "A Glória desta Última Casa (Ao Vivo)"


def hr(title: str):
    console.print()
    console.rule(f"[bold cyan]{title}[/]")


def main() -> int:
    http = httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
    auth = SpotifyAuth(client=http)
    gql = SpotifyGraphQL(auth, client=http)
    creds = SpotifyCredits(auth, client=http)

    try:
        # ---------- Teste 1: get_track ----------
        hr("TESTE 1 - get_track")
        tr = gql.get_track(TRACK_ID)
        console.print(f"[green]OK[/] name={tr['name']!r}")
        console.print(f"  playcount = {tr['playcount']:,}")
        console.print(f"  duration_ms = {tr['duration_ms']}")
        console.print(f"  artists = {tr['artists']}")
        console.print(f"  album = {tr['album']}")

        # ---------- Teste 2: queryArtistDiscographyAll ----------
        hr("TESTE 2 - get_artist_discography_all")
        disco = gql.get_artist_discography_all(ARTIST_ID, limit=50)
        console.print(f"[green]OK[/] artist={disco['name']!r}, releases={len(disco['releases'])}")
        # agrupa por tipo
        by_type: dict[str, int] = {}
        for r in disco["releases"]:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        console.print(f"  por tipo: {by_type}")
        console.print("  primeiros 5 releases:")
        for r in disco["releases"][:5]:
            console.print(f"    - [{r['type']:11}] {r['name']} ({r.get('total_tracks','?')} tracks, {r.get('release_date','?')})")

        # ---------- Teste 3: queryArtistDiscoveredOn ----------
        hr("TESTE 3 - get_artist_discovered_on")
        disc_on = gql.get_artist_discovered_on(ARTIST_ID)
        console.print(f"[green]OK[/] artist={disc_on['name']!r}, playlists={len(disc_on['playlists'])}")
        t = Table(show_header=True, header_style="bold")
        t.add_column("#")
        t.add_column("Playlist")
        t.add_column("Owner")
        t.add_column("ID")
        for i, p in enumerate(disc_on["playlists"][:15], 1):
            t.add_row(str(i), p["name"][:50], p["owner"]["name"][:30], p["id"])
        console.print(t)

        # ---------- Teste 4: queryArtistRelated ----------
        hr("TESTE 4 - get_artist_related (bonus)")
        rel = gql.get_artist_related(ARTIST_ID)
        console.print(f"[green]OK[/] artist={rel['name']!r}, related={len(rel['related'])}")
        for r in rel["related"][:10]:
            console.print(f"    - {r['name']} ({r['id']})")

        # ---------- Teste 5: Credits ----------
        hr("TESTE 5 - track credits (REST)")
        c = creds.get_track_credits(TRACK_ID)
        console.print(f"  track_title = {c['track_title']!r}")
        console.print(f"  has_data    = {c['has_data']}")
        console.print(f"  performers  = {len(c['performers'])}")
        console.print(f"  writers     = {len(c['writers'])}")
        console.print(f"  producers   = {len(c['producers'])}")
        console.print(f"  extended    = {len(c['extended'])}")
        console.print(f"  source_names= {c['source_names']}")
        if not c["has_data"]:
            console.print("  [yellow]Credits vazios (esperado para artista menor).[/]")
            console.print("  [dim]Testando com track famoso para validar parser:[/]")
            # Pink Floyd - Time (Dark Side of the Moon) — costuma ter credits
            famous = "3TO7bbrUKrOSPGRTB5MeCz"  # Time (2011 Remaster)
            try:
                c2 = creds.get_track_credits(famous)
                console.print(f"  [dim]Famous track '{c2['track_title']}': has_data={c2['has_data']}, "
                              f"performers={len(c2['performers'])}, writers={len(c2['writers'])}[/]")
                if c2["performers"]:
                    console.print(f"  [dim]Exemplo performer:[/] {c2['performers'][0]}")
            except Exception as e:
                console.print(f"  [red]Famous track falhou: {e}[/]")

        # ---------- Teste 6: track_versions ----------
        hr("TESTE 6 - track_versions (deduplicação)")
        # Teste 6a: normalizar títulos
        samples = [
            "Ainda Tem Promessa (Ao Vivo)",
            "Ainda Tem Promessa",
            "Despacito - Remix",
            "Despacito (feat. Justin Bieber) - Remix",
            "Bohemian Rhapsody - Remastered 2011",
            "Time - 2011 Remaster",
        ]
        t = Table(title="normalize_title")
        t.add_column("Original"); t.add_column("Normalizado")
        for s in samples:
            t.add_row(s, normalize_title(s))
        console.print(t)

        # Teste 6b: agrupar variantes fictícias
        variants = [
            TrackVariant(track_id="A", track_name="Ainda Tem Promessa", album_id="al1", album_name="Single", album_type="single", duration_ms=420_000, playcount=50_000),
            TrackVariant(track_id="B", track_name="Ainda Tem Promessa (Ao Vivo)", album_id="al2", album_name="Ao Vivo Album", album_type="album", duration_ms=436_320, playcount=96_387),
            TrackVariant(track_id="C", track_name="Jeová Jireh (Ao Vivo)", album_id="al2", album_name="Ao Vivo Album", album_type="album", duration_ms=300_000, playcount=569_665),
            TrackVariant(track_id="D", track_name="Jeová Jireh", album_id="al3", album_name="Studio EP", album_type="single", duration_ms=295_000, playcount=1_000_000),
        ]
        dups = find_duplicates(variants)
        console.print(f"\nDuplicatas detectadas: {len(dups)}")
        for d in dups:
            console.print(f"  [bold]{d['normalized_title']}[/]:")
            console.print(f"    canonical → {d['canonical'].track_id} ({d['canonical'].track_name}, {d['canonical'].playcount:,} plays)")
            for dup in d["duplicates"]:
                console.print(f"    dup       → {dup.track_id} ({dup.track_name}, {dup.playcount:,} plays)")

        hr("RESUMO")
        console.print("[green][OK][/] Todos os testes rodaram sem exceções.")
        console.print("Próximo passo: integrar no scraper e criar tabelas de snapshot no DB.")

    finally:
        creds.close()
        gql.close()
        auth.close()
        http.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
