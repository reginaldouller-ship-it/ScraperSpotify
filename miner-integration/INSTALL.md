# Miner — Integração Spotify Partner API

Bundle pronto-pra-colar no monorepo Miner. Adiciona enrichment de **playcount**, **monthly listeners**, **world rank**, **top cities** e **discovered on** em cima do pipeline oficial que você já tem rodando.

## Princípio

**Não substitui nada.** A API oficial continua fazendo tudo o que fazia (descoberta de álbuns, popularity, metadata). Esta camada é apenas um **enrichment** que:

1. Consome os `album_ids` que o pipeline oficial já descobriu e salvou
2. Chama `getAlbum` na Partner API (GraphQL) pra pegar o **playcount** por track
3. Chama `queryArtistOverview` pra pegar **monthly_listeners**, **followers**, **world_rank**, **top_cities**
4. Grava snapshots em tabelas separadas (prefixo `spotify_partner_*`)

Cotas separadas: não consome da rate limit oficial (`3500 req/dia por client_id`).

## Estrutura a copiar

```
miner-integration/
├── supabase/migrations/
│   └── 20260416000000_spotify_partner_api.sql    → supabase/migrations/
├── services/collector/src/partners/spotify-partner/
│   ├── auth.ts                                   → services/collector/src/partners/spotify-partner/
│   ├── graphql.ts                                   (criar diretório partners/ se não existir)
│   ├── queries.ts
│   ├── types.ts
│   ├── hash-discover.ts
│   └── index.ts
├── services/collector/src/jobs/
│   ├── spotifyPartnerEnrichPlaycount.ts          → services/collector/src/jobs/
│   └── spotifyPartnerArtistSnapshot.ts
└── services/collector/src/queues/
    └── spotifyPartnerQueue.ts                    → services/collector/src/queues/
```

## Passo a passo

### 1. Copiar arquivos

```bash
# Na raiz do monorepo
cp miner-integration/supabase/migrations/*.sql supabase/migrations/
cp -r miner-integration/services/collector/src/partners services/collector/src/
cp miner-integration/services/collector/src/jobs/spotifyPartner*.ts services/collector/src/jobs/
cp miner-integration/services/collector/src/queues/spotifyPartnerQueue.ts services/collector/src/queues/
```

### 2. Rodar migration

```bash
pnpm supabase migration up                # ou via Supabase Studio
```

Cria 4 tabelas novas com RLS habilitado:
- `spotify_partner_track_snapshots` (playcount diário)
- `spotify_partner_artist_snapshots` (métricas de artista diárias)
- `spotify_partner_discovered_on_snapshots` (playlists driving discovery)
- `spotify_partner_hashes` (cache de hashes GraphQL, observabilidade)

Mais uma view `spotify_partner_daily_streams_view` com cálculo de `daily_change_pct`, e um trigger que computa `daily_streams` automaticamente no INSERT.

### 3. Regenerar tipos TypeScript do Supabase

```bash
pnpm db:types
```

Isso atualiza `packages/db` com as novas tabelas. Se não quiser usar `@miner/db` nos jobs, os jobs já funcionam — fiz tipagem defensiva no client.

### 4. Registrar queues no bootstrap do collector

No arquivo onde o collector inicia (provavelmente `services/collector/src/index.ts` ou `bootstrap.ts`):

```typescript
import { registerSpotifyPartnerQueues, scheduleDailyArtistSnapshots } from './queues/spotifyPartnerQueue.js';

// ... bootstrap do Redis + Supabase já existente ...

const partnerInfra = registerSpotifyPartnerQueues(supabase, redisConnection);

// Agenda snapshot diário pra artistas da watchlist (ou outra fonte)
const watchlistArtistIds = await loadWatchlistArtists(supabase); // use sua lógica existente
await scheduleDailyArtistSnapshots(partnerInfra.artistSnapshotQueue, {
  artistIds: watchlistArtistIds,
  cronExpression: '0 3 * * *', // 03:00 UTC = 00:00 BRT
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  await partnerInfra.closeAll();
});
```

### 5. Hookar o enrich de playcount no pipeline oficial existente

Onde o job oficial termina de descobrir álbuns novos de um artista:

```typescript
import { enqueuePlaycountJob } from './jobs/spotifyPartnerEnrichPlaycount.js';

// depois que o pipeline oficial salvou artists + albums + tracks:
await enqueuePlaycountJob({
  queue: partnerInfra.enrichPlaycountQueue,
  artistId: artist.id,
  albumIds: discoveredAlbumIds, // os mesmos album_ids que você já tem
});
```

### 6. Variáveis de ambiente (opcionais)

No `.env` do collector, sobrescreva defaults se precisar:

