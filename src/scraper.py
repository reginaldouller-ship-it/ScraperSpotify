"""Orquestrador principal do scraping."""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Optional

import httpx

from config import settings
from src.auth import SpotifyAuth
from src.db import Database
from src.embed import SpotifyEmbed, SpotifyEmbedError
from src.graphql import HashOutdatedError, SpotifyGraphQL, SpotifyGraphQLError
from src.models import ArtistSnapshot, MonitoredTrack, ScrapeResult, TrackSnapshot

logger = logging.getLogger(__name__)


class SpotifyScraper:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()
        self.http = httpx.Client(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        self.auth = SpotifyAuth(client=self.http)
        self.graphql = SpotifyGraphQL(self.auth, client=self.http)
        self.embed = SpotifyEmbed(client=self.http)
        self.result = ScrapeResult()

    def close(self) -> None:
        self.embed.close()
        self.graphql.close()
        self.auth.close()
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ---------- Ingest: adicionar tracks ----------

    def add_album(self, album_id: str) -> int:
        """
        Busca metadata do álbum e registra todas as tracks como monitoradas.
        Não grava snapshot (isso é tarefa do run_daily).
        Retorna contagem de tracks adicionadas.
        """
        album = self._fetch_album(album_id)
        artists = album.get("artists") or []
        primary_artist = artists[0] if artists else {"id": "", "name": ""}

        tracks_to_add: list[MonitoredTrack] = []
        for t in album.get("tracks", []):
            t_artists = t.get("artists") or artists
            t_primary = t_artists[0] if t_artists else primary_artist
            tracks_to_add.append(MonitoredTrack(
                track_id=t["id"],
                track_name=t.get("name", ""),
                artist_id=t_primary.get("id", ""),
                artist_name=t_primary.get("name", ""),
                album_id=album_id,
                album_name=album.get("name", ""),
            ))

        count = self.db.upsert_monitored_tracks(tracks_to_add)
        logger.info("Álbum '%s': %d tracks registradas", album.get("name", album_id), count)
        return count

    def _fetch_album(self, album_id: str) -> dict:
        """Tenta GraphQL, cai pra Embed."""
        try:
            self.result.graphql_requests += 1
            return self.graphql.get_album(album_id)
        except HashOutdatedError as e:
            logger.warning("GraphQL hash desatualizado: %s. Usando Embed.", e)
        except SpotifyGraphQLError as e:
            logger.warning("GraphQL falhou para álbum %s: %s. Tentando Embed.", album_id, e)
        except Exception as e:
            logger.warning("Erro inesperado GraphQL álbum %s: %s. Tentando Embed.", album_id, e)

        self.result.embed_requests += 1
        tracks = self.embed.get_album_tracks(album_id)
        name = ""
        artists: list[dict] = []
        if tracks:
            name = tracks[0]["album"]["name"]
            artists = tracks[0].get("artists") or []
        return {
            "id": album_id,
            "name": name,
            "artists": artists,
            "tracks": tracks,
        }

    # ---------- Run diário: snapshot ----------

    def run_daily(self, snapshot_date: Optional[date] = None) -> ScrapeResult:
        snapshot_date = snapshot_date or date.today()
        start = time.time()

        albums = self.db.list_monitored_albums()
        all_tracks = {t.track_id: t for t in self.db.list_monitored_tracks()}
        logger.info("Snapshot %s: %d álbuns monitorados, %d tracks", snapshot_date, len(albums), len(all_tracks))

        # --- tracks (agrupadas por álbum) ---
        tracks_seen: set[str] = set()
        for album_id, album_name in albums:
            try:
                album = self._fetch_album(album_id)
            except Exception as e:
                logger.error("Falha ao buscar álbum %s: %s", album_id, e)
                failed = sum(1 for t in all_tracks.values() if t.album_id == album_id)
                self.result.tracks_failed += failed
                self.result.errors.append(f"album {album_id}: {e}")
                continue

            # Backfill: se o álbum veio do GraphQL, os artistas têm ID real.
            # Atualiza monitored_tracks se o artist_id estava vazio (caso add via embed).
            album_artist_by_track: dict[str, tuple[str, str]] = {}
            for tr in album.get("tracks", []):
                t_artists = tr.get("artists") or []
                if t_artists and t_artists[0].get("id"):
                    album_artist_by_track[tr["id"]] = (t_artists[0]["id"], t_artists[0]["name"])

            for tr in album.get("tracks", []):
                tid = tr["id"]
                if tid not in all_tracks:
                    continue
                mt = all_tracks[tid]
                source = tr.get("source", "graphql")
                playcount = tr.get("playcount")

                if playcount is None:
                    # Embed fallback para metadata: não há playcount disponível → pula.
                    logger.warning("Track %s sem playcount (source=%s) — snapshot pulado", tid, source)
                    self.result.tracks_failed += 1
                    self.result.errors.append(f"track {tid}: sem playcount (source={source})")
                    continue

                # Se temos artist_id real do GraphQL e não tínhamos antes, backfill
                artist_id = mt.artist_id
                artist_name = mt.artist_name
                if tid in album_artist_by_track:
                    new_id, new_name = album_artist_by_track[tid]
                    if not artist_id or artist_id != new_id:
                        artist_id, artist_name = new_id, new_name
                        self.db.upsert_monitored_track(MonitoredTrack(
                            track_id=tid,
                            track_name=tr.get("name") or mt.track_name,
                            artist_id=artist_id,
                            artist_name=artist_name,
                            album_id=mt.album_id or album.get("id", album_id),
                            album_name=mt.album_name or album.get("name", album_name),
                        ))

                snap = TrackSnapshot(
                    track_id=tid,
                    track_name=tr.get("name") or mt.track_name,
                    artist_id=artist_id,
                    artist_name=artist_name,
                    album_id=mt.album_id or album.get("id", album_id),
                    album_name=mt.album_name or album.get("name", album_name),
                    playcount=int(playcount),
                    snapshot_date=snapshot_date,
                    source=source,
                    duration_ms=tr.get("duration_ms"),
                    explicit=tr.get("explicit"),
                )
                try:
                    self.db.upsert_track_snapshot(snap)
                    tracks_seen.add(tid)
                    self.result.tracks_success += 1
                except Exception as e:
                    logger.error("Falha salvando snapshot da track %s: %s", tid, e)
                    self.result.tracks_failed += 1
                    self.result.errors.append(f"track {tid}: {e}")

        # Tracks monitoradas que não apareceram em nenhum álbum
        missing = [t for tid, t in all_tracks.items() if tid not in tracks_seen]
        for mt in missing:
            logger.warning("Track %s (%s) não foi encontrada no álbum %s — snapshot pulado",
                           mt.track_id, mt.track_name, mt.album_id or "(sem álbum)")
            self.result.tracks_failed += 1
            self.result.errors.append(f"track {mt.track_id}: não encontrada em nenhum álbum monitorado")

        self.result.tracks_processed = self.result.tracks_success + self.result.tracks_failed

        # --- artistas ---
        artists = self.db.distinct_artist_ids_from_tracks()
        logger.info("Buscando overview de %d artistas distintos", len(artists))
        for artist_id, artist_name in artists:
            if not artist_id:
                continue
            try:
                self.result.graphql_requests += 1
                overview = self.graphql.get_artist_overview(artist_id)
                snap = ArtistSnapshot(
                    artist_id=artist_id,
                    artist_name=overview.get("name") or artist_name,
                    monthly_listeners=overview.get("monthly_listeners"),
                    followers=overview.get("followers"),
                    world_rank=overview.get("world_rank"),
                    popularity=overview.get("popularity"),
                    top_cities_json=json.dumps(overview.get("top_cities") or [], ensure_ascii=False),
                    biography=overview.get("biography"),
                    snapshot_date=snapshot_date,
                )
                self.db.upsert_artist_snapshot(snap)
                self.result.artists_success += 1
            except HashOutdatedError as e:
                logger.warning("queryArtistOverview hash desatualizado para %s: %s", artist_id, e)
                self.result.artists_failed += 1
                self.result.errors.append(f"artist {artist_id}: hash outdated")
            except Exception as e:
                logger.error("Falha overview artista %s: %s", artist_id, e)
                self.result.artists_failed += 1
                self.result.errors.append(f"artist {artist_id}: {e}")

        self.result.artists_processed = self.result.artists_success + self.result.artists_failed
        self.result.duration_seconds = time.time() - start
        return self.result
