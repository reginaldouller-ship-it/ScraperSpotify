/**
 * Job: Spotify Partner — Artist Snapshot
 *
 * Para 1 artist_id, busca queryArtistOverview + queryArtistDiscoveredOn
 * e salva:
 *   - 1 row em spotify_partner_artist_snapshots (métricas do artista)
 *   - N rows em spotify_partner_discovered_on_snapshots (playlists do dia)
 *
 * Roda barato: ~2 requests por artista. Ideal pra rodar diário
 * pra todos artistas na watchlist.
 */
import type { Job } from 'bullmq';
import type { SupabaseClient } from '@supabase/supabase-js';

import {
  HashOutdatedError,
  SpotifyPartnerAuth,
  SpotifyPartnerGraphQL,
  type ArtistSnapshotJobData,
} from '../partners/spotify-partner/index.js';

export interface ArtistSnapshotResult {
  artistId: string;
  artistName: string;
  overviewSaved: boolean;
  discoveredOnSaved: number;
  durationMs: number;
}

export async function artistSnapshotHandler(
  job: Job<ArtistSnapshotJobData>,
  supabase: SupabaseClient,
): Promise<ArtistSnapshotResult> {
  const { artistId, includeDiscoveredOn = true, snapshotDate } = job.data;
  if (!artistId) throw new Error('artistId ausente no job data');

  const snapshotDateStr = snapshotDate ?? new Date().toISOString().slice(0, 10);
  const startedAt = Date.now();

  const auth = new SpotifyPartnerAuth();
  const gql = new SpotifyPartnerGraphQL(auth);

  // 1. Overview
  const overview = await gql.getArtistOverview(artistId);
  const { error: overviewErr } = await supabase
    .from('spotify_partner_artist_snapshots')
    .upsert({
      artist_id: overview.id,
      artist_name: overview.name,
      monthly_listeners: overview.monthlyListeners,
      followers: overview.followers,
      world_rank: overview.worldRank,
      popularity: overview.popularity,
      top_cities: overview.topCities,
      biography: overview.biography,
      snapshot_date: snapshotDateStr,
    }, { onConflict: 'artist_id,snapshot_date' });

  if (overviewErr) {
    throw new Error(`Supabase upsert overview falhou: ${overviewErr.message}`);
  }

  // 2. Discovered on (opcional)
  let discoveredOnSaved = 0;
  if (includeDiscoveredOn) {
    try {
      const disc = await gql.getArtistDiscoveredOn(artistId);
      if (disc.playlists.length > 0) {
        const rows = disc.playlists.map((pl, idx) => ({
          artist_id: artistId,
          playlist_id: pl.id,
          playlist_name: pl.name,
          playlist_image_url: pl.imageUrl,
          owner_id: pl.owner.id,
          owner_name: pl.owner.name,
          rank_position: idx + 1,
          snapshot_date: snapshotDateStr,
        }));
        const { error: discErr } = await supabase
          .from('spotify_partner_discovered_on_snapshots')
          .upsert(rows, { onConflict: 'artist_id,playlist_id,snapshot_date' });
        if (discErr) {
          await job.log(`discovered_on upsert falhou: ${discErr.message}`);
        } else {
          discoveredOnSaved = rows.length;
        }
      }
    } catch (err) {
      if (err instanceof HashOutdatedError) throw err;
      await job.log(`discovered_on query falhou: ${(err as Error).message}`);
    }
  }

  return {
    artistId,
    artistName: overview.name,
    overviewSaved: true,
    discoveredOnSaved,
    durationMs: Date.now() - startedAt,
  };
}

export interface EnqueueArtistSnapshotParams {
  queue: { add: (name: string, data: ArtistSnapshotJobData, opts?: object) => Promise<unknown> };
  artistId: string;
  includeDiscoveredOn?: boolean;
}

export async function enqueueArtistSnapshotJob(params: EnqueueArtistSnapshotParams): Promise<void> {
  await params.queue.add(
    `artist-snapshot:${params.artistId}`,
    {
      artistId: params.artistId,
      includeDiscoveredOn: params.includeDiscoveredOn ?? true,
    },
    {
      attempts: 3,
      backoff: { type: 'exponential', delay: 30_000 },
      removeOnComplete: { age: 3600 * 24, count: 500 },
      removeOnFail: { age: 3600 * 24 * 7 },
    },
  );
}
