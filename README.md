# Spotify Streams Scraper

Coleta do Spotify dados que **a API oficial não entrega** e grava um retrato (snapshot) por dia no banco do Miner. É o que abastece os números de streams/ouvintes que aparecem no produto.

> **Atalhos:** entender o desenho → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · debugar um problema → [docs/RUNBOOK.md](docs/RUNBOOK.md) · mexer no código (regras e anti-padrões) → [CLAUDE.md](CLAUDE.md) · o desenho da versão escalável → [docs/architecture/2026-06-19-scraper-escalavel-design.md](docs/architecture/2026-06-19-scraper-escalavel-design.md) · o incidente de 19/jun e a recuperação → [docs/incidents/2026-06-19-cpu-saturada-sync-parado.md](docs/incidents/2026-06-19-cpu-saturada-sync-parado.md).

---

## 1. O que o sistema faz

Todo dia o scraper pergunta ao Spotify, faixa por faixa e artista por artista, números que **não existem na API oficial** do Spotify. Ele consegue isso usando a mesma "porta dos fundos" que o Web Player do Spotify (o player que roda no navegador) usa por baixo dos panos — a **Partner GraphQL** (`api-partner.spotify.com/pathfinder/v1/query`), entrando com um **token anônimo** do Web Player (sem login; se a forma direta de pegar o token falhar, ele extrai do `__NEXT_DATA__` de uma página de embed).

> ⚠️ Isto **não** é a Web API oficial do Spotify — a oficial não expõe esses campos. É um método **frágil e combatido** pelo Spotify: as "persisted queries" têm uma assinatura (hash SHA-256) que o Spotify troca de tempos em tempos. Quando isso acontece o scraper para de funcionar com erro `PersistedQueryNotFound`, e a gente recupera os hashes novos rodando `python -m scripts.discover_hashes --write`.

**Dados coletados:**

- **playcount por faixa** (quantas vezes cada música foi tocada)
- **monthly_listeners** e **world_rank** por artista (ouvintes mensais e posição no ranking global)
- **top 5 cidades** por artista (onde o artista mais é ouvido)
- **playlists "discovered on"** (playlists pelas quais as pessoas descobriram o artista)

**Onde grava** — 4 tabelas de snapshot no Supabase do Miner. O scraper **só lê IDs e escreve snapshots**; o schema dessas tabelas pertence ao Miner (não criar/dropar/alterar sem alinhar):

| Tabela | Chave primária (PK) | O que o scraper escreve |
|---|---|---|
| `spotify_track_snapshots` | `track_id, date` | `playcount` |
| `spotify_artist_snapshots` | `artist_id, date` | `monthly_listeners`, `world_rank` — **linha compartilhada** (ver abaixo) |
| `spotify_artist_top_cities_snapshots` | `artist_id, date, rank` | top 5 cidades |
| `spotify_artist_discovered_on_snapshots` | `artist_id, date, playlist_id` | playlists "discovered on" |

---

## 2. Como funciona (o pipeline)

O run diário é o `scripts/sync_from_supabase.py`. Ele acontece em fases:

**Fase 0 — carregar os IDs do banco.** Lê de `spotify_tracks` e `spotify_artists` quais faixas/artistas monitorar. Essa leitura usa **paginação keyset** (cursor pela PK, nunca `offset`, nunca `count=exact`) — porque o PostgREST tem um `statement_timeout` de 8 segundos e um `COUNT` numa tabela gigante estoura esse limite. Detalhe em `src/supabase_client.py`.

**Fase 1 — faixas (albums/tracks).** Vários trabalhadores (workers) em paralelo batem na query `getAlbum`, pegam o playcount de cada faixa do álbum e gravam em `spotify_track_snapshots`.

**Fase 2 — artistas.** Os mesmos workers em paralelo batem em `queryArtistOverview` (ouvintes mensais, world rank, top cities) e `queryArtistDiscoveredOn`, e gravam nas tabelas de artista.

### As proteções (e por que existem)

Cada proteção abaixo nasceu de um problema real — em especial o incidente de 19/jun, em que a VPS de 1 vCPU saturou com runs empilhadas e estourou de RAM.

