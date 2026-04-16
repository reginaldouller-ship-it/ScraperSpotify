"""Modelos de dados."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class TrackSnapshot:
    track_id: str
    track_name: str
    artist_id: str
    artist_name: str
    album_id: str
    album_name: str
    playcount: int
    snapshot_date: date
    source: str  # "graphql" ou "embed"
    daily_streams: Optional[int] = None
    popularity: Optional[int] = None
    duration_ms: Optional[int] = None
    explicit: Optional[bool] = None


@dataclass
class ArtistSnapshot:
    artist_id: str
    artist_name: str
    monthly_listeners: Optional[int]
    followers: Optional[int]
    world_rank: Optional[int]
    top_cities_json: Optional[str]  # JSON string
    snapshot_date: date
    popularity: Optional[int] = None
    biography: Optional[str] = None


@dataclass
class MonitoredTrack:
    track_id: str
    track_name: str
    artist_id: str
    artist_name: str
    album_id: str
    album_name: str


@dataclass
class MonitoredArtist:
    artist_id: str
    artist_name: str


@dataclass
class ScrapeResult:
    """Resultado de uma execução do scraper."""
    tracks_processed: int = 0
    tracks_success: int = 0
    tracks_failed: int = 0
    artists_processed: int = 0
    artists_success: int = 0
    artists_failed: int = 0
    graphql_requests: int = 0
    embed_requests: int = 0
    rate_limit_hits: int = 0
    tokens_renewed: int = 0
    duration_seconds: float = 0.0
    errors: list = field(default_factory=list)
