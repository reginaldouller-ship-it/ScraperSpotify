# Arquitetura

> Atualizado em 2026-06-20 (pós-incidente 19/jun + hardening + pipeline incremental).
> Reflete o desenho **atual** em produção. Para o desenho-alvo de escala (multi-IP,
> planner, frescor), ver [docs/architecture/2026-06-19-scraper-escalavel-design.md](architecture/2026-06-19-scraper-escalavel-design.md).

## O que este sistema faz (em 1 parágrafo)

Coleta do Spotify **dados que a API oficial NÃO expõe**: `playcount` por faixa,
`monthly_listeners` + `world_rank` por artista, as **top 5 cidades** por artista e as
playlists "**discovered on**" (onde o artista está sendo descoberto). Uma vez por dia,
lê os IDs de tracks/artistas do banco do Miner, consulta a Partner GraphQL interna do
Spotify e grava 4 tabelas de "snapshot" (foto do dia) nesse mesmo banco.

## Visão de 30 segundos

```
        ┌──────────────────────────────────────────┐
        │   VPS Hostinger 187.127.73.16 (1 vCPU/4GB)│
        │   Coolify · Docker (caps: cpu 0.7, 3g RAM)│
        │                                           │   cron '0 12 * * *' UTC
        │   ┌────────────────────────────────────┐  │   = 09:00 BRT / 12:00 UTC
        │   │ scheduled task: sync-diario        │  │   timeout 14400s (4h)
        │   │ python -m scripts.sync_from_supabase│─┼─┐  TZ=UTC
        │   └────────────────────────────────────┘  │ │
        └────────────────────────────────────────────┘ │
                                                        │
       ┌────────────────────────────────────────────────┼─────────────────────────┐
       │                            │                    │                          │
       ▼                            ▼                    ▼                          ▼
┌────────────────────┐   ┌──────────────────┐  ┌──────────────────┐    ┌──────────────────┐
│ Supabase SELF-HOST │   │ Partner GraphQL  │  │  Web Player /     │    │  Embed page       │
│ supabase.minermusic│   │ api-partner.     │  │  get_access_token │    │ open.spotify.com/ │
│   .com.br          │   │ spotify.com/     │  │  (endpoint direto │    │   embed/album/... │
│ (VPS 147.79.87.195)│   │ pathfinder/v1/   │  │   — quase sempre  │    │ → __NEXT_DATA__   │
│                    │   │ query            │  │   403, exige TOTP)│    │ (FONTE PRIMÁRIA   │
│ LÊ:  spotify_tracks│◀──│ getAlbum         │  └──────────────────┘    │  do token)        │
│      spotify_artists│  │ queryArtistOver. │           ▲              └──────────────────┘
│                    │   │ queryArtistDisc..│           │                       ▲
│ ESCREVE: 4 tabelas │   └──────────────────┘           └── token anônimo ──────┘
│  de snapshot       │
└────────────────────┘
```

> ⚠️ **Banco = self-hosted, não o cloud antigo.** Desde o cutover de 13/jun, o destino é
> `supabase.minermusic.com.br` (na VPS 147.79.87.195). O cloud antigo
> (`suzcbyzidnzzahwrkveh`) foi **aposentado** (fallback congelado, pausa prevista p/ início
> de julho). Conexão é via `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` por env — nunca em código.

## Componentes

### 1. Coolify (VPS Hostinger, Docker)

- Roda a imagem do scraper construída a partir do `Dockerfile`.
- 1 scheduled task (`sync-diario`): cron `0 12 * * *` = **09:00 BRT / 12:00 UTC**, timeout 14400s (4h).
- App UUID: `bd2yfhivgp2tiv6vdflem0ab`. Container roda em **UTC** (`TZ=UTC`), pra alinhar timestamps
  do log com o `sync_date` (que já é UTC no código) e com os crons de merge/poda do Miner.