- **Flush incremental** (`src/buffered_writer.py`, classe `BufferedUpserter`). Em vez de juntar **tudo** na memória e gravar só no fim, o scraper grava em **lotes pequenos durante o run**. Antes, ele acumulava milhões de linhas (≈3M de faixas; e na fase de artista ≈1,58M de "discovered" + 310k de top cities) e estourava a RAM **antes** de gravar — eram as duas "bombas de RAM" que derrubavam o run. Com o flush incremental nas **duas** fases, a RAM fica sempre baixa.
- **Trava de instância única** (`src/singleton_lock.py`, um `flock`). Impede que um segundo run comece por cima de outro que ainda está rodando — era o empilhamento de processos que saturava a VPS. Validado em produção: dois disparos seguidos, o segundo saiu "sem empilhar".
- **Limites de recurso** no `docker-compose.yml` (`cpus=0.7`, `mem_limit=3g`, `memswap_limit=3g`). Quem impõe esses tetos é o **Docker, no kernel** — então mesmo que o scraper tente puxar mais, a VPS inteira não satura e o host não sofre OOM.
- **Resiliência por linha** (`resilient_upsert` em `src/buffered_writer.py`). Se um lote de gravação falha por dado ruim (erro 4xx — uma FK órfã `23503` ou um `CHECK`), em vez de derrubar o run inteiro ele **reenvia linha a linha** e pula só a linha problemática. Erros de infra (5xx/rede) continuam propagando (já foram retentados antes).
- **Dedup por maior playcount** (`src/snapshot_dedup.py`). Se a mesma faixa aparece duas vezes no lote, mantém a de maior playcount.
- **Status por taxa de falha** (`src/sync_status.py`). Com centenas de milhares de itens, um único "soluço" de rede não deve marcar o run como "falhou". O exit-code só sai diferente de 0 (`degraded`) se **mais de 1%** falhar; abaixo disso é `partial`/`ok` e os dados do dia foram gravados.
- **Log estruturado** de cada run em `data/sync_runs/<timestamp>.json` (status, workers, contagens, linhas puladas por dado ruim) — para auditoria.

### Contrato com o Miner (regras duras — ver §10.5 da [spec](docs/architecture/2026-06-19-scraper-escalavel-design.md))

Estas tabelas são do Miner, e quem coleta também escreve nelas. Por isso:

- **`spotify_artist_snapshots` é linha compartilhada.** O collector do Miner grava `popularity`/`follower`; o scraper grava só `monthly_listeners`/`world_rank`. O upsert do scraper é **parcial** — mandar `popularity`/`follower` apagaria o que o collector escreveu.
- **`on_conflict` sempre explícito**, igual à PK exata de cada tabela.
- **Não escrever `spotify_tracks.latest_playcount`** — um trigger do Miner propaga isso sozinho.
- **Não fazer DELETE próprio** em top_cities/discovered — são geridas pelo servidor (merge SCD-2 às 15:30 UTC, poda de top_cities às 16:00, poda do staging de discovered às 16:10). O scraper só faz UPSERT.
- **`date` única por run.** Uma data UTC é decidida no início do run e carimba **tudo** — ela faz parte da PK das 4 tabelas e é a chave do dedup diário.

---

## 3. Stack e onde roda

- **Python 3.12** + `httpx` async + `tenacity` (retry/backoff) + `rich` (logs). Cliente PostgREST próprio em `src/supabase_client.py` (keyset pagination).
- **Banco de destino:** Supabase **self-hosted** do Miner em `supabase.minermusic.com.br` (VPS `147.79.87.195`).
  - ⚠️ **Não é mais** o Supabase Cloud antigo (`suzcbyzidnzzahwrkveh`), aposentado depois do cutover de 13/jun.
- **Onde o cron roda:** VPS Hostinger `187.127.73.16` (1 vCPU / 4 GB, Frankfurt), gerenciada pelo **Coolify**.
  - App `scraper-spotify` (uuid `bd2yfhivgp2tiv6vdflem0ab`).
  - Scheduled task `sync-diario`, cron `0 12 * * *` = **09:00 BRT / 12:00 UTC**, timeout 14400s (4h). O container roda em UTC (`TZ=UTC`).
- **Deploy é MANUAL.** Hoje **não há auto-deploy no push** (o webhook não dispara). Para deployar, use o botão **Deploy** no Coolify ou dispare via API/MCP:
  ```bash
  curl -H "Authorization: Bearer $COOLIFY_TOKEN" \
    "http://187.127.73.16:8000/api/v1/deploy?uuid=bd2yfhivgp2tiv6vdflem0ab&force=false"
  ```

---

## 4. Como rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate     # Linux/Mac
# .venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

