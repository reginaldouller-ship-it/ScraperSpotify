"""Client para a Partner API GraphQL do Spotify."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from src.auth import SpotifyAuth

logger = logging.getLogger(__name__)


class SpotifyGraphQLError(Exception):
    pass


class HashOutdatedError(SpotifyGraphQLError):
    """sha256Hash da persisted query está desatualizado (HTTP 400 PersistedQueryNotFound)."""


class SpotifyGraphQL:
    """
    Client para https://api-partner.spotify.com/pathfinder/v1/query

    Expõe:
      - get_album(album_id) -> dict com tracks e playcount
      - get_artist_overview(artist_id) -> dict com monthlyListeners, followers, worldRank
    """

    def __init__(self, auth: SpotifyAuth, client: Optional[httpx.Client] = None):
        self._auth = auth
        self._client = client or httpx.Client(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
            http2=False,  # httpx[http2] é opcional
        )
        self._owns_client = client is None
        self._consecutive_429 = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": random.choice(settings.USER_AGENTS),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "App-Platform": "WebPlayer",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
            "Spotify-App-Version": "1.2.52.442",
        }

    def _delay(self) -> None:
        time.sleep(random.uniform(settings.GRAPHQL_DELAY_MIN, settings.GRAPHQL_DELAY_MAX))

    def _build_url(self, operation_name: str, variables: dict, sha256_hash: str) -> str:
        params = {
            "operationName": operation_name,
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}},
                separators=(",", ":"),
            ),
        }
        return f"{settings.GRAPHQL_URL}?{urlencode(params)}"

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.BACKOFF_FACTOR, min=1, max=60),
        retry=retry_if_exception_type((httpx.TransportError,)),
        reraise=True,
    )
    def _query(self, operation_name: str, variables: dict) -> dict:
        sha = settings.GRAPHQL_HASHES.get(operation_name)
        if not sha:
            raise SpotifyGraphQLError(f"sha256Hash desconhecido para operação '{operation_name}'")

        self._delay()
        token = self._auth.get_token()
        url = self._build_url(operation_name, variables, sha)
        resp = self._client.get(url, headers=self._headers(token))

        if resp.status_code == 401:
            logger.warning("401 Unauthorized — invalidando token e renovando")
            self._auth.invalidate()
            token = self._auth.get_token(force_refresh=True)
            resp = self._client.get(url, headers=self._headers(token))

        if resp.status_code == 429:
            self._consecutive_429 += 1
            retry_after = int(resp.headers.get("Retry-After", "30"))
            logger.warning("429 rate limited. Pausando %ds (consecutive=%d)", retry_after, self._consecutive_429)
            if self._consecutive_429 >= settings.CONSECUTIVE_429_THRESHOLD:
                logger.error(
                    "Atingido threshold de 429 consecutivos — pausando %ds",
                    settings.RATE_LIMIT_PAUSE_SECONDS,
                )
                time.sleep(settings.RATE_LIMIT_PAUSE_SECONDS)
                self._consecutive_429 = 0
            else:
                time.sleep(retry_after)
            raise SpotifyGraphQLError("429 Rate Limited")

        if resp.status_code == 400:
            body = resp.text[:500]
            if "PersistedQueryNotFound" in body or "persistedQueryNotFound" in body:
                raise HashOutdatedError(
                    f"sha256Hash desatualizado para {operation_name}. Response: {body}"
                )
            raise SpotifyGraphQLError(f"400 Bad Request em {operation_name}: {body}")

        if resp.status_code >= 500:
            raise SpotifyGraphQLError(f"{resp.status_code} servidor: {resp.text[:200]}")

        resp.raise_for_status()
        self._consecutive_429 = 0

        data = resp.json()
        if "errors" in data and data["errors"]:
            raise SpotifyGraphQLError(f"GraphQL errors: {data['errors']}")

        return data.get("data", {})

    # --------- operations ---------

    def get_album(self, album_id: str, limit: int = 300) -> dict:
        """
        Retorna detalhes do álbum incluindo tracks com playcount.

        Output shape (normalizado):
        {
            "id": str,
            "name": str,
            "artists": [{"id": str, "name": str}, ...],
            "tracks": [
                {"id": str, "name": str, "playcount": int, "duration_ms": int,
                 "disc_number": int, "track_number": int, "explicit": bool,
                 "artists": [...]},
                ...
            ],
        }
        """
        variables = {
            "uri": f"spotify:album:{album_id}",
            "locale": "",
            "offset": 0,
            "limit": limit,
        }
        data = self._query("getAlbum", variables)
        album = data.get("albumUnion") or data.get("album") or {}
        if not album:
            raise SpotifyGraphQLError(f"albumUnion vazio para {album_id}: {data}")

        name = album.get("name", "")
        artists_raw = (album.get("artists") or {}).get("items") or []
        artists = [
            {"id": _uri_id(a.get("uri", "")), "name": (a.get("profile") or {}).get("name", "")}
            for a in artists_raw
        ]

        tracks_container = album.get("tracks") or album.get("tracksV2") or {}
        items = tracks_container.get("items") or []

        tracks: list[dict] = []
        for it in items:
            # estrutura: { "track": {...} } em alguns esquemas, ou direto
            tr = it.get("track") if isinstance(it.get("track"), dict) else it
            if not tr:
                continue
            track_id = _uri_id(tr.get("uri", ""))
            if not track_id:
                continue

            try:
                playcount = int(tr.get("playcount") or 0)
            except (TypeError, ValueError):
                playcount = 0

            duration_ms = None
            duration = tr.get("duration")
            if isinstance(duration, dict):
                duration_ms = duration.get("totalMilliseconds") or duration.get("total_ms")
            elif isinstance(duration, (int, float)):
                duration_ms = int(duration)

            t_artists_raw = (tr.get("artists") or {}).get("items") or []
            t_artists = [
                {"id": _uri_id(a.get("uri", "")), "name": (a.get("profile") or {}).get("name", "")}
                for a in t_artists_raw
            ] or artists

            tracks.append({
                "id": track_id,
                "name": tr.get("name", ""),
                "playcount": playcount,
                "duration_ms": duration_ms,
                "disc_number": tr.get("discNumber"),
                "track_number": tr.get("trackNumber"),
                "explicit": (tr.get("contentRating") or {}).get("label", "").upper() == "EXPLICIT"
                    if isinstance(tr.get("contentRating"), dict)
                    else tr.get("explicit"),
                "artists": t_artists,
                "source": "graphql",
            })

        return {
            "id": album_id,
            "name": name,
            "artists": artists,
            "tracks": tracks,
        }

    def get_artist_overview(self, artist_id: str) -> dict:
        """
        Retorna overview do artista.
        {
            "id": str,
            "name": str,
            "monthly_listeners": int,
            "followers": int,
            "world_rank": int | None,
            "top_cities": [{"city": str, "country": str, "listeners": int}, ...],
            "biography": str,
            "popularity": int | None,
        }
        """
        variables = {
            "uri": f"spotify:artist:{artist_id}",
            "locale": "",
            "includePrerelease": True,
        }
        data = self._query("queryArtistOverview", variables)
        artist = data.get("artistUnion") or data.get("artist") or {}
        if not artist:
            raise SpotifyGraphQLError(f"artistUnion vazio para {artist_id}: {data}")

        profile = artist.get("profile") or {}
        stats = artist.get("stats") or {}
        visuals = artist.get("visuals") or {}

        # top cities
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
            "name": profile.get("name", ""),
            "monthly_listeners": stats.get("monthlyListeners"),
            "followers": stats.get("followers"),
            "world_rank": stats.get("worldRank"),
            "top_cities": top_cities,
            "biography": (profile.get("biography") or {}).get("text"),
            "popularity": artist.get("popularity"),
            "source": "graphql",
        }

    def get_track(self, track_id: str) -> dict:
        """
        Retorna detalhes de uma track individual, incluindo playcount.
        Útil quando você quer só 1 track sem baixar o álbum inteiro.

        {
            "id": str,
            "name": str,
            "playcount": int,
            "duration_ms": int,
            "track_number": int,
            "explicit": bool,
            "album": {"id": str, "name": str},
            "artists": [{"id": str, "name": str}, ...],
        }
        """
        variables = {"uri": f"spotify:track:{track_id}"}
        data = self._query("getTrack", variables)
        tr = data.get("trackUnion") or data.get("track") or {}
        if not tr:
            raise SpotifyGraphQLError(f"trackUnion vazio para {track_id}: {data}")

        try:
            playcount = int(tr.get("playcount") or 0)
        except (TypeError, ValueError):
            playcount = 0

        duration_ms = None
        duration = tr.get("duration")
        if isinstance(duration, dict):
            duration_ms = duration.get("totalMilliseconds")
        elif isinstance(duration, (int, float)):
            duration_ms = int(duration)

        # artists: firstArtist + otherArtists
        first = tr.get("firstArtist") or {}
        first_items = first.get("items") or ([first] if first.get("uri") else [])
        other = (tr.get("otherArtists") or {}).get("items") or []
        artists_raw = first_items + other
        artists = [
            {"id": _uri_id(a.get("uri", "")), "name": (a.get("profile") or {}).get("name", "")}
            for a in artists_raw
        ]

        # album
        album_of = tr.get("albumOfTrack") or {}
        album = {
            "id": _uri_id(album_of.get("uri", "")),
            "name": album_of.get("name", ""),
        }

        content_rating = tr.get("contentRating") or {}
        explicit = (
            content_rating.get("label", "").upper() == "EXPLICIT"
            if isinstance(content_rating, dict)
            else None
        )

        return {
            "id": track_id,
            "name": tr.get("name", ""),
            "playcount": playcount,
            "duration_ms": duration_ms,
            "track_number": tr.get("trackNumber"),
            "explicit": explicit,
            "album": album,
            "artists": artists,
            "source": "graphql",
        }

    def get_artist_discography_all(self, artist_id: str, limit: int = 50) -> dict:
        """
        Retorna a discografia completa de um artista (albums + singles + compilations + appears_on).
        A estrutura do Spotify tem várias sub-seções; aqui agregamos tudo em `releases`.

        Benefício vs chamar getAlbum × N: 1 request paginada por artista.
        Nota: os playcounts por track NÃO vêm aqui — o response tem só metadata
        de release. Para playcount precisa chamar getAlbum por álbum.

        {
            "id": str,
            "name": str,
            "releases": [
                {
                    "id": str, "name": str, "type": "album"|"single"|"compilation"|"appears_on",
                    "release_date": str, "total_tracks": int,
                    "artists": [{"id", "name"}],
                },
                ...
            ],
        }
        """
        releases: list[dict] = []
        total_count: Optional[int] = None
        offset = 0
        # Pagina automaticamente até pegar todos (limite hard: 10 páginas = 500 releases)
        for _ in range(10):
            variables = {
                "uri": f"spotify:artist:{artist_id}",
                "offset": offset,
                "limit": limit,
            }
            data = self._query("queryArtistDiscographyAll", variables)
            artist = data.get("artistUnion") or data.get("artist") or {}
            if not artist:
                raise SpotifyGraphQLError(f"artistUnion vazio para {artist_id}: {data}")

            profile = artist.get("profile") or {}
            disco = artist.get("discography") or {}
            disco_all = disco.get("all") or {}
            if total_count is None:
                total_count = disco_all.get("totalCount")

            items = disco_all.get("items") or []
            if not items:
                break

            for wrapper in items:
                inner_items = (wrapper.get("releases") or {}).get("items") or []
                for release in inner_items:
                    rid = _uri_id(release.get("uri", "")) or release.get("id", "")
                    if not rid:
                        continue
                    release_date_obj = release.get("date") or {}
                    if isinstance(release_date_obj, dict):
                        date_str = release_date_obj.get("isoString") or (
                            str(release_date_obj.get("year")) if release_date_obj.get("year") else None
                        )
                    else:
                        date_str = str(release_date_obj) if release_date_obj else None

                    tracks_info = release.get("tracks") or {}
                    total_tracks = tracks_info.get("totalCount") if isinstance(tracks_info, dict) else None

                    cover_sources = ((release.get("coverArt") or {}).get("sources")) or []
                    cover_url = cover_sources[0].get("url") if cover_sources else None

                    r_artists_raw = (release.get("artists") or {}).get("items") or []
                    r_artists = [
                        {"id": _uri_id(a.get("uri", "")), "name": (a.get("profile") or {}).get("name", "")}
                        for a in r_artists_raw
                    ]

                    rtype = (release.get("type") or "").lower()  # "ALBUM" / "SINGLE" / ...
                    releases.append({
                        "id": rid,
                        "name": release.get("name", ""),
                        "type": rtype,
                        "release_date": date_str,
                        "total_tracks": total_tracks,
                        "cover_url": cover_url,
                        "artists": r_artists,
                    })

            offset += limit
            if total_count is not None and len(releases) >= total_count:
                break

        # nome do artista (última iteração já tem profile)
        artist_name = (artist.get("profile") or {}).get("name", "")

        return {
            "id": artist_id,
            "name": artist_name,
            "releases": releases,
            "total_count": total_count,
            "source": "graphql",
        }

    def get_artist_appears_on(self, artist_id: str, limit: int = 50, max_items: Optional[int] = None) -> dict:
        """
        Retorna releases onde o artista APARECE COMO FEATURE (álbuns de outros artistas).

        Diferente de `get_artist_discography_all`, esta query retorna releases que
        não pertencem ao artista — o artista é apenas feature em algumas tracks.
        Para saber EM QUAIS tracks ele aparece, é preciso chamar `get_album`
        por release e filtrar por `artist_id` na lista de artistas de cada track.

        {
            "id": str,
            "releases": [
                {"id", "name", "primary_artists": [{id, name}], "cover_url"},
                ...
            ],
            "total_count": int,
        }

        ⚠️ Artistas populares podem ter > 1000 "appears on" (cada playlist, cada
        release onde são creditados). Use `max_items` pra cortar.
        """
        releases: list[dict] = []
        total_count: Optional[int] = None
        offset = 0

        for _ in range(200):  # hard cap de páginas
            variables = {
                "uri": f"spotify:artist:{artist_id}",
                "offset": offset,
                "limit": limit,
            }
            data = self._query("queryArtistAppearsOn", variables)
            artist = data.get("artistUnion") or data.get("artist") or {}
            if not artist:
                raise SpotifyGraphQLError(f"artistUnion vazio para {artist_id}: {data}")

            ao = (artist.get("relatedContent") or {}).get("appearsOn") or {}
            if total_count is None:
                total_count = ao.get("totalCount")

            items = ao.get("items") or []
            if not items:
                break

            for wrapper in items:
                inner_items = (wrapper.get("releases") or {}).get("items") or []
                for release in inner_items:
                    rid = _uri_id(release.get("uri", "")) or release.get("id", "")
                    if not rid:
                        continue
                    artists_raw = (release.get("artists") or {}).get("items") or []
                    primary_artists = [
                        {"id": _uri_id(a.get("uri", "")), "name": (a.get("profile") or {}).get("name", "")}
                        for a in artists_raw
                    ]
                    cover_sources = ((release.get("coverArt") or {}).get("sources")) or []
                    cover_url = cover_sources[0].get("url") if cover_sources else None

                    releases.append({
                        "id": rid,
                        "name": release.get("name", ""),
                        "primary_artists": primary_artists,
                        "cover_url": cover_url,
                    })
                    if max_items is not None and len(releases) >= max_items:
                        break
                if max_items is not None and len(releases) >= max_items:
                    break

            if max_items is not None and len(releases) >= max_items:
                break
            offset += limit
            if total_count is not None and len(releases) >= total_count:
                break

        return {
            "id": artist_id,
            "releases": releases,
            "total_count": total_count,
            "source": "graphql",
        }

    def get_artist_discovered_on(self, artist_id: str) -> dict:
        """
        Retorna a lista 'Descoberto em' do perfil do artista — playlists que
        estão impulsionando streams daquele artista. Dado visível no web player
        mas NÃO disponível na API oficial do Spotify.

        {
            "id": str,
            "name": str,
            "playlists": [
                {
                    "id": str, "name": str, "uri": str,
                    "image_url": str | None,
                    "owner": {"id": str, "name": str},
                },
                ...
            ],
        }
        """
        variables = {"uri": f"spotify:artist:{artist_id}"}
        data = self._query("queryArtistDiscoveredOn", variables)
        artist = data.get("artistUnion") or data.get("artist") or {}
        if not artist:
            raise SpotifyGraphQLError(f"artistUnion vazio para {artist_id}: {data}")

        profile = artist.get("profile") or {}
        related = artist.get("relatedContent") or {}
        discovered = related.get("discoveredOnV2") or related.get("discoveredOn") or {}
        items = discovered.get("items") or []

        playlists: list[dict] = []
        for item in items:
            pl = item.get("data") or item
            if pl.get("__typename") not in (None, "Playlist", "PlaylistResponseWrapper"):
                # tem outros tipos eventualmente, filtra
                pass
            uri = pl.get("uri", "")
            if not uri.startswith("spotify:playlist:"):
                # alguns items são podcasts etc.
                continue

            owner_obj = pl.get("ownerV2") or pl.get("owner") or {}
            owner_data = owner_obj.get("data") if isinstance(owner_obj.get("data"), dict) else owner_obj
            owner = {
                "id": _uri_id((owner_data or {}).get("uri", "")),
                "name": (owner_data or {}).get("name", ""),
            }

            # image
            images = (pl.get("images") or {}).get("items") or pl.get("imageUrls") or []
            image_url = None
            if images:
                first_img = images[0]
                if isinstance(first_img, dict):
                    sources = first_img.get("sources") or []
                    image_url = sources[0].get("url") if sources else first_img.get("url")
                elif isinstance(first_img, str):
                    image_url = first_img

            playlists.append({
                "id": _uri_id(uri),
                "uri": uri,
                "name": pl.get("name", ""),
                "image_url": image_url,
                "owner": owner,
            })

        return {
            "id": artist_id,
            "name": profile.get("name", ""),
            "playlists": playlists,
            "source": "graphql",
        }

    def get_artist_related(self, artist_id: str) -> dict:
        """
        Retorna artistas relacionados (API oficial deprecou esse endpoint em 2024,
        só a Partner API ainda expõe).

        {
            "id": str,
            "name": str,
            "related": [{"id": str, "name": str, "image_url": str|None}, ...],
        }
        """
        variables = {"uri": f"spotify:artist:{artist_id}"}
        data = self._query("queryArtistRelated", variables)
        artist = data.get("artistUnion") or data.get("artist") or {}
        if not artist:
            raise SpotifyGraphQLError(f"artistUnion vazio para {artist_id}: {data}")

        profile = artist.get("profile") or {}
        related_raw = (artist.get("relatedContent") or {}).get("relatedArtists") or {}
        items = related_raw.get("items") or []

        related = []
        for it in items:
            r_profile = it.get("profile") or {}
            r_visuals = it.get("visuals") or {}
            avatar = ((r_visuals.get("avatarImage") or {}).get("sources") or [])
            image_url = avatar[0].get("url") if avatar else None
            related.append({
                "id": _uri_id(it.get("uri", "")),
                "name": r_profile.get("name", ""),
                "image_url": image_url,
            })

        return {
            "id": artist_id,
            "name": profile.get("name", ""),
            "related": related,
            "source": "graphql",
        }


def _uri_id(uri: str) -> str:
    """'spotify:track:XYZ' -> 'XYZ'."""
    if not uri:
        return ""
    if ":" in uri:
        return uri.rsplit(":", 1)[-1]
    return uri