- **Caps de recurso** (`docker-compose.yml`): `cpus=0.7`, `mem_limit=3g`, `memswap_limit=3g`.
  O Docker **impõe** isso no kernel → mesmo se o sync surtar, a VPS de 1 vCPU **não satura** nem
  estoura o host (foi exatamente isso que derrubou tudo no incidente 19/jun).
- **NÃO há auto-deploy no push.** O webhook não dispara. O deploy é **MANUAL**: botão Deploy no
  Coolify ou via API/MCP. (O README antigo dizia "auto-deploy no push" — está **errado**.)

### 2. Spotify Partner GraphQL API (camada NÃO-oficial)

- Endpoint: `https://api-partner.spotify.com/pathfinder/v1/query`.
- **Por que não a Web API oficial?** A oficial só dá `popularity` (0-100) e `followers`. Ela **não
  expõe** playcount, monthly listeners, world rank, top cities nem discovered on. Para esses dados
  **não existe caminho legítimo** — só a Partner interna. Método **frágil e combatido** pelo Spotify
  (ver Pontos frágeis).
- **Autenticação: token anônimo** do Web Player (sem login). Ordem de tentativa (`src/auth.py`):
  1. Endpoint direto `get_access_token` — hoje quase sempre 403 (passou a exigir TOTP ~mar/2025).
     Por isso fica **desabilitado por padrão** (só tenta se `TRY_DIRECT_TOKEN_ENDPOINT=1`).
  2. **Fallback (primário na prática):** extrai o `accessToken` do JSON `__NEXT_DATA__` de uma página
     de **embed** pública — o mesmo token que o iframe oficial do Spotify usa.
- **Persisted queries** identificadas por **SHA-256 hash** (em `config/settings.py:GRAPHQL_HASHES`).
  Rotacionam periodicamente; quando quebram (`400 PersistedQueryNotFound`), rodar
  `python -m scripts.discover_hashes --write`.
- Operações usadas pelo sync:
  - `getAlbum(uri, limit)` — playcount de todas as tracks do álbum.
  - `queryArtistOverview(uri)` — monthly listeners, world rank, top cities.
  - `queryArtistDiscoveredOn(uri)` — playlists impulsionando o artista.

### 3. Supabase self-hosted (banco do Miner)

Postgres 17 + PostgREST com **statement_timeout = 8s** (a origem da maioria das regras de query).

- **Tabelas que o scraper LÊ** (populadas pelo Miner — scraper só consulta IDs):
  - `spotify_tracks` — `spotify_id`, `album_id`, `primary_artist_spotify_id`.
  - `spotify_artists` — `spotify_id`.
- **Tabelas que o scraper ESCREVE** (snapshots — keep-forever, nunca podadas por nós):

  | Tabela | PK | Quem grava o quê |
  |---|---|---|
  | `spotify_track_snapshots` | `(spotify_track_id, date)` | scraper: `playcount` |
  | `spotify_artist_snapshots` | `(spotify_artist_id, date)` | **LINHA COMPARTILHADA**: collector grava `popularity`/`follower_count`; scraper grava só `monthly_listeners`/`world_rank` |
  | `spotify_artist_top_cities_snapshots` | `(spotify_artist_id, date, rank)` | scraper: top 5 cidades (staging diária; servidor poda/merge) |
  | `spotify_artist_discovered_on_snapshots` | `(spotify_artist_id, date, playlist_id)` | scraper: playlists "discovered on" (staging diária; servidor faz merge SCD-2) |

### 4. Cliente Supabase próprio (`src/supabase_client.py`)

Não usamos `supabase-py` — só precisamos de SELECT/UPSERT. Cliente assíncrono minimalista que:

- **SELECT por keyset pagination** (cursor por PK), nunca offset, nunca `count=exact`. Cada página é
  um `WHERE pk > último_visto ORDER BY pk LIMIT N` — eficiente em qualquer tamanho de tabela, imune a
  `statement_timeout` e a `PGRST103` (416). O cursor avança por `batch[-1][order_by]`, então
  truncamento do `db-max-rows` do PostgREST não pula linhas.
