/**
 * Cliente da Spotify Partner GraphQL API.
 *
 * Expõe métodos tipados para as queries mais valiosas:
 *   - getAlbum            → playcount por track
 *   - getArtistOverview   → monthly_listeners, followers, world_rank, top_cities
 *   - getArtistDiscoveredOn  → playlists que impulsionam streams
 *   - getArtistRelated    → artistas relacionados (API oficial deprecou)
 *
 * Features:
 *   - Retry com backoff exponencial em 429 / 5xx / network errors
 *   - Rotação automática de User-Agent
 *   - Refresh de token em 401
 *   - HashOutdatedError tipado quando persisted query muda (sinal pra atualizar hashes)
 */
import { setTimeout as sleep } from 'node:timers/promises';

import type { SpotifyPartnerAuth } from './auth.js';
import { GRAPHQL_ENDPOINT, GRAPHQL_HASHES, USER_AGENTS, type OperationName } from './queries.js';
import type {
  Album,
  AlbumArtist,
  AlbumTrack,
  ArtistOverview,
  ArtistOverviewData,
  DiscoveredOn,
  DiscoveredOnData,
  DiscoveredOnPlaylist,
  GetAlbumData,
  GraphQLResponse,
  RawAlbumArtist,
  RawAlbumTrackItem,
  RawDiscoveredOnItem,
  RelatedArtist,
  TopCity,
} from './types.js';

export class SpotifyGraphQLError extends Error {
  constructor(message: string, public readonly status?: number, public readonly body?: string) {
    super(message);
    this.name = 'SpotifyGraphQLError';
  }
}

export class HashOutdatedError extends SpotifyGraphQLError {
  constructor(public readonly operation: string, body: string) {
    super(`Persisted query hash desatualizado para ${operation}`, 400, body);
    this.name = 'HashOutdatedError';
  }
}

export interface ClientOpts {
  /** Delay mínimo entre requests em ms (random jitter). Default: 0. */
  delayMinMs?: number;
  /** Delay máximo entre requests em ms. Default: 100. */
  delayMaxMs?: number;
  /** Retries em falhas transientes. Default: 3. */
  maxRetries?: number;
  /** Timeout por request em ms. Default: 20000. */
  timeoutMs?: number;
  /** Override de hashes (ex: vindo de `spotify_partner_hashes` no DB). */
  hashOverrides?: Partial<Record<OperationName, string>>;
}

export class SpotifyPartnerGraphQL {
  private consecutive429 = 0;

  constructor(private readonly auth: SpotifyPartnerAuth, private readonly opts: ClientOpts = {}) {}

  // ========== Public methods ==========

  async getAlbum(albumId: string, limit = 300): Promise<Album> {
    const data = await this.query<GetAlbumData>('getAlbum', {
      uri: `spotify:album:${albumId}`,
      locale: '',
      offset: 0,
      limit,
    });

    const album = data.albumUnion ?? data.album;
    if (!album) throw new SpotifyGraphQLError(`albumUnion vazio para ${albumId}`);

    const artistsRaw = album.artists?.items ?? [];
    const artists: AlbumArtist[] = artistsRaw.map(mapArtist);

    const trackContainer = album.tracks ?? album.tracksV2 ?? { items: [] };
    const items = trackContainer.items ?? [];

    const tracks: AlbumTrack[] = [];
    for (const it of items) {
      const tr = (it.track ?? it) as RawAlbumTrackItem;
      const trackId = uriId(tr.uri ?? '');
      if (!trackId) continue;

      tracks.push({
        id: trackId,
        name: tr.name ?? '',
        playcount: toInt(tr.playcount) ?? 0,
        durationMs: typeof tr.duration === 'object'
          ? tr.duration?.totalMilliseconds ?? null
          : (typeof tr.duration === 'number' ? tr.duration : null),
        discNumber: tr.discNumber ?? null,
        trackNumber: tr.trackNumber ?? null,
        explicit: typeof tr.contentRating === 'object'
          ? (tr.contentRating?.label ?? '').toUpperCase() === 'EXPLICIT'
          : null,
        artists: (tr.artists?.items ?? artistsRaw).map(mapArtist),
      });
    }

    return {
      id: albumId,
      name: album.name ?? '',
      artists,
      tracks,
      source: 'graphql_partner',
    };
  }

