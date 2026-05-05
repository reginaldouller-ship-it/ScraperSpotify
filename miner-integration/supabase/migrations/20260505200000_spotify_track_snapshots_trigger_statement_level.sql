-- Migration: troca trigger trg_propagate_latest_playcount de FOR EACH ROW
-- para FOR EACH STATEMENT, eliminando overhead de 500 trigger calls por
-- batch de UPSERT no sync_from_supabase.py.
--
-- Causa raiz: a função antiga rodava 1× por linha. Com batch de 500 rows
-- × 9 índices em spotify_tracks, o tempo agregado do batch ultrapassava
-- statement_timeout=8s do role authenticator do PostgREST.
--
-- Solução: trigger AFTER INSERT/UPDATE FOR EACH STATEMENT com
-- REFERENCING NEW TABLE — roda 1× por statement com UPDATE em massa.
-- Postgres 10+ exige triggers separados por evento quando usa NEW TABLE.
--
-- Smoke test (500 rows UPSERT): 3.34s — bem dentro do timeout de 8s.
--
-- Rollback: a função antiga foi renomeada (não dropada). Para reverter:
--
--   DROP TRIGGER trg_propagate_latest_playcount_ins ON public.spotify_track_snapshots;
--   DROP TRIGGER trg_propagate_latest_playcount_upd ON public.spotify_track_snapshots;
--   DROP FUNCTION public.spotify_tracks_propagate_latest_playcount_stmt();
--   ALTER FUNCTION public._legacy_spotify_tracks_propagate_latest_playcount_row()
--     RENAME TO spotify_tracks_propagate_latest_playcount;
--   CREATE TRIGGER trg_propagate_latest_playcount
--   AFTER INSERT OR UPDATE ON public.spotify_track_snapshots
--   FOR EACH ROW EXECUTE FUNCTION public.spotify_tracks_propagate_latest_playcount();

-- 1) Drop trigger antigo (a função fica preservada via rename abaixo)
DROP TRIGGER IF EXISTS trg_propagate_latest_playcount ON public.spotify_track_snapshots;

-- 2) Preservar função antiga renomeada — disponível pra rollback rápido
ALTER FUNCTION public.spotify_tracks_propagate_latest_playcount()
  RENAME TO _legacy_spotify_tracks_propagate_latest_playcount_row;

-- 3) Função nova: statement-level com UPDATE em massa via NEW TABLE
CREATE OR REPLACE FUNCTION public.spotify_tracks_propagate_latest_playcount_stmt()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $function$
BEGIN
  -- Para cada track no batch, pega o snapshot mais recente (DISTINCT ON)
  -- e propaga pra spotify_tracks SE a data do batch >= latest_playcount_date.
  -- Isso preserva a semântica do trigger antigo (não regride dados).
  UPDATE public.spotify_tracks st
  SET latest_playcount = src.playcount,
      latest_playcount_date = src.date
  FROM (
    SELECT DISTINCT ON (spotify_track_id)
      spotify_track_id, playcount, date
    FROM new_rows
    WHERE playcount IS NOT NULL
    ORDER BY spotify_track_id, date DESC
  ) AS src
  WHERE st.spotify_id = src.spotify_track_id
    AND (st.latest_playcount_date IS NULL OR st.latest_playcount_date <= src.date);
  RETURN NULL; -- statement-level trigger ignora retorno
END;
$function$;

-- 4) Triggers statement-level (separados por evento — exigência do Postgres
-- quando usa REFERENCING NEW TABLE). Ambos chamam a mesma função.
CREATE TRIGGER trg_propagate_latest_playcount_ins
AFTER INSERT ON public.spotify_track_snapshots
REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT
EXECUTE FUNCTION public.spotify_tracks_propagate_latest_playcount_stmt();

CREATE TRIGGER trg_propagate_latest_playcount_upd
AFTER UPDATE ON public.spotify_track_snapshots
REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT
EXECUTE FUNCTION public.spotify_tracks_propagate_latest_playcount_stmt();

COMMENT ON FUNCTION public.spotify_tracks_propagate_latest_playcount_stmt()
  IS 'Statement-level trigger que propaga latest_playcount de spotify_track_snapshots para spotify_tracks. Substituiu a versão FOR EACH ROW em 2026-05-05 para evitar statement_timeout em batches de UPSERT (>500 rows).';

COMMENT ON FUNCTION public._legacy_spotify_tracks_propagate_latest_playcount_row()
  IS 'DEPRECATED: versão FOR EACH ROW do trigger trg_propagate_latest_playcount. Mantida para rollback rápido via rename. Não está em uso desde 2026-05-05.';