- **UPSERT em batches** (500/req) com `Prefer: resolution=merge-duplicates,return=minimal`. `on_conflict`
  é sempre passado **explicitamente** = a PK exata de cada tabela.
- **Retry em 5xx/rede** com backoff exponencial + jitter (4 tentativas). **4xx NÃO retenta** (FK,
  CHECK, validação = dado errado, não rede).

## Fluxo do sync diário (`scripts/sync_from_supabase.py`)

```
0. Trava de instância única (flock em /tmp). Se já há um sync rodando → SAI sem empilhar.
   Define sync_date = data UTC do início da run → carimba TODAS as 4 tabelas (parte da PK).

1. LOAD IDs (keyset):
     select_all("spotify_tracks", "spotify_id,album_id", where=album_id NOT NULL, order_by=spotify_id)
     select_all("spotify_artists", "spotify_id", order_by=spotify_id)
   → deriva: albums_set (album_ids distintos), artists_set, known_track_ids

2. Token anônimo (embed __NEXT_DATA__).

3. FASE ALBUMS  — N workers async (SYNC_WORKERS, default 20; prod hoje 16)
     getAlbum × cada álbum → playcount por track
       • pula track com playcount None  (NUNCA int(x or 0) — mascara campo ausente)
       • pula track que não está em known_track_ids (FK — é trabalho do collector)
     → BufferedUpserter grava spotify_track_snapshots em LOTES DURANTE a run (FLUSH INCREMENTAL)

4. FASE ARTISTAS — N workers async
     queryArtistOverview → monthly_listeners/world_rank + top_cities (rank 1..5)
     queryArtistDiscoveredOn → playlists (request separado)
     → BufferedUpserter grava artist_snapshots / top_cities / discovered_on, TAMBÉM incremental

5. Decide status por TAXA de falha (>1% por categoria = degraded/exit 1; senão exit 0).
   Grava log estruturado em data/sync_runs/<timestamp>_<sync_date>.json (stub no início, final no fim).
```

> **Por que DUAS fases incrementais?** Eram **duas bombas de RAM** (ver abaixo). O flush incremental
> em ambas mantém a RAM em MBs (antes acumulava milhões de linhas pro fim e estourava o OOM).

**Validação (20/jun):** run completa em **~50 min** escreveu as 4 tabelas para 2026-06-20:
track 1.295.823 · artist (monthly_listeners) 61.592 · top_cities 286.739 · discovered_on 1.584.722
— todos em nível de dia cheio, sem OOM. Pipeline validado ponta a ponta.

## Módulos (responsabilidade única)

| Módulo | Papel |
|---|---|
| `scripts/sync_from_supabase.py` | Orquestra a run diária: load IDs → fase albums → fase artistas → log. |
| `src/supabase_client.py` | Cliente PostgREST: `select_all` (keyset), `upsert` (batch + retry 5xx). |
| `src/buffered_writer.py` | `BufferedUpserter` (buffer + flush incremental, take-and-release lock) + `resilient_upsert` (resiliência por-linha). |
| `src/snapshot_dedup.py` | `dedupe_track_snapshots`: colapsa (track, date) duplicada mantendo o **maior** playcount. |
| `src/singleton_lock.py` | Trava `flock` de instância única (impede empilhar runs). |
| `src/sync_status.py` | Decisão **pura** de status/exit-code por taxa de falha (testável isolado). |
| `src/auth.py` | Token anônimo do Spotify (direto → fallback embed), refresh/rotação. |
| `src/graphql.py` | Cliente da Partner GraphQL (parsers, `HashOutdatedError`, rate limiting). |

## Proteções (o que evita repetir o incidente 19/jun)

O incidente foi a **VPS de 1 vCPU saturada por runs empilhadas + bomba de RAM** (OOM). Quatro defesas:

1. **Trava de instância única** (`src/singleton_lock.py`) — `flock` não-bloqueante: a 2ª run **sai na
   hora** em vez de empilhar. Se o dono morre (OOM/SIGKILL), o SO libera o lock sozinho. **Validado em
   prod:** 2 disparos saíram "sem empilhar".