  async getArtistOverview(artistId: string): Promise<ArtistOverview> {
    const data = await this.query<ArtistOverviewData>('queryArtistOverview', {
      uri: `spotify:artist:${artistId}`,
      locale: '',
      includePrerelease: true,
    });
    const artist = data.artistUnion ?? data.artist;
    if (!artist) throw new SpotifyGraphQLError(`artistUnion vazio para ${artistId}`);

    const stats = artist.stats ?? {};
    const topCitiesRaw = stats.topCities?.items ?? [];
    const topCities: TopCity[] = topCitiesRaw.map(c => ({
      city: c.city ?? null,
      country: c.country ?? null,
      region: c.region ?? null,
      listeners: c.numberOfListeners ?? null,
    }));

    return {
      id: artistId,
      name: artist.profile?.name ?? '',
      monthlyListeners: stats.monthlyListeners ?? null,
      followers: stats.followers ?? null,
      worldRank: stats.worldRank ?? null,
      topCities,
      biography: artist.profile?.biography?.text ?? null,
      popularity: artist.popularity ?? null,
      source: 'graphql_partner',
    };
  }

  async getArtistDiscoveredOn(artistId: string): Promise<DiscoveredOn> {
    const data = await this.query<DiscoveredOnData>('queryArtistDiscoveredOn', {
      uri: `spotify:artist:${artistId}`,
    });
    const artist = data.artistUnion;
    if (!artist) throw new SpotifyGraphQLError(`artistUnion vazio para ${artistId}`);

    const related = artist.relatedContent ?? {};
    const discovered = related.discoveredOnV2 ?? related.discoveredOn ?? { items: [] };
    const items = discovered.items ?? [];

    const playlists: DiscoveredOnPlaylist[] = [];
    for (const raw of items) {
      const parsed = parseDiscoveredOnItem(raw);
      if (parsed) playlists.push(parsed);
    }

    return {
      artistId,
      artistName: artist.profile?.name ?? '',
      playlists,
      source: 'graphql_partner',
    };
  }

  async getArtistRelated(artistId: string): Promise<{ id: string; name: string; related: RelatedArtist[] }> {
    const data = await this.query<DiscoveredOnData>('queryArtistRelated', {
      uri: `spotify:artist:${artistId}`,
    });
    const artist = data.artistUnion;
    if (!artist) throw new SpotifyGraphQLError(`artistUnion vazio para ${artistId}`);

    const items = artist.relatedContent?.relatedArtists?.items ?? [];
    const related: RelatedArtist[] = items.map(it => ({
      id: uriId(it.uri ?? ''),
      name: it.profile?.name ?? '',
      imageUrl: it.visuals?.avatarImage?.sources?.[0]?.url ?? null,
    }));

    return { id: artistId, name: artist.profile?.name ?? '', related };
  }

  // ========== Core ==========

  private async query<T>(operation: OperationName, variables: Record<string, unknown>): Promise<T> {
    const hash = this.opts.hashOverrides?.[operation] ?? GRAPHQL_HASHES[operation];
    if (!hash) throw new SpotifyGraphQLError(`Hash não configurado para ${operation}`);

    const maxRetries = this.opts.maxRetries ?? 3;
    let lastErr: unknown;

    for (let attempt = 0; attempt < maxRetries; attempt++) {
      await this.delay();
      try {
        return await this.doRequest<T>(operation, variables, hash);
      } catch (err) {
        lastErr = err;
        if (err instanceof HashOutdatedError) throw err; // não faz sentido retry
        if (err instanceof SpotifyGraphQLError && err.status && err.status >= 400 && err.status < 500 && err.status !== 429) {
          throw err; // 4xx não-429 não é retryable
        }
        const backoffMs = Math.min(2 ** attempt * 1000, 30000);
        await sleep(backoffMs);
      }
    }
    throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
  }