```bash
# Concorrência dos workers (default: 2 e 4 respectivamente)
SPOTIFY_PARTNER_PLAYCOUNT_CONCURRENCY=2
SPOTIFY_PARTNER_ARTIST_CONCURRENCY=4

# Testar endpoint direto /get_access_token antes do fallback embed (default: false)
TRY_DIRECT_TOKEN_ENDPOINT=0
```

Nenhum secret novo é necessário. Partner API usa token anônimo do Web Player.

### 7. Job de discover hashes (semanal)

Quando o Spotify atualiza a API, as persisted queries quebram com `HashOutdatedError`. Pra evitar, rodar semanalmente:

```typescript
import { discoverHashes } from './partners/spotify-partner/index.js';

// Cron semanal via BullMQ:
partnerInfra.artistSnapshotQueue.add(
  'discover-hashes',
  {} as never,
  { repeat: { pattern: '0 4 * * 0' }, jobId: 'discover-hashes' },
);

// Handler separado:
async function discoverHashesHandler(): Promise<void> {
  const hashes = await discoverHashes();
  const rows = Object.entries(hashes).map(([operation_name, sha256_hash]) => ({
    operation_name,
    sha256_hash,
    source: 'js_bundle',
  }));
  await supabase.from('spotify_partner_hashes').upsert(rows, { onConflict: 'operation_name' });
}
```

(Se quiser, posso gerar esse handler completo — avisa.)

## Consultas úteis no Supabase

### Top tracks por crescimento diário (último dia com dado)

```sql
SELECT
    ds.track_id,
    h.name AS track_name,         -- assumindo join com track_history ou tracks do pipeline oficial
    ds.total_streams,
    ds.daily_streams,
    ds.daily_change_pct
FROM spotify_partner_daily_streams_view ds
LEFT JOIN tracks h ON h.spotify_id = ds.track_id
WHERE ds.snapshot_date = (SELECT MAX(snapshot_date) FROM spotify_partner_track_snapshots)
  AND ds.daily_streams IS NOT NULL
ORDER BY ds.daily_streams DESC
LIMIT 50;
```

### Evolução de monthly listeners de um artista

```sql
SELECT snapshot_date, monthly_listeners, world_rank
FROM spotify_partner_artist_snapshots
WHERE artist_id = '7FNnA9vBm6EKceENgCGRMb'
ORDER BY snapshot_date DESC
LIMIT 30;
```

### Playlists editoriais Spotify que hoje driving discovery de um artista

```sql
SELECT playlist_name, rank_position
FROM spotify_partner_discovered_on_snapshots
WHERE artist_id = '7FNnA9vBm6EKceENgCGRMb'
  AND snapshot_date = (SELECT MAX(snapshot_date) FROM spotify_partner_discovered_on_snapshots WHERE artist_id = '7FNnA9vBm6EKceENgCGRMb')
  AND is_spotify_editorial = TRUE
ORDER BY rank_position;
```

## Performance esperada

Baseado em stress test (2026-04-15, 474 requests, **zero 429s**, latency p50 = 219ms):

| Cenário | Requests | Tempo (3 workers paralelos) |
|---|---|---|
| 1 artista, 50 álbuns | 50 | ~6s |
| 1 artista, 200 álbuns (Anitta) | 200 | ~25s |
| 1 artista, 500 álbuns (Taylor Swift) | 500 | ~60s |
| 100 artistas da watchlist (snapshot diário de overview) | 100 | ~13s |

## Troubleshooting

### `HashOutdatedError` em produção

Spotify atualizou a API. Rodar manualmente:

```typescript
import { discoverHashes } from './partners/spotify-partner/index.js';
const hashes = await discoverHashes();
console.log(hashes);
// copiar e atualizar config/queries.ts ou a tabela spotify_partner_hashes
```

### Muitos 429 repetidos

Reduzir `SPOTIFY_PARTNER_PLAYCOUNT_CONCURRENCY=1` no env. Se persistir, provavelmente o IP foi flagged — considerar rodar o collector via proxy residencial.

### Supabase upsert dando conflict sem UPDATE

Certificar que a migration criou os UNIQUE constraints. Pode checar com:
```sql
SELECT conname, conrelid::regclass, contype
FROM pg_constraint
WHERE conrelid::regclass::text LIKE 'spotify_partner_%';
```

## Convenções seguidas

- ✅ TypeScript strict (nada de `any`, usamos `unknown` + type guards)
- ✅ RLS habilitado em todas tabelas novas
- ✅ Token anônimo — nenhum secret novo a proteger
- ✅ BullMQ com retry exponencial
- ✅ Commits em inglês (conventional commits)
- ✅ Tipos do Supabase regeneráveis via `pnpm db:types`
- ✅ Isolado em `partners/spotify-partner/` — fácil de extrair se precisar
