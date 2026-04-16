"""
Client para o endpoint de créditos do Spotify.

Diferente das outras queries que usam GraphQL, credits é servido por um endpoint
REST legado em `spclient.wg.spotify.com/track-credits-view/v0/experimental/`.
Usa o mesmo accessToken anônimo do embed.

Retorna:
  - Performers (intérpretes, instrumentistas)
  - Writers (compositores, letristas)
  - Producers (produtores, engenheiros de mixagem/masterização)
  - Source (selo, ano, catálogo)

⚠️ IMPORTANTE: Muitos tracks (especialmente de artistas menores ou releases antigos)
retornam créditos vazios porque o Spotify simplesmente não tem essa informação
cadastrada. O endpoint retorna 200 com roleCredits contendo arrays vazios.
"""
from __future__ import annotations

import logging
import random
from typing import Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import settings
from src.auth import SpotifyAuth

logger = logging.getLogger(__name__)

CREDITS_URL = "https://spclient.wg.spotify.com/track-credits-view/v0/experimental/{track_id}/credits"


class SpotifyCreditsError(Exception):
    pass


class SpotifyCredits:
    """Client para o endpoint REST de créditos de track."""

    def __init__(self, auth: SpotifyAuth, client: Optional[httpx.Client] = None):
        self._auth = auth
        self._client = client or httpx.Client(timeout=settings.HTTP_TIMEOUT, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.BACKOFF_FACTOR, min=1, max=30),
        retry=retry_if_exception_type((httpx.TransportError,)),
        reraise=True,
    )
    def get_track_credits(self, track_id: str) -> dict:
        """
        Retorna créditos normalizados da track.

        Output shape:
        {
            "track_id": str,
            "track_title": str,
            "performers": [{"name": str, "roles": [str, ...]}, ...],
            "writers":    [{"name": str, "roles": [str, ...]}, ...],
            "producers":  [{"name": str, "roles": [str, ...]}, ...],
            "extended":   [{"name": str, "roles": [str, ...]}, ...],  # outras roles
            "source_names": [str, ...],  # selos, catálogo
            "has_data": bool,
        }
        """
        token = self._auth.get_token()
        url = CREDITS_URL.format(track_id=track_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": random.choice(settings.USER_AGENTS),
            "App-Platform": "WebPlayer",
            "Accept": "application/json",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
        }
        resp = self._client.get(url, headers=headers)

        if resp.status_code == 401:
            self._auth.invalidate()
            token = self._auth.get_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            resp = self._client.get(url, headers=headers)

        if resp.status_code == 404:
            return {
                "track_id": track_id,
                "track_title": "",
                "performers": [], "writers": [], "producers": [], "extended": [],
                "source_names": [],
                "has_data": False,
            }

        resp.raise_for_status()
        data = resp.json()

        # Normalizar roleCredits: [{roleTitle: "Performers", artists: [{name, roles:[]}, ...]}, ...]
        role_map: dict[str, list] = {}
        for block in data.get("roleCredits") or []:
            role_title = (block.get("roleTitle") or "").lower()
            artists = [
                {
                    "name": a.get("name", ""),
                    "roles": a.get("roles") or [],
                    "image_url": a.get("imageUri"),
                }
                for a in (block.get("artists") or [])
            ]
            role_map[role_title] = artists

        extended = []
        for block in data.get("extendedCredits") or []:
            role_title = block.get("roleTitle") or ""
            for a in block.get("artists") or []:
                extended.append({
                    "name": a.get("name", ""),
                    "role_title": role_title,
                    "roles": a.get("roles") or [],
                })

        performers = role_map.get("performers", [])
        writers = role_map.get("writers", [])
        producers = role_map.get("producers", [])

        has_data = bool(performers or writers or producers or extended)

        return {
            "track_id": track_id,
            "track_title": data.get("trackTitle", ""),
            "performers": performers,
            "writers": writers,
            "producers": producers,
            "extended": extended,
            "source_names": data.get("sourceNames") or [],
            "has_data": has_data,
        }