2. **Caps de recurso** (`docker-compose.yml`) — `cpus=0.7` / `mem_limit=3g` / `memswap_limit=3g`.
   O Docker impõe no kernel; a VPS não satura nem dá OOM no host.
3. **Flush incremental nas DUAS fases** (`BufferedUpserter`) — RAM sempre baixa.
4. **Resiliência por-linha** (`resilient_upsert`) — se um lote falha por **4xx** (uma linha ruim:
   FK 23503 ou CHECK), reenvia **linha-a-linha** pulando só a ruim (conta em `rows_skipped_bad`), em
   vez de derrubar a run. **5xx** propaga (infra real).
5. **Exit-code por taxa** (`sync_status.py`) — 1 blip de rede em ~450 mil itens **não** vira "failed".
   Só `degraded`/exit 1 se a taxa de falha de alguma categoria passar de 1%.

### As 2 bombas de RAM (histórico)

- **1ª (tracks):** acumulava ~3M linhas pra gravar no fim → OOM. Corrigida com flush incremental.
- **2ª (artistas):** acumulava ~1,9M linhas (1,58M discovered + 310k top_cities) → OOM **antes** de
  gravar os artistas. **Era a causa das falhas de top_cities/discovered desde 06-14** (o playcount
  passava porque a fase de tracks já era incremental). Corrigida 20/jun estendendo o flush à fase de artista.
- **Insight de capacidade:** pela métrica da VPS, o scraper é **rede-bound** (CPU ~30%, longe do cap)
  → subir workers acelera. `SYNC_WORKERS` foi de 8→16 (~2× mais rápido). Configurável por env.

## Contrato com o Miner (regras duras)

As tabelas `spotify_*` **pertencem ao Miner**. O scraper só lê IDs e escreve snapshots. Resumo das
regras (detalhe e justificativa em **§10.5** de
[2026-06-19-scraper-escalavel-design.md](architecture/2026-06-19-scraper-escalavel-design.md)):

- `spotify_artist_snapshots` é **LINHA COMPARTILHADA**: upsert **PARCIAL** — só `monthly_listeners`/
  `world_rank`. Mandar `popularity`/`follower_count` (mesmo NULL) **apagaria** o dado do collector.
  Por isso o worker **só grava a linha de artista se houver ao menos um dado útil** (não escreve linha
  toda-NULL que sobrescreveria o collector numa re-run).
- `on_conflict` **explícito** = PK exata de cada tabela.
- **NÃO** escrever `spotify_tracks.latest_playcount`/`_date` — um **trigger statement-level** do Miner
  propaga do snapshot (só avança, nunca regride).
- **NÃO** fazer DELETE próprio em `top_cities`/`discovered_on` — são geridas pelo **servidor**: merge
  SCD-2 às **15:30 UTC**, poda top_cities **16:00 UTC**, poda staging discovered **16:10 UTC**.
  (O código tinha um DELETE-then-INSERT; **removido 20/jun** — UPSERT por PK numa data nova basta.)
