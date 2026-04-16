"""Gerencia tokens anônimos do Spotify Web Player.

Duas fontes em ordem:
  1. Endpoint direto: https://open.spotify.com/get_access_token (pode ser bloqueado
     em algumas redes ou retornar 403 sem motivo aparente).
  2. Fallback: extrair o accessToken do JSON __NEXT_DATA__ de uma página de embed
     pública. Este é o mesmo token que o próprio iframe do Spotify usa para chamar
     a Partner API GraphQL — funciona em redes onde o endpoint direto está bloqueado.
"""
from __future__ import annotations

import html
import json
import logging
import random
import re
import threading
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


class SpotifyAuthError(Exception):
    pass


class SpotifyAuth:
    """
    Gerencia access tokens anônimos do Spotify Web Player.
    Tenta primeiro /get_access_token; se falhar, faz scrape de página de embed.
    Renova automaticamente antes de expirar. Thread-safe.
    """

    def __init__(self, client: Optional[httpx.Client] = None):
        self._access_token: Optional[str] = None
        self._expires_at_ms: int = 0
        self._request_count: int = 0
        self._lock = threading.Lock()
        self._client = client or httpx.Client(
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        self._owns_client = client is None
        # Uma vez que o endpoint direto falha, não tentamos de novo nesta sessão.
        self._direct_endpoint_disabled = False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _is_expired(self) -> bool:
        if not self._access_token:
            return True
        now_ms = int(time.time() * 1000)
        margin_ms = settings.TOKEN_REFRESH_MARGIN_SECONDS * 1000
        return now_ms + margin_ms >= self._expires_at_ms

    def _needs_rotation(self) -> bool:
        return self._request_count >= settings.TOKEN_ROTATION_REQUESTS

    def _try_direct_endpoint(self) -> Optional[dict]:
        """Tenta /get_access_token. Retorna dict ou None se falhar.

        Por padrão, não tentamos esse endpoint (retorna 403 na maioria dos IPs
        residenciais brasileiros). Só tenta se TRY_DIRECT_TOKEN_ENDPOINT=1 no env.
        """
        if self._direct_endpoint_disabled:
            return None
        if not settings.TRY_DIRECT_TOKEN_ENDPOINT:
            # Embed é primário nesta rede — nem tenta o endpoint direto.
            self._direct_endpoint_disabled = True
            return None
        ua = random.choice(settings.USER_AGENTS)
        headers = {
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://open.spotify.com/",
            "Origin": "https://open.spotify.com",
        }
        try:
            resp = self._client.get(settings.TOKEN_URL, headers=headers)
            if resp.status_code == 403:
                logger.info("Endpoint direto indisponível (403), usando embed.")
                self._direct_endpoint_disabled = True
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data.get("accessToken"):
                logger.info("Endpoint direto retornou payload inválido, usando embed.")
                return None
            return data
        except (httpx.HTTPStatusError, httpx.TransportError, json.JSONDecodeError) as e:
            logger.info("Endpoint direto falhou (%s), usando embed.", type(e).__name__)
            return None

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((httpx.TransportError,)),
        reraise=True,
    )
    def _fetch_token_via_embed(self) -> dict:
        """
        Extrai o accessToken de uma página de embed pública.
        O próprio iframe do Spotify faz isso para chamar a Partner API.
        """
        album_id = settings.TOKEN_FALLBACK_ALBUM_ID
        url = f"https://open.spotify.com/embed/album/{album_id}"
        headers = {
            "User-Agent": random.choice(settings.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://open.spotify.com/",
        }
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            raise SpotifyAuthError("Não foi possível encontrar __NEXT_DATA__ na página de embed")
        raw = m.group("json")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = json.loads(html.unescape(raw))
        try:
            session = data["props"]["pageProps"]["state"]["settings"]["session"]
        except (KeyError, TypeError) as e:
            raise SpotifyAuthError(f"Estrutura __NEXT_DATA__ inesperada: {e}")
        token = session.get("accessToken")
        exp = session.get("accessTokenExpirationTimestampMs")
        if not token or not exp:
            raise SpotifyAuthError(f"session sem accessToken: {session}")
        return {
            "accessToken": token,
            "accessTokenExpirationTimestampMs": exp,
            "isAnonymous": session.get("isAnonymous", True),
        }

    def _fetch_token(self) -> None:
        logger.debug("Obtaining anonymous access token...")
        data = self._try_direct_endpoint()
        source = "direct"
        if data is None:
            data = self._fetch_token_via_embed()
            source = "embed"

        token = data.get("accessToken")
        exp = data.get("accessTokenExpirationTimestampMs")
        if not token or not exp:
            raise SpotifyAuthError(f"Resposta inválida do endpoint de token ({source}): {data}")

        self._access_token = token
        self._expires_at_ms = int(exp)
        self._request_count = 0
        is_anon = data.get("isAnonymous", True)
        logger.info(
            "Access token obtido via %s (anonymous=%s, expires_in=%ds)",
            source,
            is_anon,
            (self._expires_at_ms - int(time.time() * 1000)) // 1000,
        )

    def get_token(self, force_refresh: bool = False) -> str:
        """Retorna um access token válido, renovando se necessário."""
        with self._lock:
            if force_refresh or self._is_expired() or self._needs_rotation():
                self._fetch_token()
            assert self._access_token is not None
            self._request_count += 1
            return self._access_token

    def invalidate(self) -> None:
        """Força renovação na próxima chamada (ex: após 401)."""
        with self._lock:
            self._access_token = None
            self._expires_at_ms = 0
            self._request_count = 0