O `.env` precisa de:

```
SUPABASE_URL=https://supabase.minermusic.com.br
SUPABASE_SERVICE_ROLE_KEY=...
SYNC_WORKERS=16                # opcional; default 20 no código
```

Rodando o sync (o mesmo que roda em produção):

```bash
python -m scripts.sync_from_supabase --dry-run     # só mostra o que faria, não escreve
python -m scripts.sync_from_supabase --limit 10    # 10 albums + 10 artists (smoke test)
python -m scripts.sync_from_supabase               # run completo
python -m scripts.sync_from_supabase --workers 16  # override do default
python -m scripts.sync_from_supabase --snapshot-date 2026-06-20   # força uma data
```

> O scraper é **rede-bound**, não CPU-bound (a CPU fica em ~30%, longe do teto de 0.7). Por isso subir o número de workers acelera o run — de 8 para 16 workers o run ficou ~2x mais rápido. Configurável via env `SYNC_WORKERS`.

**Validação do pipeline (20/jun):** um run completo em ~50 min escreveu as 4 tabelas para `2026-06-20` em nível de dia cheio — track 1.295.823, artist `monthly_listeners` 61.592, top_cities 286.739, discovered_on 1.584.722 — sem OOM.

---

## 5. Sintomas comuns e onde olhar

| Sintoma | O que é / onde olhar |
|---|---|
| `57014 statement timeout` | Query estourou os 8s do PostgREST — olhar índice/plano ([RUNBOOK](docs/RUNBOOK.md)) |
| `PGRST103 range not satisfiable` | `offset > total` — usar keyset pagination ([RUNBOOK](docs/RUNBOOK.md)) |
| `HashOutdatedError` / `PersistedQueryNotFound` | Hash de persisted query desatualizado → `python -m scripts.discover_hashes --write` |
| Snapshots/dia caindo aos poucos | Run morrendo antes do fim (foi a 2ª bomba de RAM, na fase de artista) ([RUNBOOK](docs/RUNBOOK.md)) |

**Coolify mente em runs longos:** o setting de servidor `Deployment timeout (seconds)` / `dynamic_timeout` está em 3600 (60 min). Ele **corta o rastreamento** do run aos 60 min e mostra "Success" — mas **não mata o processo** (ele continua rodando órfão). Então **não confie no status do Coolify** para runs longos; confirme pelo **banco**, contando snapshots por `date` no self-host:

```bash
ssh miner-vps "docker exec -i supabase-db-bax8nu79nywtkqoxyvb4lhtu psql -U postgres -d postgres"
```

> `spotify_track_snapshots` (~33,7M linhas) não tem índice em `date` nem partição → contar "só por date" faz um scan gigante. Para acompanhar progresso, use `spotify_tracks.latest_playcount_date` como proxy (seq scan em ~1,08M tracks, ~1-2s — bem mais leve) — nunca um `count` em `track_snapshots` filtrado por `date`.

---

## 6. Roadmap / O que pode ser feito depois

Rastreado no Jira (projeto **SS**, Epic **SS-1**). Nada disso é necessário para o run diário funcionar hoje — são melhorias para quando o catálogo crescer ou para mais observabilidade.

- **SS-6 — planner inteligente:** priorizar por popularidade; portão "canário" de frescor (só coletar artista/faixa se o Spotify já virou o dia para ele); fila de "ainda não coletado hoje".
- **SS-9 — sharding multi-IP:** dividir o catálogo entre vários nós (`NODE_COUNT`, `hash(id) % N`) quando crescer 7-8x (~10M faixas/dia).
- **Outbox real (SQLite):** reenviar gravações que falharam (FK `23503` é raro hoje porque o Miner não poda o catálogo).
- **Fixar `--workers` no comando da task** (em vez de depender só do env).
- **Subir `dynamic_timeout` de 3600 → 14400** no Coolify (visibilidade de runs longos).
- **Lado Miner:** tabela `spotify_sync_runs` (observabilidade, MMPDA-125); particionar `track_snapshots` por mês (MMPDA-82) antes do salto de 7-8x; write-on-change (bloqueado até consertar `avg_daily_delta` + `mv_genre_stats`).
- **`orjson`** para parse mais rápido (ganho de CPU).

---

## 7. Notas legais

Usa dados acessíveis pelo Web Player (sem login, sem dados privados). Pode violar os Termos de Serviço do Spotify — uso por sua conta e risco.
