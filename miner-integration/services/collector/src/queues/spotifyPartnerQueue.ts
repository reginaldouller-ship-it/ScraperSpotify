/**
 * Registro das queues BullMQ do Spotify Partner API.
 *
 * Duas queues:
 *   - spotify-partner:enrich-playcount  (getAlbum em batch, pesado)
 *   - spotify-partner:artist-snapshot   (overview + discovered_on, leve)
 *
 * Rodar este módulo ao bootar o collector:
 *
 *   import { registerSpotifyPartnerQueues } from './queues/spotifyPartnerQueue.js';
 *   const { worker1, worker2 } = registerSpotifyPartnerQueues(supabase, redisConnection);
 */
import { Queue, Worker, type ConnectionOptions, type Job } from 'bullmq';
import type { SupabaseClient } from '@supabase/supabase-js';

import {
  enrichPlaycountHandler,
  type EnrichPlaycountResult,
} from '../jobs/spotifyPartnerEnrichPlaycount.js';
import {
  artistSnapshotHandler,
  type ArtistSnapshotResult,
} from '../jobs/spotifyPartnerArtistSnapshot.js';
import type {
  ArtistSnapshotJobData,
  EnrichPlaycountJobData,
} from '../partners/spotify-partner/index.js';

export const QUEUE_NAMES = {
  enrichPlaycount: 'spotify-partner:enrich-playcount',
  artistSnapshot: 'spotify-partner:artist-snapshot',
} as const;

export interface SpotifyPartnerInfra {
  enrichPlaycountQueue: Queue<EnrichPlaycountJobData, EnrichPlaycountResult>;
  artistSnapshotQueue: Queue<ArtistSnapshotJobData, ArtistSnapshotResult>;
  enrichPlaycountWorker: Worker<EnrichPlaycountJobData, EnrichPlaycountResult>;
  artistSnapshotWorker: Worker<ArtistSnapshotJobData, ArtistSnapshotResult>;
  closeAll: () => Promise<void>;
}

export function registerSpotifyPartnerQueues(
  supabase: SupabaseClient,
  connection: ConnectionOptions,
): SpotifyPartnerInfra {
  const enrichPlaycountQueue = new Queue<EnrichPlaycountJobData, EnrichPlaycountResult>(
    QUEUE_NAMES.enrichPlaycount,
    { connection },
  );

  const artistSnapshotQueue = new Queue<ArtistSnapshotJobData, ArtistSnapshotResult>(
    QUEUE_NAMES.artistSnapshot,
    { connection },
  );

  // Workers — concorrência escolhida pra balancear throughput vs não sobrecarregar
  // o Spotify Partner API. Ajustar via env se precisar.
  const enrichPlaycountWorker = new Worker<EnrichPlaycountJobData, EnrichPlaycountResult>(
    QUEUE_NAMES.enrichPlaycount,
    async (job: Job<EnrichPlaycountJobData>) => enrichPlaycountHandler(job, supabase),
    {
      connection,
      concurrency: Number(process.env.SPOTIFY_PARTNER_PLAYCOUNT_CONCURRENCY ?? 2),
    },
  );

  const artistSnapshotWorker = new Worker<ArtistSnapshotJobData, ArtistSnapshotResult>(
    QUEUE_NAMES.artistSnapshot,
    async (job: Job<ArtistSnapshotJobData>) => artistSnapshotHandler(job, supabase),
    {
      connection,
      concurrency: Number(process.env.SPOTIFY_PARTNER_ARTIST_CONCURRENCY ?? 4),
    },
  );

  // Logs de falhas / HashOutdated → alertar ops
  enrichPlaycountWorker.on('failed', (job, err) => {
    const isHashOutdated = err.name === 'HashOutdatedError';
    // eslint-disable-next-line no-console
    console.error(
      '[spotify-partner:enrich-playcount] failed',
      JSON.stringify({
        jobId: job?.id,
        albumIds: job?.data.albumIds?.length,
        err: err.message,
        hashOutdated: isHashOutdated,
      }),
    );
  });

  artistSnapshotWorker.on('failed', (job, err) => {
    // eslint-disable-next-line no-console
    console.error(
      '[spotify-partner:artist-snapshot] failed',
      JSON.stringify({ jobId: job?.id, artistId: job?.data.artistId, err: err.message }),
    );
  });

  return {
    enrichPlaycountQueue,
    artistSnapshotQueue,
    enrichPlaycountWorker,
    artistSnapshotWorker,
    closeAll: async () => {
      await Promise.all([
        enrichPlaycountWorker.close(),
        artistSnapshotWorker.close(),
        enrichPlaycountQueue.close(),
        artistSnapshotQueue.close(),
      ]);
    },
  };
}

// ==========================================================================
// Scheduler: agenda snapshots diários via repeatable jobs
// ==========================================================================

export interface SchedulerConfig {
  /** IDs dos artistas a snapshotar diariamente (vindos da watchlist). */
  artistIds: string[];
  /** Horário UTC pra disparar (default: 03:00 UTC = 00:00 BRT). */
  cronExpression?: string;
}

export async function scheduleDailyArtistSnapshots(
  queue: Queue<ArtistSnapshotJobData>,
  config: SchedulerConfig,
): Promise<void> {
  const cron = config.cronExpression ?? '0 3 * * *';
  for (const artistId of config.artistIds) {
    await queue.add(
      `daily-snapshot:${artistId}`,
      { artistId, includeDiscoveredOn: true },
      {
        repeat: { pattern: cron },
        jobId: `daily-snapshot:${artistId}`, // único — previne duplicatas
        removeOnComplete: { age: 3600 * 24, count: 500 },
      },
    );
  }
}
