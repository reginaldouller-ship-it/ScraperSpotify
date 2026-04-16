/**
 * Tipos compartilhados do Spotify Partner API client.
 *
 * A API GraphQL do Spotify Web Player retorna estruturas complexas e
 * parcialmente opacas (é uma API interna). Os tipos aqui cobrem o subset
 * que efetivamente parseamos.
 */

// ============================================================
// Auth
// ============================================================

export interface AnonymousToken {
  accessToken: string;
  expiresAtMs: number;
  isAnonymous: boolean;
  source: 'embed' | 'direct';
}

// ============================================================
// getAlbum
// ============================================================

export interface AlbumArtist {
  id: string;
  name: string;
}

export interface AlbumTrack {
  id: string;
  name: string;
  playcount: number;
  durationMs: number | null;
  discNumber: number | null;
  trackNumber: number | null;
  explicit: boolean | null;
  artists: AlbumArtist[];
}

export interface Album {
  id: string;
  name: string;
  artists: AlbumArtist[];
  tracks: AlbumTrack[];
  source: 'graphql_partner';
}

// ============================================================
// queryArtistOverview
// ============================================================

export interface TopCity {
  city: string | null;
  country: string | null;
  region: string | null;
  listeners: number | null;
}

export interface ArtistOverview {
  id: string;
  name: string;
  monthlyListeners: number | null;
  followers: number | null;
  worldRank: number | null;
  topCities: TopCity[];
  biography: string | null;
  popularity: number | null;
  source: 'graphql_partner';
}

// ============================================================
// queryArtistDiscoveredOn
// ============================================================

export interface DiscoveredOnPlaylist {
  id: string;
  uri: string;
  name: string;
  imageUrl: string | null;
  owner: {
    id: string;
    name: string;
  };
}

export interface DiscoveredOn {
  artistId: string;
  artistName: string;
  playlists: DiscoveredOnPlaylist[];
  source: 'graphql_partner';
}

// ============================================================
// queryArtistRelated
// ============================================================

export interface RelatedArtist {
  id: string;
  name: string;
  imageUrl: string | null;
}

// ============================================================
// Jobs (inputs)
// ============================================================

export interface EnrichPlaycountJobData {
  albumIds: string[];               // álbuns a refrescar playcount
  snapshotDate?: string;            // ISO date, default: hoje
}

export interface ArtistSnapshotJobData {
  artistId: string;
  includeDiscoveredOn?: boolean;    // default: true
  snapshotDate?: string;
}

// ============================================================
// Raw GraphQL response shapes (subsets que parseamos)
// ============================================================

export interface GraphQLResponse<T> {
  data?: T;
  errors?: Array<{ message: string; extensions?: unknown }>;
}

export interface GetAlbumData {
  albumUnion?: RawAlbum;
  album?: RawAlbum;
}

export interface RawAlbum {
  __typename?: string;
  uri?: string;
  name?: string;
  artists?: { items?: RawAlbumArtist[] };
  tracks?: { totalCount?: number; items?: RawAlbumTrackItem[] };
  tracksV2?: { totalCount?: number; items?: RawAlbumTrackItem[] };
}

export interface RawAlbumArtist {
  uri?: string;
  profile?: { name?: string };
}

export interface RawAlbumTrackItem {
  // Às vezes: { track: {...} }, outras vezes o track vem direto
  track?: RawTrack;
  uri?: string;
  name?: string;
  playcount?: string | number;
  discNumber?: number;
  trackNumber?: number;
  contentRating?: { label?: string };
  duration?: { totalMilliseconds?: number } | number;
  artists?: { items?: RawAlbumArtist[] };
}

export interface RawTrack extends RawAlbumTrackItem {}

export interface ArtistOverviewData {
  artistUnion?: RawArtistOverview;
  artist?: RawArtistOverview;
}

export interface RawArtistOverview {
  __typename?: string;
  uri?: string;
  profile?: {
    name?: string;
    biography?: { text?: string };
  };
  stats?: {
    monthlyListeners?: number;
    followers?: number;
    worldRank?: number;
    topCities?: { items?: RawTopCity[] };
  };
  popularity?: number;
}

export interface RawTopCity {
  city?: string;
  country?: string;
  region?: string;
  numberOfListeners?: number;
}

export interface DiscoveredOnData {
  artistUnion?: RawArtistWithRelated;
}

export interface RawArtistWithRelated {
  profile?: { name?: string };
  relatedContent?: {
    discoveredOnV2?: { items?: RawDiscoveredOnItem[] };
    discoveredOn?: { items?: RawDiscoveredOnItem[] };
    relatedArtists?: { items?: RawRelatedArtistItem[] };
  };
}

export interface RawDiscoveredOnItem {
  data?: RawPlaylist;
  uri?: string;
  name?: string;
  images?: { items?: Array<{ sources?: Array<{ url?: string }>; url?: string }> };
  ownerV2?: { data?: RawUser } | RawUser;
  owner?: RawUser;
}

export interface RawPlaylist {
  __typename?: string;
  uri?: string;
  name?: string;
  images?: { items?: Array<{ sources?: Array<{ url?: string }>; url?: string }> };
  ownerV2?: { data?: RawUser };
  owner?: RawUser;
}

export interface RawUser {
  uri?: string;
  name?: string;
}

export interface RawRelatedArtistItem {
  uri?: string;
  profile?: { name?: string };
  visuals?: { avatarImage?: { sources?: Array<{ url?: string }> } };
}
