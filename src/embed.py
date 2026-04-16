"""Client para a Embed API do Spotify.

Papéis do embed neste projeto:
  1. Fonte de METADATA de fallback quando GraphQL falha (nome, track_id, duração,
     artista nome — mas SEM playcount, que a embed não retorna).
  2. Fonte de accessToken anônimo (ver auth.py) — o iframe do embed carrega um
     token válido em __NEXT_DATA__.

IMPORTANTE: A Embed API **não retorna playcount**. Ela só é útil como:
  - Fallback de metadata (catálogo de tracks em um álbum).
  - Fonte de token para chamar GraphQL.
"""
from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<json>.*?)</script>',
    re.DOTALL,
)


class SpotifyEmbedError(Exception):
    pass


class SpotifyEmbed:
    """Client para open.spotify.com/embed/<type>/<id>."""

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client or httpx.Client(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(settings.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://open.spotify.com/",
        }

    def _delay(self) -> None:
        time.sleep(random.uniform(settings.EMBED_DELAY_MIN, settings.EMBED_DELAY_MAX))

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.BACKOFF_FACTOR, min=1, max=30),
        retry=retry_if_exception_type((httpx.TransportError,)),
        reraise=True,
    )
    def _fetch_html(self, url: str) -> str:
        resp = self._client.get(url, headers=self._headers())
        if resp.status_code == 429:
            raise SpotifyEmbedError(f"429 rate limited em {url}")
        resp.raise_for_status()
        return resp.text

    def _extract_next_data(self, html_text: str) -> dict:
        m = _NEXT_DATA_RE.search(html_text)
        if not m:
            raise SpotifyEmbedError("Não foi possível encontrar __NEXT_DATA__ no HTML")
        raw = m.group("json")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return json.loads(html.unescape(raw))

    def get_track(self, track_id: str) -> dict:
        """
        Retorna metadata de uma track via embed. SEM playcount.
        {
            "id": str,
            "name": str,
            "duration_ms": int,
            "artists": [{"id": str, "name": str}, ...],  # track embed tem artists com URI
            "album": {"id": str, "name": str},
            "explicit": bool,
            "playcount": None,  # embed não retorna
        }
        """
        self._delay()
        url = settings.EMBED_TRACK_URL.format(track_id=track_id)
        html_text = self._fetch_html(url)
        data = self._extract_next_data(html_text)

        entity = (
            data.get("props", {})
            .get("pageProps", {})
            .get("state", {})
            .get("data", {})
            .get("entity", {})
        )
        if not entity:
            raise SpotifyEmbedError(f"Entity não encontrada no embed da track {track_id}")

        artists_raw = entity.get("artists") or []
        artists = [
            {"id": _uri_id(a.get("uri", "")), "name": a.get("name", "")}
            for a in artists_raw
        ]

        # album_id nem sempre vem no track embed; relatedEntityUri às vezes aponta pro artista
        album_uri = entity.get("releaseUri") or entity.get("albumUri") or ""
        album_name = entity.get("release") or entity.get("albumName") or ""

        return {
            "id": track_id,
            "playcount": None,  # indisponível no embed
            "name": entity.get("title") or entity.get("name") or "",
            "duration_ms": entity.get("duration"),
            "artists": artists,
            "album": {"id": _uri_id(album_uri), "name": album_name},
            "explicit": entity.get("isExplicit"),
            "source": "embed",
        }

    def get_album_tracks(self, album_id: str) -> list[dict]:
        """
        Retorna lista de tracks do álbum via embed (só metadata).

        LIMITAÇÕES do embed de álbum (vs track individual):
        - Não retorna playcount (sempre None).
        - Tracks vêm em `trackList` com `uri`, `title`, `subtitle` (nome do artista
          como string), `duration`, `isExplicit` — MAS sem URI de artista.
        - Para pegar artist_id real, precisa hitar o embed de cada track individual.
        """
        self._delay()
        url = settings.EMBED_ALBUM_URL.format(album_id=album_id)
        html_text = self._fetch_html(url)
        data = self._extract_next_data(html_text)

        entity = (
            data.get("props", {})
            .get("pageProps", {})
            .get("state", {})
            .get("data", {})
            .get("entity", {})
        )
        if not entity:
            raise SpotifyEmbedError(f"Entity não encontrada no embed do álbum {album_id}")

        album_name = entity.get("title") or entity.get("name") or ""
        album_artist_name = entity.get("subtitle") or ""  # string, sem ID
        tracks_raw = entity.get("trackList") or entity.get("tracks") or []

        result = []
        for t in tracks_raw:
            tid = _uri_id(t.get("uri", ""))
            if not tid:
                continue
            # artist no trackList: só string em "subtitle", sem URI
            artist_name = t.get("subtitle") or album_artist_name
            result.append({
                "id": tid,
                "playcount": None,  # indisponível no embed
                "name": t.get("title") or t.get("name") or "",
                "duration_ms": t.get("duration"),
                "artists": [{"id": "", "name": artist_name}] if artist_name else [],
                "album": {"id": album_id, "name": album_name},
                "explicit": t.get("isExplicit"),
                "source": "embed",
            })
        return result


def _uri_id(uri: str) -> str:
    """'spotify:track:XYZ' -> 'XYZ'. Tolera IDs puros."""
    if not uri:
        return ""
    if ":" in uri:
        return uri.rsplit(":", 1)[-1]
    return uri
