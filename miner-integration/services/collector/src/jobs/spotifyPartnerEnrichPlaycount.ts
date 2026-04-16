/**
 * Job: Spotify Partner — Enrich Playcount
 *
 * Para uma lista de album_ids (vinda do pipeline oficial que já descobriu
 * todos os álbuns do artista), faz `getAlbum` em cada um e salva snapshot
 * diário de playcount por track em `spotify_partner_track_snapshots`.
 *
 * Não chama endpoints da API oficial do Spotify. Roda em cota separada.
 *
 * Entrada: { albumIds: string[], snapshotDate?: string }
 * Saída:   { snapshotsWritten: number, albumsFailed: number, durationMs: number }
 */
import type { Job } from 'bullmq';
import type { SupabaseClient } from '@supabase/supabase-js';

import {
  HashOutdatedError,
  SpotifyGraphQLError,
  SpotifyPartnerAuth,
  SpotifyPartnerGraphQL,
  type EnrichPlaycountJobData,
} from '../partners/spotify-partner/index.js';

const CONCURRENCY = 3; // workers paralelos por job (stress test: 4.4 req/s em 1 worker)

export interface EnrichPlaycountResult {
  snapshotsWritten: number;
  albumsOk: number;
  albumsFailed: number;
  tracksNoPlaycount: number;
  durationMs: number;
  hashOutdated: boolean;
}

export async function enrichPlaycountHandler(
  job: Job<EnrichPlaycountJobData>,
  supabase: SupabaseClient,
): Promise<EnrichPlaycountResult> {
  const { albumIds, snapshotDate } = job.data;
  if (!albumIds || albumIds.length === 0) {
    throw new Error('albumIds vazio no job data');
  }

  const snapshotDateStr = snapshotDate ?? new Date().toISOString().slice(0, 10);
  const startedAt = Date.now();

  const auth = new SpotifyPartnerAuth();
  const gql = new SpotifyPartnerGraphQL(auth, { delayMinMs: 0, delayMaxMs: 100 });

  let snapshotsWritten = 0;
  let albumsOk = 0;
  let albumsFailed = 0;
  let tracksNoPlaycount = 0;
  let hashOutdated = false;

  // Worker pool manual (sem dep externa)
  let cursor = 0;
  async function worker(): Promise<void> {
    while (cursor < albumIds.length) {
      const index = cursor++;
      const albumId = albumIds[index]!;

      try {
        const album = await gql.getAlbum(albumId);
        const rows = album.tracks.map(t => ({
          track_id: t.id,
          playcount: t.playcount,
          snapshot_date: snapshotDateStr,
          source: 'graphql_partner',
        }));

        const tracksWithPlaycount = rows.filter(r => Number.isFinite(r.playcount));
        tracksNoPlaycount += rows.length - tracksWithPlaycount.length;

        if (tracksWithPlaycount.length > 0) {
          const { error } = await supabase
            .from('spotify_partner_track_snapshots')
            .upsert(tracksWithPlaycount, { onConflict: 'track_id,snapshot_date' });

          if (error) {
            throw new Error(`Supabase upsert falhou em album ${albumId}: ${error.message}`);
          }
          snapshotsWritten += tracksWithPlaycount.length;
        }
        albumsOk += 1;
      } catch (err) {
        albumsFailed += 1;
        if (err instanceof HashOutdatedError) {
          hashOutdated = true;
          // sinaliza pra rodar discover-hashes; não adianta continuar
          throw err;
        }
        // log local — BullMQ vai capturar e aplicar retry policy
        await job.log(`album ${albumId} falhou: ${(err as Error).message}`);
      }

      // progress report (BullMQ UI)
      if (index % 10 === 0) {
        await job.updateProgress(Math.round((index / albumIds.length) * 100));
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));

  return {
    snapshotsWritten,
    albumsOk,
    albumsFailed,
    tracksNoPlaycount,
    durationMs: Date.now() - startedAt,
    hashOutdated,
  };
}

/**
 * Helper pra invocar este job a partir de outro pipeline (ex: depois que o
 * job oficial terminar a descoberta de álbuns).
 */
export interface EnqueuePlaycountParams {
  queue: { add: (name: string, data: EnrichPlaycountJobData, opts?: object) => Promise<unknown> };
  artistId: string;
  albumIds: string[];
  snapshotDate?: string;
}

export async function enqueuePlaycountJob(params: EnqueuePlaycountParams): Promise<void> {
  const jobName = `enrich-playcount:${params.artistId}`;
  await params.queue.add(jobName, {
    albumIds: params.albumIds,
    snapshotDate: params.snapshotDate,
  }, {
    attempts: 3,
    backoff: { type: 'exponential', delay: 60_000 },
    removeOnComplete: { age: 3600 * 24, count: 200 },
    removeOnFail: { age: 3600 * 24 * 7 },
  });
}
