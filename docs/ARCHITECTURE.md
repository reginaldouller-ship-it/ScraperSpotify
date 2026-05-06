# Arquitetura

## Visão de 30 segundos

```
                     ┌─────────────────────────┐
                     │   Coolify VPS (Docker)  │
                     │  187.127.73.16:8000     │
                     │                         │
                     │  ┌──────────────────┐   │
                     │  │ cron: sync-diario│   │   09:00 SP / 12:00 UTC
                     │  │ python -m ...    │───┼─┐
                     │  └──────────────────┘   │ │
                     └─────────────────────────┘ │
                                                 │
                  ┌──────────────────────────────┼──────────────────────────────┐
                  │                              │                              │
                  ▼                              ▼                              ▼
         ┌─────────────────┐           ┌──────────────────┐         ┌────────────────────┐
         │ Supabase Miner  │           │ Spotify Partner  │         │  Spotify Embed     │
         │ suzcbyzidnzz... │           │  GraphQL API     │         │  (fallback token)  │
         │                 │           │ api-partner.spo. │         │ open.spotify.com/  │
         │ READ:           │           │                  │         │   embed/album/...  │
         │  spotify_tracks │  ◀─────── │ getAlbum         │         │                    │
         │  spotify_artists│           │ queryArtistOver. │         └────────────────────┘
         │                 │           │ queryArtistDisc..│
         │ WRITE:          │           │                  │
         │  spotify_track_ │           └──────────────────┘
         │   snapshots     │
         │  spotify_artist_│
         │   snapshots     │
         │  ..._top_cities │
         │  ..._discovered │
         └─────────────────┘
```

## Componentes

### 1. Coolify (VPS, Docker)

- Roda a imagem do scraper construída a partir do `Dockerfile`.
- Tem 1 scheduled task (`sync-diario`) que executa `python -m scripts.sync_from_supabase` diariamente.
- Auto-deploy: `git push origin main` → rebuild + restart em ~60s.
- App UUID: `bd2yfhivgp2tiv6vdflem0ab`. Task UUID: `wynmgo9ssfzwp5h5mmro1511`.

### 2. Spotify Partner GraphQL API

- Endpoint: `https://api-partner.spotify.com/pathfinder/v1/query`
- Autenticação: **token anônimo** do Web Player (sem login).
  - Tenta `open.spotify.com/get_access_token` (403 em IPs residenciais BR).
  - Fallback: extrair `accessToken` do `__NEXT_DATA__` da página de embed.
- **Persisted queries** identificadas por SHA-256 hash. Mudam periodicamente.
- Operações usadas pelo sync:
  - `getAlbum(uri, limit)` — playcount de todas tracks do álbum.
  - `queryArtistOverview(uri)` — monthly listeners, world rank, top cities.
  - `queryArtistDiscoveredOn(uri)` — playlists impulsionando o artista.

### 3. Supabase (banco do Miner)

- Postgres 17 + PostgREST.
- **Tabelas que o scraper LÊ** (são populadas pelo Miner, scraper só consulta):
  - `spotify_tracks` (~99k rows) — track_id, album_id, primary_artist_spotify_id.
  - `spotify_artists` (~7.5k rows) — artist_id.
- **Tabelas que o scraper ESCREVE** (snapshots):
  - `spotify_track_snapshots` — playcount diário por track. PK `(spotify_track_id, date)`.
  - `spotify_artist_snapshots` — monthly listeners, world rank por dia.
  - `spotify_artist_top_cities_snapshots` — top 5 cidades por artista/dia.
  - `spotify_artist_discovered_on_snapshots` — playlists "discovered on" por artista/dia.

### 4. Cliente Supabase próprio (`src/supabase_client.py`)

Não usamos `supabase-py`. Cliente minimalista que:
- Usa **keyset pagination** (cursor por PK) em SELECT — imune a timeout/PGRST103.
- Faz UPSERT em batches de 500 com `Prefer: resolution=merge-duplicates`.
- Retry automático em 5xx/rede com backoff exponencial.

## Fluxo de dados (sync_from_supabase)

```
1. SELECT spotify_tracks (spotify_id, album_id) → ~99k tracks
   SELECT spotify_artists (spotify_id)         → ~7.5k artists
   ↓
   Deduplicar por album_id → ~41k álbuns únicos

2. Pegar token anônimo (embed __NEXT_DATA__ fallback)

3. ┌─ 20 workers async paralelos ────────────────────────┐
   │  Albums:  getAlbum × 41k                            │
   │  Artists: queryArtistOverview × 7.5k                │
   │           queryArtistDiscoveredOn × 7.5k            │
   └─────────────────────────────────────────────────────┘
   ↓
   Coletar:
   - track_snapshots (playcount por track)
   - artist_snapshots (monthly listeners)
   - top_cities_snapshots
   - discovered_on_snapshots

4. UPSERT em batches de 500 → Supabase
   Trigger statement-level propaga latest_playcount → spotify_tracks

5. Log estruturado em data/sync_runs/<timestamp>.json
```

**Duração típica:** ~22 minutos. **Total de requests Spotify:** ~56k.

## Decisões arquiteturais

| Decisão | Por quê |
|---|---|
| **Keyset pagination** em vez de offset | Tabela cresce; offset+count estoura timeout 8s ou retorna 416 PGRST103 |
| **Cliente HTTP próprio** em vez de `supabase-py` | Precisamos só de SELECT/UPSERT/DELETE, sem dep extra |
| **Trigger statement-level** em vez de row-level | Em batch de 500, statement-level roda 1× vs 500× |
| **20 workers async** | Stress test mostrou 4.4 req/s/worker sustentável |
| **Token anônimo via embed** em vez de OAuth | Não precisa app registrado; sustentável |
| **SQLite local + Supabase remoto** (dois modos) | SQLite pra dev/debug, Supabase pra produção |

## Tracks "skipped (não em spotify_tracks)"

Sintoma comum nos logs: "skipped (não em spotify_tracks)=79041".

**Não é bug.** Quando chamamos `getAlbum` no Spotify, ele retorna **TODAS** as tracks do álbum. Mas a tabela `spotify_tracks` só tem as que o Miner cadastrou (não necessariamente todas). As tracks que vêm da API mas não estão cadastradas são puladas — é responsabilidade do **collector do Miner** popular `spotify_tracks`, não deste sync.

## Pontos frágeis conhecidos

1. **SHA-256 dos persisted queries** — Spotify atualiza periodicamente. Mitigação: `discover_hashes.py` redescobre automaticamente.
2. **Statement timeout do Supabase = 8s** — qualquer query que cresça com a tabela pode estourar. Sempre usar keyset/triggers eficientes.
3. **Token rate limit** — após N=100 requests, força refresh. Em IPs bloqueados pelo endpoint direto, usa fallback embed.
4. **db-max-rows do PostgREST** — pode truncar respostas. Cliente atual avança offset por `len(batch)` em vez de `page_size` pra ser robusto.