  private async doRequest<T>(operation: OperationName, variables: Record<string, unknown>, hash: string): Promise<T> {
    const token = await this.auth.getToken();
    const url = buildUrl(operation, variables, hash);
    const timeoutMs = this.opts.timeoutMs ?? 20_000;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let resp: Response;
    try {
      resp = await fetch(url, {
        headers: {
          Authorization: `Bearer ${token}`,
          'User-Agent': USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)]!,
          Accept: 'application/json',
          'App-Platform': 'WebPlayer',
          Origin: 'https://open.spotify.com',
          Referer: 'https://open.spotify.com/',
          'Spotify-App-Version': '1.2.52.442',
        },
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    if (resp.status === 401) {
      this.auth.invalidate();
      // o próximo attempt do retry loop vai renovar
      throw new SpotifyGraphQLError('401 Unauthorized', 401);
    }

    if (resp.status === 429) {
      this.consecutive429 += 1;
      const retryAfter = Number(resp.headers.get('Retry-After')) || 30;
      await sleep(retryAfter * 1000);
      throw new SpotifyGraphQLError('429 Rate Limited', 429);
    }

    if (resp.status === 400) {
      const body = await resp.text();
      if (body.includes('PersistedQueryNotFound') || body.includes('persistedQueryNotFound')) {
        throw new HashOutdatedError(operation, body.slice(0, 500));
      }
      throw new SpotifyGraphQLError(`400 Bad Request em ${operation}`, 400, body.slice(0, 500));
    }

    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      throw new SpotifyGraphQLError(`HTTP ${resp.status} em ${operation}`, resp.status, body.slice(0, 200));
    }

    this.consecutive429 = 0;
    const json = await resp.json() as GraphQLResponse<T>;
    if (json.errors && json.errors.length > 0) {
      throw new SpotifyGraphQLError(`GraphQL errors: ${JSON.stringify(json.errors)}`);
    }
    if (!json.data) throw new SpotifyGraphQLError(`Response sem data em ${operation}`);
    return json.data;
  }

  private async delay(): Promise<void> {
    const min = this.opts.delayMinMs ?? 0;
    const max = this.opts.delayMaxMs ?? 100;
    if (max <= 0) return;
    const ms = min + Math.random() * Math.max(max - min, 0);
    if (ms > 0) await sleep(ms);
  }
}

// ========== Helpers ==========

function buildUrl(operation: string, variables: Record<string, unknown>, sha256Hash: string): string {
  const params = new URLSearchParams({
    operationName: operation,
    variables: JSON.stringify(variables),
    extensions: JSON.stringify({ persistedQuery: { version: 1, sha256Hash } }),
  });
  return `${GRAPHQL_ENDPOINT}?${params.toString()}`;
}

function uriId(uri: string): string {
  if (!uri) return '';
  const idx = uri.lastIndexOf(':');
  return idx >= 0 ? uri.slice(idx + 1) : uri;
}

function mapArtist(a: RawAlbumArtist): AlbumArtist {
  return { id: uriId(a.uri ?? ''), name: a.profile?.name ?? '' };
}

function toInt(v: string | number | undefined | null): number | null {
  if (v === undefined || v === null) return null;
  const n = typeof v === 'number' ? v : Number.parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}

function parseDiscoveredOnItem(raw: RawDiscoveredOnItem): DiscoveredOnPlaylist | null {
  const pl = raw.data ?? raw;
  const uri = pl.uri ?? '';
  if (!uri.startsWith('spotify:playlist:')) return null;

  const ownerObj = ('ownerV2' in pl ? pl.ownerV2 : pl.owner) as
    | { data?: { uri?: string; name?: string } }
    | { uri?: string; name?: string }
    | undefined;
  const ownerData = ownerObj && 'data' in ownerObj ? ownerObj.data : ownerObj;

  const imagesList = pl.images?.items ?? [];
  const firstImg = imagesList[0];
  let imageUrl: string | null = null;
  if (firstImg) {
    imageUrl = firstImg.sources?.[0]?.url ?? firstImg.url ?? null;
  }

  return {
    id: uriId(uri),
    uri,
    name: pl.name ?? '',
    imageUrl,
    owner: {
      id: uriId(ownerData?.uri ?? ''),
      name: ownerData?.name ?? '',
    },
  };
}
