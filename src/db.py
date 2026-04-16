"""Persistência em SQLite."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from config import settings
from src.models import ArtistSnapshot, MonitoredArtist, MonitoredTrack, TrackSnapshot

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS monitored_tracks (
    track_id TEXT PRIMARY KEY,
    track_name TEXT,
    artist_id TEXT,
    artist_name TEXT,
    album_id TEXT,
    album_name TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitored_artists (
    artist_id TEXT PRIMARY KEY,
    artist_name TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitored_albums (
    album_id TEXT PRIMARY KEY,
    album_name TEXT,
    artist_id TEXT,
    artist_name TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Mapping N:N: um artista pode ter múltiplas tracks e uma track pode ter
-- múltiplos artistas (primary + features). O mesmo track_id pode aparecer
-- várias vezes aqui (uma linha por artista envolvido).
CREATE TABLE IF NOT EXISTS artist_tracks (
    artist_id  TEXT NOT NULL,
    track_id   TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 1,  -- 1=primary, 0=feature
    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_artist_tracks_artist ON artist_tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_tracks_track  ON artist_tracks(track_id);

-- Backfill: se monitored_tracks já tem artist_id, cria a relação primary.
-- Roda na inicialização; IGNORE garante idempotência.
INSERT OR IGNORE INTO artist_tracks (artist_id, track_id, is_primary)
SELECT artist_id, track_id, 1
FROM monitored_tracks
WHERE artist_id IS NOT NULL AND artist_id != '';

CREATE TABLE IF NOT EXISTS track_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    playcount BIGINT NOT NULL,
    daily_streams BIGINT,
    popularity INTEGER,
    duration_ms INTEGER,
    explicit INTEGER,
    snapshot_date DATE NOT NULL,
    source TEXT DEFAULT 'graphql',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(track_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_track_snapshots_date ON track_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_track_snapshots_track ON track_snapshots(track_id);

CREATE TABLE IF NOT EXISTS artist_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id TEXT NOT NULL,
    monthly_listeners BIGINT,
    world_rank INTEGER,
    followers BIGINT,
    popularity INTEGER,
    top_cities_json TEXT,
    biography TEXT,
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(artist_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_artist_snapshots_date ON artist_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_artist_snapshots_artist ON artist_snapshots(artist_id);

CREATE VIEW IF NOT EXISTS daily_streams AS
SELECT
    t.track_id,
    t.track_name,
    t.artist_name,
    t.album_name,
    ts.snapshot_date,
    ts.playcount AS total_streams,
    ts.daily_streams,
    CASE
        WHEN prev.playcount > 0
        THEN ROUND((ts.playcount - prev.playcount) * 100.0 / prev.playcount, 4)
        ELSE NULL
    END AS daily_change_pct,
    ts.source
FROM track_snapshots ts
JOIN monitored_tracks t ON t.track_id = ts.track_id
LEFT JOIN track_snapshots prev
    ON prev.track_id = ts.track_id
    AND prev.snapshot_date = date(ts.snapshot_date, '-1 day');
"""


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.DATABASE_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # --------- monitored_tracks ---------

    def upsert_monitored_track(self, t: MonitoredTrack) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO monitored_tracks (track_id, track_name, artist_id, artist_name, album_id, album_name)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    track_name=excluded.track_name,
                    artist_id=excluded.artist_id,
                    artist_name=excluded.artist_name,
                    album_id=excluded.album_id,
                    album_name=excluded.album_name
                """,
                (t.track_id, t.track_name, t.artist_id, t.artist_name, t.album_id, t.album_name),
            )

    def upsert_monitored_tracks(self, tracks: Iterable[MonitoredTrack]) -> int:
        count = 0
        with self.connect() as conn:
            for t in tracks:
                conn.execute(
                    """
                    INSERT INTO monitored_tracks (track_id, track_name, artist_id, artist_name, album_id, album_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                        track_name=excluded.track_name,
                        artist_id=excluded.artist_id,
                        artist_name=excluded.artist_name,
                        album_id=excluded.album_id,
                        album_name=excluded.album_name
                    """,
                    (t.track_id, t.track_name, t.artist_id, t.artist_name, t.album_id, t.album_name),
                )
                count += 1
        return count

    def list_monitored_tracks(self) -> list[MonitoredTrack]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM monitored_tracks ORDER BY added_at").fetchall()
        return [
            MonitoredTrack(
                track_id=r["track_id"],
                track_name=r["track_name"] or "",
                artist_id=r["artist_id"] or "",
                artist_name=r["artist_name"] or "",
                album_id=r["album_id"] or "",
                album_name=r["album_name"] or "",
            )
            for r in rows
        ]

    def list_monitored_albums(self) -> list[tuple[str, str]]:
        """Retorna (album_id, album_name) dos álbuns únicos das tracks monitoradas."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT album_id, album_name FROM monitored_tracks WHERE album_id != '' AND album_id IS NOT NULL"
            ).fetchall()
        return [(r["album_id"], r["album_name"] or "") for r in rows]

    # --------- monitored_artists ---------

    def upsert_monitored_artist(self, a: MonitoredArtist) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO monitored_artists (artist_id, artist_name) VALUES (?, ?)
                ON CONFLICT(artist_id) DO UPDATE SET artist_name=excluded.artist_name
                """,
                (a.artist_id, a.artist_name),
            )

    def list_monitored_artists(self) -> list[MonitoredArtist]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM monitored_artists ORDER BY added_at").fetchall()
        return [MonitoredArtist(artist_id=r["artist_id"], artist_name=r["artist_name"] or "") for r in rows]

    # --------- artist_tracks (N:N) ---------

    def upsert_artist_track(self, artist_id: str, track_id: str, is_primary: bool) -> None:
        if not artist_id or not track_id:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artist_tracks (artist_id, track_id, is_primary)
                VALUES (?, ?, ?)
                ON CONFLICT(artist_id, track_id) DO UPDATE SET
                    is_primary = CASE
                        -- uma vez primary, sempre primary (não rebaixa pra feature)
                        WHEN artist_tracks.is_primary = 1 THEN 1
                        ELSE excluded.is_primary
                    END
                """,
                (artist_id, track_id, int(is_primary)),
            )

    def list_tracks_for_artist(
        self,
        artist_id: str,
        include_primary: bool = True,
        include_features: bool = True,
    ) -> list[sqlite3.Row]:
        """Lista tracks de um artista (primary e/ou feature) com último snapshot."""
        role_filter = []
        if include_primary:
            role_filter.append("at.is_primary = 1")
        if include_features:
            role_filter.append("at.is_primary = 0")
        if not role_filter:
            return []
        where = f"at.artist_id = ? AND ({' OR '.join(role_filter)})"

        query = f"""
            SELECT
                mt.track_id,
                mt.track_name,
                mt.artist_name AS primary_artist_name,
                mt.album_name,
                at.is_primary,
                ts.playcount,
                ts.daily_streams,
                ts.snapshot_date,
                ts.source
            FROM artist_tracks at
            JOIN monitored_tracks mt ON mt.track_id = at.track_id
            LEFT JOIN track_snapshots ts ON ts.track_id = mt.track_id
                AND ts.snapshot_date = (
                    SELECT MAX(snapshot_date) FROM track_snapshots WHERE track_id = mt.track_id
                )
            WHERE {where}
            ORDER BY ts.playcount DESC NULLS LAST
        """
        with self.connect() as conn:
            return conn.execute(query, (artist_id,)).fetchall()

    def distinct_artist_ids_from_tracks(self) -> list[tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT artist_id, artist_name FROM monitored_tracks "
                "WHERE artist_id != '' AND artist_id IS NOT NULL"
            ).fetchall()
        return [(r["artist_id"], r["artist_name"] or "") for r in rows]

    # --------- track_snapshots ---------

    def get_previous_playcount(self, track_id: str, current_date: date) -> Optional[int]:
        """Retorna o playcount mais recente antes de current_date."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT playcount FROM track_snapshots
                WHERE track_id = ? AND snapshot_date < ?
                ORDER BY snapshot_date DESC LIMIT 1
                """,
                (track_id, current_date.isoformat()),
            ).fetchone()
        return row["playcount"] if row else None

    def upsert_track_snapshot(self, s: TrackSnapshot) -> None:
        prev = self.get_previous_playcount(s.track_id, s.snapshot_date)
        daily = (s.playcount - prev) if prev is not None else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO track_snapshots
                    (track_id, playcount, daily_streams, popularity, duration_ms, explicit, snapshot_date, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_id, snapshot_date) DO UPDATE SET
                    playcount=excluded.playcount,
                    daily_streams=excluded.daily_streams,
                    popularity=excluded.popularity,
                    duration_ms=excluded.duration_ms,
                    explicit=excluded.explicit,
                    source=excluded.source
                """,
                (
                    s.track_id,
                    s.playcount,
                    daily,
                    s.popularity,
                    s.duration_ms,
                    int(s.explicit) if s.explicit is not None else None,
                    s.snapshot_date.isoformat(),
                    s.source,
                ),
            )

    # --------- artist_snapshots ---------

    def upsert_artist_snapshot(self, s: ArtistSnapshot) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artist_snapshots
                    (artist_id, monthly_listeners, world_rank, followers, popularity, top_cities_json, biography, snapshot_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artist_id, snapshot_date) DO UPDATE SET
                    monthly_listeners=excluded.monthly_listeners,
                    world_rank=excluded.world_rank,
                    followers=excluded.followers,
                    popularity=excluded.popularity,
                    top_cities_json=excluded.top_cities_json,
                    biography=excluded.biography
                """,
                (
                    s.artist_id,
                    s.monthly_listeners,
                    s.world_rank,
                    s.followers,
                    s.popularity,
                    s.top_cities_json,
                    s.biography,
                    s.snapshot_date.isoformat(),
                ),
            )

    # --------- queries ---------

    def status(self) -> dict:
        with self.connect() as conn:
            monitored = conn.execute("SELECT COUNT(*) as c FROM monitored_tracks").fetchone()["c"]
            artists = conn.execute("SELECT COUNT(*) as c FROM monitored_artists").fetchone()["c"]
            snaps = conn.execute("SELECT COUNT(*) as c FROM track_snapshots").fetchone()["c"]
            last = conn.execute("SELECT MAX(snapshot_date) as d FROM track_snapshots").fetchone()["d"]
        return {
            "monitored_tracks": monitored,
            "monitored_artists": artists,
            "track_snapshots": snaps,
            "last_snapshot_date": last,
        }

    def export_daily_streams(
        self,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        artist_id: Optional[str] = None,
        only_primary: bool = False,
        only_features: bool = False,
    ) -> list[sqlite3.Row]:
        """
        Exporta daily_streams, opcionalmente filtrado por artista via artist_tracks.
        Quando `artist_id` é passado, usa o mapping N:N — pega tracks como
        primary OU feature (a menos que only_primary/only_features seja True).
        """
        params: list = []
        if artist_id:
            # join via artist_tracks (captura primary + feature)
            role_filter = ""
            if only_primary and not only_features:
                role_filter = " AND at.is_primary = 1"
            elif only_features and not only_primary:
                role_filter = " AND at.is_primary = 0"
            query = (
                "SELECT ds.*, art.monthly_listeners, art.world_rank, "
                "       mt.artist_id AS primary_artist_id, at.is_primary AS role_primary "
                "FROM artist_tracks at "
                "JOIN monitored_tracks mt ON mt.track_id = at.track_id "
                "JOIN daily_streams ds ON ds.track_id = at.track_id "
                "LEFT JOIN artist_snapshots art "
                "  ON art.artist_id = at.artist_id AND art.snapshot_date = ds.snapshot_date "
                f"WHERE at.artist_id = ?{role_filter}"
            )
            params.append(artist_id)
        else:
            query = (
                "SELECT ds.*, art.monthly_listeners, art.world_rank, "
                "       mt.artist_id AS primary_artist_id, 1 AS role_primary "
                "FROM daily_streams ds "
                "LEFT JOIN monitored_tracks mt ON mt.track_id = ds.track_id "
                "LEFT JOIN artist_snapshots art "
                "  ON art.artist_id = mt.artist_id AND art.snapshot_date = ds.snapshot_date "
                "WHERE 1=1"
            )
        if date_from:
            query += " AND ds.snapshot_date >= ?"
            params.append(date_from.isoformat())
        if date_to:
            query += " AND ds.snapshot_date <= ?"
            params.append(date_to.isoformat())
        query += " ORDER BY ds.snapshot_date DESC, ds.total_streams DESC"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()
