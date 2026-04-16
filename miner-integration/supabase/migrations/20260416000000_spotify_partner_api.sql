-- Migration: Spotify Partner API enrichment tables
--
-- Adiciona capacidade de capturar dados NÃO disponíveis na API oficial do Spotify:
--   - playcount por track (streams totais)
--   - monthly listeners por artista
--   - world rank, followers exatos, top cities
--   - "Discovered on" (playlists que impulsionam streams)
--
-- Não substitui tabelas existentes. Coexiste com:
--   - `artists`, `track_history`, `artist_snapshots`, `track_positions`
--
-- Fonte dos dados: https://api-partner.spotify.com/pathfinder/v1/query
-- Autenticação: token anônimo extraído do web player (sem OAuth).

BEGIN;

-- ============================================================
-- 1. Snapshots diários de playcount por track
-- ============================================================
CREATE TABLE IF NOT EXISTS public.spotify_partner_track_snapshots (
    id BIGSERIAL PRIMARY KEY,
    track_id TEXT NOT NULL,             -- Spotify track ID (22 chars base62)
    playcount BIGINT NOT NULL,          -- streams totais acumulados
    daily_streams BIGINT,               -- playcount_hoje - playcount_ontem (calculado no INSERT)
    snapshot_date DATE NOT NULL,
    source TEXT NOT NULL DEFAULT 'graphql_partner',  -- graphql_partner | embed
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (track_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_partner_track_snap_date
    ON public.spotify_partner_track_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_partner_track_snap_track
    ON public.spotify_partner_track_snapshots (track_id, snapshot_date DESC);

COMMENT ON TABLE public.spotify_partner_track_snapshots IS
    'Snapshot diário de playcount por track via Spotify Partner GraphQL API. Complementa `track_history` (legado) adicionando streams totais.';

-- ============================================================
-- 2. Snapshots diários de dados de artista
-- ============================================================
CREATE TABLE IF NOT EXISTS public.spotify_partner_artist_snapshots (
    id BIGSERIAL PRIMARY KEY,
    artist_id TEXT NOT NULL,
    artist_name TEXT,
    monthly_listeners BIGINT,
    followers BIGINT,
    world_rank INTEGER,                 -- 0 / NULL quando artista pequeno
    popularity INTEGER,                 -- 0-100
    top_cities JSONB,                   -- [{city, country, region, listeners}]
    biography TEXT,
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (artist_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_partner_artist_snap_date
    ON public.spotify_partner_artist_snapshots (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_partner_artist_snap_artist
    ON public.spotify_partner_artist_snapshots (artist_id, snapshot_date DESC);

COMMENT ON TABLE public.spotify_partner_artist_snapshots IS
    'Snapshot diário de métricas do artista via Partner API: monthly_listeners, world_rank, top_cities. Complementa `artist_snapshots`.';

-- ============================================================
-- 3. Playlists "Descoberto em" (driving discovery)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.spotify_partner_discovered_on_snapshots (
    id BIGSERIAL PRIMARY KEY,
    artist_id TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    playlist_name TEXT,
    playlist_image_url TEXT,
    owner_id TEXT,
    owner_name TEXT,
    is_spotify_editorial BOOLEAN GENERATED ALWAYS AS (LOWER(COALESCE(owner_name,'')) = 'spotify') STORED,
    rank_position INTEGER,              -- ordem no response (1 = mais influente)
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (artist_id, playlist_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_partner_disc_on_artist_date
    ON public.spotify_partner_discovered_on_snapshots (artist_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_partner_disc_on_editorial
    ON public.spotify_partner_discovered_on_snapshots (is_spotify_editorial) WHERE is_spotify_editorial = true;

COMMENT ON TABLE public.spotify_partner_discovered_on_snapshots IS
    'Playlists que impulsionam streams do artista. Útil para CRM de curadores e bot detection.';

-- ============================================================
-- 4. Hashes GraphQL descobertos (observabilidade de mudanças)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.spotify_partner_hashes (
    operation_name TEXT PRIMARY KEY,
    sha256_hash TEXT NOT NULL,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    source TEXT DEFAULT 'js_bundle'     -- de onde foi extraído
);

COMMENT ON TABLE public.spotify_partner_hashes IS
    'Cache dos sha256Hash de persisted queries GraphQL. Atualizado pelo job discover-hashes. Detecta quando Spotify muda a API.';

-- ============================================================
-- 5. View amigável: daily streams por artista
-- ============================================================
CREATE OR REPLACE VIEW public.spotify_partner_daily_streams_view AS
SELECT
    ts.track_id,
    ts.playcount AS total_streams,
    ts.daily_streams,
    ts.snapshot_date,
    ts.source,
    CASE
        WHEN prev.playcount > 0
        THEN ROUND((ts.playcount - prev.playcount) * 100.0 / prev.playcount, 4)
        ELSE NULL
    END AS daily_change_pct
FROM public.spotify_partner_track_snapshots ts
LEFT JOIN public.spotify_partner_track_snapshots prev
    ON prev.track_id = ts.track_id
   AND prev.snapshot_date = ts.snapshot_date - INTERVAL '1 day';

-- ============================================================
-- 6. RLS
-- ============================================================
ALTER TABLE public.spotify_partner_track_snapshots      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.spotify_partner_artist_snapshots     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.spotify_partner_discovered_on_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.spotify_partner_hashes               ENABLE ROW LEVEL SECURITY;

-- Service role (collector, data-bridge com service key) escreve livremente.
-- Users autenticados leem. Anon não tem acesso.
CREATE POLICY partner_track_snap_service_write ON public.spotify_partner_track_snapshots
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY partner_track_snap_authenticated_read ON public.spotify_partner_track_snapshots
    FOR SELECT TO authenticated USING (true);

CREATE POLICY partner_artist_snap_service_write ON public.spotify_partner_artist_snapshots
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY partner_artist_snap_authenticated_read ON public.spotify_partner_artist_snapshots
    FOR SELECT TO authenticated USING (true);

CREATE POLICY partner_disc_on_service_write ON public.spotify_partner_discovered_on_snapshots
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY partner_disc_on_authenticated_read ON public.spotify_partner_discovered_on_snapshots
    FOR SELECT TO authenticated USING (true);

CREATE POLICY partner_hashes_service ON public.spotify_partner_hashes
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY partner_hashes_read ON public.spotify_partner_hashes
    FOR SELECT TO authenticated USING (true);

-- ============================================================
-- 7. Trigger para calcular daily_streams no INSERT/UPDATE
-- ============================================================
CREATE OR REPLACE FUNCTION public.spotify_partner_compute_daily_streams()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    prev_playcount BIGINT;
BEGIN
    SELECT playcount INTO prev_playcount
    FROM public.spotify_partner_track_snapshots
    WHERE track_id = NEW.track_id
      AND snapshot_date < NEW.snapshot_date
    ORDER BY snapshot_date DESC
    LIMIT 1;

    IF prev_playcount IS NOT NULL THEN
        NEW.daily_streams := NEW.playcount - prev_playcount;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_spotify_partner_daily_streams ON public.spotify_partner_track_snapshots;
CREATE TRIGGER trg_spotify_partner_daily_streams
    BEFORE INSERT OR UPDATE ON public.spotify_partner_track_snapshots
    FOR EACH ROW EXECUTE FUNCTION public.spotify_partner_compute_daily_streams();

COMMIT;
