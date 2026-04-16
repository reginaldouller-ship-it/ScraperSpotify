/**
 * Spotify Partner API — barrel export.
 *
 * Uso típico no collector:
 *
 *   import { SpotifyPartnerAuth, SpotifyPartnerGraphQL } from '@/partners/spotify-partner';
 *
 *   const auth = new SpotifyPartnerAuth();
 *   const gql = new SpotifyPartnerGraphQL(auth);
 *   const album = await gql.getAlbum(albumId);
 *   // album.tracks[].playcount está populado
 */
export { SpotifyPartnerAuth } from './auth.js';
export {
  SpotifyPartnerGraphQL,
  SpotifyGraphQLError,
  HashOutdatedError,
  type ClientOpts,
} from './graphql.js';
export { discoverHashes, syncHashesToStorage, type HashStorage } from './hash-discover.js';
export { GRAPHQL_HASHES, type OperationName } from './queries.js';
export type {
  Album,
  AlbumTrack,
  AlbumArtist,
  ArtistOverview,
  TopCity,
  DiscoveredOn,
  DiscoveredOnPlaylist,
  RelatedArtist,
  EnrichPlaycountJobData,
  ArtistSnapshotJobData,
} from './types.js';
