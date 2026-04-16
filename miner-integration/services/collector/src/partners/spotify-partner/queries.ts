/**
 * Hashes das persisted queries GraphQL do Spotify Web Player.
 *
 * Validados em 2026-04-15 via bundle web-player.a4ad69de.js. Estes hashes
 * mudam eventualmente. Quando isso acontecer, o client vai retornar
 * `PersistedQueryNotFound`. Rodar `pnpm --filter @miner/collector discover-hashes`
 * pra atualizar automaticamente.
 *
 * A tabela `spotify_partner_hashes` no Supabase pode armazenar versões
 * dinâmicas — fallback dessa constante.
 */

export const GRAPHQL_HASHES = {
  getAlbum: 'b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10',
  queryArtistOverview: '7f86ff63e38c24973a2842b672abe44c910c1973978dc8a4a0cb648edef34527',
  getTrack: '612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294',
  queryArtistDiscographyAll: '5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599',
  queryArtistAppearsOn: '9a4bb7a20d6720fe52d7b47bc001cfa91940ddf5e7113761460b4a288d18a4c1',
  queryArtistDiscoveredOn: '71c2392e4cecf6b48b9ad1311ae08838cbdabcfd189c6bf0c66c2430b8dcfdb1',
  queryArtistRelated: '3d031d6cb22a2aa7c8d203d49b49df731f58b1e2799cc38d9876d58771aa66f3',
  queryArtistPlaylists: '54f7e5a5a2af05b7dc98526df376a46c6b15c05440c8dfdc8f6cecb1a807eca7',
  fetchPlaylist: '32b05e92e438438408674f95d0fdad8082865dc32acd55bd97f5113b8579092b',
  similarAlbumsBasedOnThisTrack: '1d1f93a737498adca2c892c73af87fc0b052afe4e1a33c989540c32413dfae17',
} as const;

export type OperationName = keyof typeof GRAPHQL_HASHES;

export const GRAPHQL_ENDPOINT = 'https://api-partner.spotify.com/pathfinder/v1/query';

export const USER_AGENTS = [
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
] as const;

/** Álbum público estável usado como fonte de token fallback (embed). */
export const TOKEN_FALLBACK_ALBUM_ID = '4LH4d3cOWNNsVw41Gqt2kv'; // Pink Floyd — Dark Side of the Moon