- **playcount por `spotify_id`**, nunca dedupe por ISRC (o Miner resolve ISRC na camada canônica).
- `sync_date` = data **UTC** única no início da run, carimba tudo.
- ✅ **Timing do `discovered_on` (resolvido — PR #157, merge "dias fechados"):** o merge do Miner consolida
  **só `date < CURRENT_DATE`**, então o `discovered_on` de hoje entra **sempre em D+1, independentemente da
  hora** da coleta. A nota antiga ("coletar nas primeiras ~3,5h, antes das 15:30 UTC") está **OBSOLETA** — o
  planner (SS-6) NÃO precisa priorizar discovered_on de manhã. Confirmado contra o `pg_cron` vivo (20/jun).

## Decisões arquiteturais

| Decisão | Por quê |
|---|---|
| **Keyset pagination** em vez de offset | Tabela cresce; offset+count estoura `statement_timeout` 8s (57014) ou retorna 416 (PGRST103). |
| **Cliente HTTP próprio** vs `supabase-py` | Só precisamos de SELECT/UPSERT — sem dep extra. |
| **Flush incremental** (buffer + flush durante a run) | Acumular milhões de linhas pro fim estourava o OOM (incidente 19/jun). |
| **Resiliência por-linha** (4xx) | Uma linha ruim não pode derrubar a run inteira; 5xx (infra) ainda propaga. |
| **Trava flock de instância única** | Runs empilhadas saturavam a VPS de 1 vCPU. |
| **Exit-code por taxa de falha** | 1 blip de rede em ~450k itens não é "run failed". |
| **Caps CPU/RAM no compose** | A VPS é compartilhada com o Coolify; sem cap, o sync derruba tudo. |
| **`SYNC_WORKERS` configurável** | Scraper é rede-bound — mais workers acelera sem bater no cap de CPU. |
| **Token anônimo via embed** vs OAuth | Não precisa app registrado; sobrevive ao TOTP do endpoint direto. |
| **dedup por MAIOR playcount** | O guard do Miner é por **data**, não por valor — gravar o menor rebaixaria `latest_playcount`. |

## Tracks "skipped (não em spotify_tracks)"

Sintoma comum nos logs (ex: "skipped (não em spotify_tracks)=…"). **Não é bug.** `getAlbum` devolve
**todas** as tracks do álbum, mas `spotify_tracks` só tem as que o **collector do Miner** cadastrou.
As que vêm da API e não estão cadastradas são puladas (FK) — popular `spotify_tracks` é responsabilidade
do collector, não deste sync.

## Pontos frágeis conhecidos

1. **A camada de acesso é não-oficial e combatida** (risco existencial). A Web API oficial não expõe
   esses dados → **não há migração possível**. O Spotify quebra o mecanismo recorrentemente (restrições
   dez/2025, update 06/fev/2026 derrubou scrapers). Mitigação: preflight de token+hash, comportamento
   de navegador real, ritmo conservador, tratar ban de IP como esperado. **Mais IPs = mais risco de
   detecção** — escala e redução-de-risco andam juntas. (Detalhe em §12 do design de escala.)
2. **SHA-256 dos persisted queries** rotacionam → `discover_hashes.py --write` redescobre.
3. **`statement_timeout` = 8s** → qualquer query que cresça com a tabela pode estourar. Sempre keyset/
   triggers eficientes.
4. **`track_snapshots` (~33,7M) sem índice em `date` e não particionada** → operação "só por date" é scan
   gigante. **Verificar progresso por `spotify_tracks.latest_playcount_date` (proxy), nunca `count` por
   date em `track_snapshots`.** Particionamento (Timescale) é trabalho futuro do lado Miner.
5. **Coolify corta a visibilidade da run aos 60 min** (`dynamic_timeout=3600`) e mostra "Success" — mas
   **não mata** o processo (roda órfão). **Não confiar no status do Coolify pra runs longas**; verificar
   pelo banco. Sugestão aberta: subir `dynamic_timeout` p/ 14400.

## Trabalho futuro (Jira projeto SS, Epic SS-1)

- **SS-6 planner:** prioridade por popularidade, portão canário de frescor (só coletar se o Spotify
  virou o dia), fila "não coletado hoje".
- **SS-9 sharding multi-IP** (`NODE_COUNT`, `hash(id) % N`) quando o catálogo crescer 7-8× (~10M tracks/dia).
- **Outbox real (SQLite)** pra reenviar gravações que falharam (FK 23503 raro hoje).
- Fixar `--workers` no comando da task (vs depender do env); `dynamic_timeout` 3600→14400.
- **Lado Miner:** tabela `spotify_sync_runs` (observabilidade, MMPDA-125); particionar `track_snapshots`
  por mês (MMPDA-82) antes do salto 7-8×; write-on-change (bloqueado até consertar `avg_daily_delta` +
  `mv_genre_stats`).
- `orjson` pra parse mais rápido (CPU).
