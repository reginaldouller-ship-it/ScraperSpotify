"""Configurações do scraper."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Diretórios
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Banco de dados
DATABASE_PATH = os.getenv("DATABASE_PATH", str(DATA_DIR / "spotify_streams.db"))

# Endpoints
TOKEN_URL = "https://open.spotify.com/get_access_token?reason=transport&productType=web-player"
GRAPHQL_URL = "https://api-partner.spotify.com/pathfinder/v1/query"
EMBED_TRACK_URL = "https://open.spotify.com/embed/track/{track_id}"
EMBED_ALBUM_URL = "https://open.spotify.com/embed/album/{album_id}"

# ATENÇÃO: sha256Hash das persisted queries mudam. Atualizar periodicamente
# inspecionando a aba Network do Spotify Web Player (filtro: api-partner).
# Estes são hashes conhecidos publicamente (podem estar desatualizados —
# o scraper tem fallback para Embed API).
GRAPHQL_HASHES = {
    "ArtistConcerts": "ef53c43b865496b9890b7167eab1dc614a8949ef9451b3c41184ea888de8bd2b",
    "ArtistConcertsPageLocation": "320698465a352f0d0247ec8ed02471244106d4199820f99de4d0a785561c2b03",
    "accountAttributes": "3030aeca7614b9e00b728c91383fff23d1a7c2982929dc5c9db3dc35e2e5c0be",
    "areEntitiesInLibrary": "134337999233cc6fdd6b1e6dbf94841409f04a946c5c7b744b09ba0dfe5a85ed",
    "assistedCurationSearch": "f78953bf9207d73493c27284103f5aeb6e728876d5793851bf79bc706127ff70",
    "assistedCurationSearchAlbum": "e33489c81fdab1986d8b785fb9bf13993a2d5ff171190c575963f97e525870fe",
    "assistedCurationSearchArtist": "a562324cea8976b4d51f08cee971bb180ce9279c947d27e0814675220b160cf7",
    "centralisedStatePlayerOptions": "e2dcfcab470854d4d1c7cb1a851438f14fe0a94d57db7f0b9dde492559d5395d",
    "concertCount": "29be9d486e073a49268e13ed9e2d2180187e669fcb7a19b98011aca7ab61b141",
    "concertLocationsByLatLon": "8a059d072a17a1199feb21fe846271f1680eda87010c832852ced0c55c6c7c96",
    "decorateContextEpisodesOrChapters": "383de00240775c39a6afe0b1055dc562b2a3930894201f9762f3fc32a74971c7",
    "decorateContextTracks": "383de00240775c39a6afe0b1055dc562b2a3930894201f9762f3fc32a74971c7",
    "editablePlaylists": "d5c4b8096437dcc2ac9528c91dfcd299e35b747cda2f8f75d28f41f49c5092ba",
    "episodeSponsoredContent": "a5c1fe722b60c29ad247ea3df57ace52043382a7f080d525f58745db78a42618",
    "fetchEntitiesForRecentlyPlayed": "5bb408450626d595cb24363104b612e14f9b966430f599121696e8996ea03794",
    "fetchExtractedColors": "36e90fcaea00d47c695fce31874efeb2519b97d4cd0ee1abfb4f8dc9348596ea",
    "fetchLibraryTracks": "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240",
    "fetchPlaylist": "32b05e92e438438408674f95d0fdad8082865dc32acd55bd97f5113b8579092b",
    "fetchPlaylistContents": "32b05e92e438438408674f95d0fdad8082865dc32acd55bd97f5113b8579092b",
    "fetchPlaylistMetadata": "32b05e92e438438408674f95d0fdad8082865dc32acd55bd97f5113b8579092b",
    "getAlbum": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
    "getAlbumNameAndTracks": "8628ad33de3267d7bef516c76a746979a5f98891a2c9eaff3dfec828abdcd983",
    "getArtistDiscographyAll": "9380995a9d4663cbcb5113fff3b9f9d15a1eb885e2b84f4cdbd5e3d9bd35de34",
    "getArtistNameAndTracks": "0adaf1a1a8a94c7ed095639c4d9456d2b1cfac16ac511d5dd2b01b6dd89f748a",
    "getCommentsForEntity": "bba34fe5f2da3aaa25ab5c90eef1fe2036d325bf32e791ae462b637665185d83",
    "getDynamicColors": "f0f112945d6d745bd8ff790317bbf8d310036da75df33130490e9d6dc96c59d9",
    "getDynamicColorsByUris": "f0f112945d6d745bd8ff790317bbf8d310036da75df33130490e9d6dc96c59d9",
    "getEpisodeName": "508f9db2e7dc340c338950dc67a6045ee1406703646f23b760986fa689c239b1",
    "getEpisodeOrChapter": "3416929067571ac4b79db16716be3c6ea5f6265f7975a0ee94b1fc5ee1dc1e9d",
    "getLists": "0f40e72e0f2469e8d6f474161242af3feda7cf1c4d20785fd73cc2cc8c2dee5f",
    "getListsContents": "0f40e72e0f2469e8d6f474161242af3feda7cf1c4d20785fd73cc2cc8c2dee5f",
    "getListsMetadata": "0f40e72e0f2469e8d6f474161242af3feda7cf1c4d20785fd73cc2cc8c2dee5f",
    "getPodcastOrBookName": "631676b4cf1eb7c93d1133e3f1f17e5bfe8d6a5e2fb9560148bac61f1531f267",
    "getReactions": "0d209bf9507779887fe2b3032d1afd8f35de8425b01aead094698ff1abecda71",
    "getReplies": "a2018b23184ee9c8f355f5bcb0584aa3afbacaed6912195a367aa1bb807359f6",
    "getTrack": "612585ae06ba435ad26369870deaae23b5c8800a256cd8a57e08eddc25a37294",
    "getTrackName": "3dee761788854e8dd9239e13ce0d712da031fb8c2036f096a1c765062b410660",
    "getVideoTrackAssociatedAlbum": "e9ecdc49f7777062fc841415262d47c3927e09ab6e6845b420a373caec602812",
    "home": "23e37f2e58d82d567f27080101d36609009d8c3676457b1086cb0acc55b72a5d",
    "homePinnedSections": "23e37f2e58d82d567f27080101d36609009d8c3676457b1086cb0acc55b72a5d",
    "homeSection": "23e37f2e58d82d567f27080101d36609009d8c3676457b1086cb0acc55b72a5d",
    "isCurated": "e4ed1f91a2cc5415befedb85acf8671dc1a4bf3ca1a5b945a6386101a22e28a6",
    "isCuratedEntities": "af6bb0d2691f78f9169e1ba2dfed34a414bb4994e858f81487d6a26b95280566",
    "isFollowingUsers": "c00e0cb6c7766e7230fc256cf4fe07aec63b53d1160a323940fce7b664e95596",
    "libraryV3": "973e511ca44261fda7eebac8b653155e7caee3675abb4fb110cc1b8c78b091c3",
    "lookupEntity": "027903e8eb620517d49218421ddb2a4032e64c43ab0f9d015571a71ef2e31c6b",
    "npvPageContent": "28f282055d667fce7095bbd1597fb7f0f0621de4dab51a6e45bc28796c3683ad",
    "playlistPermissions": "f4c99a92059b896b9e4e567403abebe666c0625a36286f9c2bb93961374a75c6",
    "profileAttributes": "53bcb064f6cd18c23f752bc324a791194d20df612d8e1239c735144ab0399ced",
    "queryAlbumTrackUris": "a2a17981f8439ca1798f56260277d9d7800ec0ca7040053b564e0f975d8aa344",
    "queryAlbumTracks": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
    "queryArtistAppearsOn": "9a4bb7a20d6720fe52d7b47bc001cfa91940ddf5e7113761460b4a288d18a4c1",
    "queryArtistDiscographyAlbums": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
    "queryArtistDiscographyAll": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
    "queryArtistDiscographyCompilations": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
    "queryArtistDiscographyOverview": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
    "queryArtistDiscographySingles": "5e07d323febb57b4a56a42abbf781490e58764aa45feb6e3dc0591564fc56599",
    "queryArtistDiscoveredOn": "71c2392e4cecf6b48b9ad1311ae08838cbdabcfd189c6bf0c66c2430b8dcfdb1",
    "queryArtistFeaturing": "20842d6d9d2d28ef945984b68cb927bb33edd00eab84a8da1667def21f1f2c54",
    "queryArtistMinimal": "53d3f76582c49ad0a05dc685955f20dc2a5f2209b192e5446e5e4e623ce23a48",
    "queryArtistOverview": "7f86ff63e38c24973a2842b672abe44c910c1973978dc8a4a0cb648edef34527",
    "queryArtistPlaylists": "54f7e5a5a2af05b7dc98526df376a46c6b15c05440c8dfdc8f6cecb1a807eca7",
    "queryArtistRelated": "3d031d6cb22a2aa7c8d203d49b49df731f58b1e2799cc38d9876d58771aa66f3",
    "queryArtistRelatedVideos": "8958042d3dd127ec7882a7117fafa4df21af27ff1560af51e55061e8451de67b",
    "queryBookChapters": "8f342d1c624755901657fa65cbb80dd3bacbcca2f6d802f570ae3269d59a403e",
    "queryNpvArtist": "b2cedf7ed0f29c713567d97ed69b848c8387294edfe58a0e439a3a5669cc27bb",
    "queryNpvEpisode": "5460cf262b0eed4ca71be308a0e4991ac72184660ed504af77ee2440d79ba7b6",
    "queryNpvEpisodeChapters": "367f0e93a0d219ae6f5874bcc460201db0a43467ae94f16298931a704ac62ea6",
    "queryPodcastEpisodes": "06046f9b939d56c8eb7cdbb687da938de1164c006871aec91dc26e4dc7d8eb08",
    "queryShowMetadataV2": "aaad798a17a43c0f443c45d630a83df39d2ca1062a090c2e4fb045d6b00ab360",
    "queryTrackArtists": "ee2b038198f5e62c679c3996584d9249bbee55fe69fc212271c56492a022c798",
    "queryWhatsNewFeed": "d889c8c936ab192af8ced595427f5ba2acdf63478fdc0a181c8d477f8322630e",
    "recentSearches": "3f8b6efeae2444ce82a102c50e476374b6b14c4f97010d7fbe3fd15585c32869",
    "recents": "698be5892a3cc95331deebeff463d05dfdd5febf5254bea30b895b5a93dfb584",
    "searchConcertLocations": "43ededefcba8b3f519fd0c2d6c025dfeec9f742cf47d04a3c3711d95b27deda3",
    "searchSuggestions": "9fe3ad78e43a1684b3a9fabc741c5928928d4d30d7d8fd7fd193c7ebb4a544f4",
    "seoRecommendedTrackPlaylistDesktop": "2121830f81030dea46648d65a05c430cd822f08db6aaa18c7f35cc9b93c22396",
    "similarAlbumsBasedOnThisTrack": "1d1f93a737498adca2c892c73af87fc0b052afe4e1a33c989540c32413dfae17",
    "smartShuffle": "3384085be84fbf2f855b024f99bc06cded1c0fd71af3a8fb8abb84e9656faba2",
    "userLocation": "079939378ca79b67c6d047be9152ea940d21f10bbfa2f5d4cf4d8320d87774c2",
    "whatsNewFeedNewItems": "d889c8c936ab192af8ced595427f5ba2acdf63478fdc0a181c8d477f8322630e",
}

# Rate limiting
#
# Defaults calibrados pelo stress test (2026-04-15): 474 req, ZERO 429s,
# latência p50=219ms. Teto sequencial natural = ~4.4 req/s (limitado por
# latência de rede, não pelo Spotify).
#
# Com delay 0-0.1s + latência ~220ms = ~3.7-4.5 req/s por worker, seguro.
# Pode sobrescrever via env se quiser ser mais conservador (ex: 0.3/0.5).
GRAPHQL_DELAY_MIN = float(os.getenv("GRAPHQL_DELAY_MIN", "0.0"))
GRAPHQL_DELAY_MAX = float(os.getenv("GRAPHQL_DELAY_MAX", "0.1"))
EMBED_DELAY_MIN = float(os.getenv("EMBED_DELAY_MIN", "0.2"))
EMBED_DELAY_MAX = float(os.getenv("EMBED_DELAY_MAX", "0.6"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_FACTOR = float(os.getenv("BACKOFF_FACTOR", "2.0"))
RATE_LIMIT_PAUSE_SECONDS = int(os.getenv("RATE_LIMIT_PAUSE_SECONDS", "300"))
CONSECUTIVE_429_THRESHOLD = 3

# Concorrência padrão em scripts que suportam workers (populate_from_artist).
# Com 3 workers + delay 0-0.1s aggregate ≈ 10-12 req/s. Passou do tested ceiling
# (4.4 req/s do stress test de 1 worker), mas dentro do que a maioria dos
# scraping reports comunitários mostra como sustentável. Se aparecer 429 em
# escala, reduzir pra 1-2.
DEFAULT_WORKERS = int(os.getenv("DEFAULT_WORKERS", "3"))

# Token
TOKEN_REFRESH_MARGIN_SECONDS = 300  # renovar 5min antes de expirar
TOKEN_ROTATION_REQUESTS = 100  # renovar após N requests

# HTTP
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20.0"))

# User Agents para rotação
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# App tokens / feature flags
CLIENT_TOKEN_URL = "https://clienttoken.spotify.com/v1/clienttoken"

# Fallback de token: extrair accessToken do __NEXT_DATA__ de uma página de embed.
# Usamos um álbum bem conhecido e estável (Pink Floyd - The Dark Side of the Moon)
# porque é improvável que seja removido e tem tráfego alto → páginas sempre em cache.
TOKEN_FALLBACK_ALBUM_ID = os.getenv("TOKEN_FALLBACK_ALBUM_ID", "4LH4d3cOWNNsVw41Gqt2kv")

# Em IPs residenciais brasileiros o endpoint /get_access_token retorna 403
# consistentemente. Por padrão pulamos direto para o fallback de embed (que
# sempre funciona). Defina TRY_DIRECT_TOKEN_ENDPOINT=1 para tentar o endpoint
# direto primeiro (útil em VPS ou outros ranges de IP).
TRY_DIRECT_TOKEN_ENDPOINT = os.getenv("TRY_DIRECT_TOKEN_ENDPOINT", "0") == "1"
