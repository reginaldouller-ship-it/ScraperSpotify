# RUNBOOK — Diagnóstico e operações

Guia operacional: o que fazer quando algo dá errado em produção.

> **⚠️ Onde fica o banco hoje (atualizado 2026-06-20):** as 4 tabelas de snapshot vivem no **Supabase SELF-HOSTED do Miner** — `supabase.minermusic.com.br` (VPS 147.79.87.195). **NÃO** é mais o Supabase Cloud antigo (`suzcbyzidnzzahwrkveh`), que foi **aposentado** no cutover de 13/jun. Qualquer comando/SQL aqui assume o self-host. Acesso ao banco é via `ssh miner-vps` + `psql` dentro do container (ver [Como verificar um run](#como-verificar-um-run)).

## Atalho mental (decoreba)

| Sintoma | O que é | Pula pra |
|---|---|---|
| `Timeout 57014` | Query estourou os 8s do `statement_timeout` do PostgREST. Olhar índice/plano. | [Timeout 57014](#timeout-57014) |
| `PGRST103` | Offset pedido > total estimado. Usar keyset, nunca offset. | [PGRST103](#pgrst103) |
| `HashOutdatedError` / `PersistedQueryNotFound` | Spotify rotacionou o hash da persisted query. Rodar `discover_hashes`. | [Hash desatualizado](#hash-desatualizado) |
| **Snapshots/dia caindo** | **Run morrendo antes do fim.** Até 20/jun era a 2ª bomba de RAM (fase de artista estourava OOM antes de gravar) — **já corrigida** com flush incremental. Se voltar, é outra coisa parando a paginação no meio. | [Snapshots incompletos](#snapshots-incompletos) |
| `403` em `/get_access_token` | IP brasileiro bloqueado no endpoint direto. Fallback via embed já cobre. | [Token bloqueado](#token-bloqueado) |
| `429 Rate Limited` consecutivos | Spotify limitando. Cliente pausa 5min após 3 seguidos. | [Rate limit Spotify](#rate-limit-spotify) |

## Sumário das operações

- [Como verificar um run (contar snapshots por date)](#como-verificar-um-run)
- [Verificar progresso sem scan gigante](#verificar-progresso-sem-scan-gigante)
- [Deploy é MANUAL (não tem auto-deploy no push)](#deploy-é-manual)

## Onde/como roda (referência rápida)

- **VPS scraper:** Hostinger `187.127.73.16` (1 vCPU / 4 GB, Frankfurt), gerenciada por **Coolify**.
- **App:** `scraper-spotify`, uuid `bd2yfhivgp2tiv6vdflem0ab`.
- **Cron:** scheduled task `sync-diario`, expressão `0 12 * * *` = **09:00 BRT / 12:00 UTC**, timeout 14400s (4h). O container roda em **UTC** (`TZ=UTC`) — o `sync_date` que carimba os snapshots é a data UTC capturada no início da run.
- **Caps de recurso** (impostos pelo Docker no `docker-compose.yml`, evitam saturar a VPS): `cpus=0.7`, `mem_limit=3g`, `memswap_limit=3g`.
- **Workers:** `SYNC_WORKERS` por env (default 20 no código; prod hoje usa 16). O scraper é **rede-bound** (CPU fica ~30%, não bate no cap) — subir workers acelera, não estoura CPU.

## Comandos de diagnóstico

### Listar app no Coolify
```bash
curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/applications/bd2yfhivgp2tiv6vdflem0ab" | jq
```

### Ver scheduled tasks
```bash
curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/applications/bd2yfhivgp2tiv6vdflem0ab/scheduled-tasks" | jq
```

> Para **disparar deploy** veja [Deploy é MANUAL](#deploy-é-manual). Para **verificar um run** veja [Como verificar um run](#como-verificar-um-run) — **não confie no status do Coolify**.

### Statement timeout dos roles
```sql
SELECT rolname, rolconfig FROM pg_roles
WHERE rolname IN ('authenticator','authenticated','anon','service_role');
```

---

## Como verificar um run

> **⚠️ NÃO confie no status "Success" do Coolify para runs longas.** Existe um setting **de servidor** no Coolify — `Deployment timeout (seconds)` / `dynamic_timeout` — hoje em **3600 (60 min)**. Quando a run passa de 60 min, o Coolify **para de rastrear** a execução e marca como **"Success"** mesmo que o sync ainda esteja rodando (ou tenha morrido). Ele **não mata** o processo — o sync continua como processo órfão dentro do container. Ou seja: o "Success" do Coolify só significa "o Coolify desistiu de assistir aos 60 min", não "o sync terminou bem". **A fonte da verdade é o BANCO.** (Recomendação de melhoria: subir `dynamic_timeout` para 14400 só pra ganhar visibilidade — ver [Future work](#future-work).)

A maneira correta de saber se um run foi bem é **contar os snapshots daquela data (UTC) direto no self-host**:

```bash
# 1) Entrar na VPS do Supabase self-host
ssh miner-vps

# 2) Abrir o psql dentro do container do Postgres
#    (o nome do container muda se o Supabase for recriado — confira com `docker ps`)
docker exec -i supabase-db-bax8nu79nywtkqoxyvb4lhtu psql -U postgres -d postgres
```

Com o `psql` aberto, conte as 4 tabelas para a data do run (use a **data UTC**, ex.: `'2026-06-20'`):

```sql
-- Substitua a data pela do run que você está verificando
\set d '2026-06-20'

-- Tracks (a tabela gigante — ver aviso na próxima seção antes de rodar count por date)
-- Para track, prefira o proxy latest_playcount_date (seção seguinte) em vez deste count.
SELECT COUNT(*) AS track_snaps
FROM spotify_track_snapshots WHERE date = :'d';

-- Artist (monthly_listeners / world_rank — linha compartilhada com o collector)
SELECT COUNT(*) AS artist_snaps
FROM spotify_artist_snapshots
WHERE date = :'d' AND monthly_listeners IS NOT NULL;

-- Top 5 cidades por artista
SELECT COUNT(*) AS top_cities
FROM spotify_artist_top_cities_snapshots WHERE date = :'d';

-- Playlists "discovered on"
SELECT COUNT(*) AS discovered_on
FROM spotify_artist_discovered_on_snapshots WHERE date = :'d';
```

**Como ler os números** — compare com um **dia cheio recente** (referência: o run validado de 2026-06-20 escreveu, em ~50 min, com 16 workers e sem OOM):

| Tabela | Dia cheio (2026-06-20) |
|---|---|
| `spotify_track_snapshots` | ~1.295.823 |
| `spotify_artist_snapshots` (monthly_listeners) | ~61.592 |
| `spotify_artist_top_cities_snapshots` | ~286.739 |
| `spotify_artist_discovered_on_snapshots` | ~1.584.722 |

Se as 4 batem o dia cheio anterior → run completo. Se `top_cities`/`discovered_on` vierem **zerados ou muito baixos** mas track veio cheio → era o sintoma clássico da 2ª bomba de RAM (a run morria na fase de artista depois de gravar tracks). Isso foi corrigido em 20/jun; se reaparecer, ver [Snapshots incompletos](#snapshots-incompletos).

O log estruturado de cada run também fica em `data/sync_runs/<timestamp>.json` (dentro do container), com `status`, `workers`, contagens e `rows_skipped_bad`.

---

## Verificar progresso sem scan gigante

> **⚠️ NUNCA rode `COUNT(*) ... WHERE date = ...` em `spotify_track_snapshots` para acompanhar progresso ao vivo.** Essa tabela tem **~33,7 milhões de linhas**, **sem índice em `date` e sem partição** — um filtro "só por date" vira um **scan da tabela inteira** (lento e pesado). Use o count por date só na verificação final pós-run, com paciência; para progresso **em andamento**, use os proxies abaixo.

**Proxy do progresso de tracks** — a coluna `spotify_tracks.latest_playcount_date` (mantida por trigger do Miner) avança conforme o sync grava playcounts. Contar quantas tracks já têm a data de hoje é **aceitável**: é um seq scan em ~1,08M linhas de `spotify_tracks` (a coluna **NÃO** tem índice), ~1-2s — bem mais leve que o scan de ~33,7M do `track_snapshots`:

```sql
SELECT COUNT(*) AS tracks_atualizadas_hoje
FROM spotify_tracks
WHERE latest_playcount_date = '2026-06-20';   -- data UTC do run
```

**Progresso das tabelas de artista** — essas são bem menores, então `COUNT(*) WHERE date = ...` é aceitável e dá uma noção de quanto da fase de artista já gravou:

```sql
SELECT COUNT(*) FROM spotify_artist_snapshots
  WHERE date = '2026-06-20' AND monthly_listeners IS NOT NULL;
SELECT COUNT(*) FROM spotify_artist_top_cities_snapshots WHERE date = '2026-06-20';
SELECT COUNT(*) FROM spotify_artist_discovered_on_snapshots WHERE date = '2026-06-20';
```

Resumo: **track → use `latest_playcount_date` como proxy; artista → conte direto nas 3 tabelas de artista.**

---

## Deploy é MANUAL

> **⚠️ NÃO existe auto-deploy no `git push` hoje** — o webhook do Coolify não dispara. (O README/RUNBOOK antigo dizia "Coolify auto-deploya no push" — **estava errado**, corrigido em 20/jun.) Depois de `git push origin main`, **o código novo NÃO entra em produção sozinho**: você precisa disparar o deploy à mão.

Duas formas:

1. **Botão Deploy no painel do Coolify** (app `scraper-spotify`) — mais simples.
2. **Via API** (ou pelo MCP `coolify-scraper`):
   ```bash
   curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
     "http://187.127.73.16:8000/api/v1/deploy?uuid=bd2yfhivgp2tiv6vdflem0ab&force=false"
   ```

Depois do deploy, a **próxima execução do cron** já roda o código novo. Para validar de fato, rode o sync e [verifique pelo banco](#como-verificar-um-run).

---

## Timeout 57014

**Sintoma:**
```
SupabaseError: ... 500: {"code":"57014","message":"canceling statement due to statement timeout"}
```

**Causa raiz:** uma query estourou os **8s** do `statement_timeout` do role `authenticator`. Acontece em:
- SELECT com `Prefer: count=exact` em tabelas grandes (COUNT seq_scan).
- UPSERT em batch grande quando há trigger ROW-level pesado.
- Query mal-indexada conforme tabela cresce.

**Diagnóstico:**
```sql
-- 1) Pegar o EXPLAIN da query problemática
EXPLAIN (ANALYZE, BUFFERS) <a query>;

-- 2) Ver triggers da tabela alvo
SELECT trigger_name, event_manipulation, action_orientation
FROM information_schema.triggers WHERE event_object_table = '<tabela>';

-- 3) Ver índices
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '<tabela>';
```

**Mitigações:**
1. **Em SELECT:** trocar `count=exact` por `count=estimated` ou usar keyset pagination (já implementado em `select_all`).
2. **Em UPSERT:** se trigger ROW-level está causando, migrar pra STATEMENT-level (ver migration `20260505200000_*.sql`). Mitigação rápida: reduzir `UPSERT_BATCH_SIZE` em `sync_from_supabase.py:66`.
3. **Em SELECT mal-indexado:** criar índice cobrindo o predicate.

---

## PGRST103

**Sintoma:**
```
416: {"code":"PGRST103","message":"Requested range not satisfiable",
      "details":"An offset of N was requested, but there are only M rows."}
```

**Causa raiz:** offset solicitado excede o total estimado pela tabela. Acontece com `Prefer: count=estimated` quando `pg_class.reltuples` (estimativa) < real.

**Solução definitiva:** keyset pagination — já implementada em `select_all()` desde o commit `421175f`. Se aparecer de novo, é porque algum lugar está usando offset+count manualmente. Procurar por `Prefer: count=` no código.

---

## Hash desatualizado

**Sintoma:**
```
HashOutdatedError: sha256Hash desatualizado para getAlbum
```
Ou nos logs do sync:
```
PersistedQueryNotFound em getAlbum
```

**Causa:** Spotify atualizou os SHA-256 das persisted queries.

**Como corrigir:**
```bash
# Roda discover_hashes que faz scrape do Web Player e atualiza config/settings.py
python -m scripts.discover_hashes --write

# Comita
git add config/settings.py
git commit -m "fix: atualiza SHA-256 dos persisted queries do Spotify"
git push
```

Depois do push, **dispare o deploy manualmente** (não é automático — ver [Deploy é MANUAL](#deploy-é-manual)). A próxima run da task volta a funcionar.

**Manual fallback (se script falhar):**
1. Abrir `https://open.spotify.com/album/<qualquer-id>` no Chrome.
2. DevTools → Network → filtro `api-partner`.
3. Achar a request `getAlbum`/`queryArtistOverview` etc.
4. Copiar `extensions.persistedQuery.sha256Hash` da URL.
5. Colar em `config/settings.py:GRAPHQL_HASHES`.

---

## Snapshots incompletos

**Sintoma:** o número de snapshots de um dia está menor que o esperado, sem erro explícito. Dois padrões diferentes:

- **Só `track` veio baixo:** cliente parando de paginar antes do fim (timeout silencioso após retry esgotado, bug de cursor, ou loop quebrando cedo em `len(batch) < page_size` quando PostgREST trunca).
- **`track` veio cheio MAS `top_cities`/`discovered_on` vieram zerados/baixos:** era a **2ª bomba de RAM** — a fase de artista acumulava ~1,9M linhas (1,58M discovered + 310k top_cities) na memória e estourava **OOM antes de gravar**, enquanto track já sobrevivia por ser incremental. **Foi a causa das falhas de 06-14 a 06-19 e está CORRIGIDA desde 20/jun** (commit `1933d7f`: o flush incremental do `BufferedUpserter` foi estendido à fase de artista, então a RAM fica baixa nas duas fases). Se este padrão reaparecer, suspeite primeiro de OOM (caps do `docker-compose.yml`) ou de um regressão no flush incremental.

> A **1ª bomba de RAM** (fase de tracks acumulando ~3M linhas pra gravar no fim) já tinha sido morta antes, também com flush incremental. Hoje as duas fases gravam em lotes durante a run via `src/buffered_writer.py` (`BufferedUpserter`).

**Diagnóstico:** verifique o run [pelo banco](#como-verificar-um-run) e compare as 4 tabelas com o dia cheio anterior. Veja qual(is) tabela(s) ficaram baixas — isso aponta a fase que falhou. Os logs da última run estão em `data/sync_runs/<timestamp>.json`.

---

## Token bloqueado

**Sintoma:** `403` em `https://open.spotify.com/get_access_token`.

**Causa:** IP residencial brasileiro é bloqueado pelo endpoint direto.

**Solução:** já temos fallback automático que extrai o token do `__NEXT_DATA__` de uma página de embed (Pink Floyd - DSOTM). Por padrão, **nem tenta** o endpoint direto (`TRY_DIRECT_TOKEN_ENDPOINT=0`). Se quiser tentar (ex: rodando em VPS com IP de outro país):
```bash
export TRY_DIRECT_TOKEN_ENDPOINT=1
```

---

## Rate limit Spotify

**Sintoma:** `429 Rate Limited` recorrentes.

**Diagnóstico nos logs:**
```
429 em getAlbum — pausando 30s
```

Após **3 consecutivos**, o cliente pausa **5 minutos** automaticamente.

**Mitigação:**
- Reduzir `DEFAULT_WORKERS` (default 20) em `config/settings.py`.
- Aumentar `GRAPHQL_DELAY_MIN/MAX` (default 0–0.1s).

Stress test (2026-04-15) mostrou que 4.4 req/s por worker é sustentável. 20 workers = 88 req/s teórico, na prática Spotify tolera bem.

---

## Aplicar nova migration no Supabase (self-host do Miner)

> Migrations rodam contra o **self-host** (`supabase.minermusic.com.br`, VPS 147.79.87.195), não o cloud antigo. E lembrando: mudança estrutural em `spotify_*` precisa **alinhamento com o Miner** (ver [Contrato com o Miner](#contrato-com-o-miner-não-violar)).

**Sempre:**
1. Escrever SQL com `BEGIN; ... ROLLBACK;` localmente pra testar.
2. Documentar header com motivo + SQL de rollback.
3. Smoke test com `EXPLAIN (ANALYZE, BUFFERS)` em transação.
4. Aplicar via `ssh miner-vps` + `psql` no container (mesma conexão de [Como verificar um run](#como-verificar-um-run)) ou pelo Studio do self-host.
5. Salvar arquivo em `miner-integration/supabase/migrations/<timestamp>_<nome>.sql`.
6. Commit do arquivo (sem precisar rebuild do app).

**Rollback de uma migration:** o header de cada migration tem o SQL de reversão. Basta rodar.

---

## Checklist quando o sync falha

1. ✅ A task rodou? (Ver Coolify → Scheduled Tasks → última execução.) **Lembre:** o status "Success" do Coolify **não é confiável** para runs > 60 min (ver [Como verificar um run](#como-verificar-um-run)).
2. ✅ O run gravou de fato? **Verifique pelo banco**, não pelo Coolify — conte as 4 tabelas por date no self-host.
3. ✅ Qual o erro exato no log? Ver `data/sync_runs/<timestamp>.json` no container.
4. ✅ É um dos casos do [atalho mental](#atalho-mental-decoreba)? Segue o procedimento.
5. ✅ Se `top_cities`/`discovered_on` ficaram baixos mas track cheio → suspeite de OOM na fase de artista (ver [Snapshots incompletos](#snapshots-incompletos)).
6. ✅ Logs de Postgres do self-host: `docker logs` do container do Postgres na VPS `miner-vps`.

---

## Contrato com o Miner (não violar)

As tabelas `spotify_*` são **propriedade do Miner**. O scraper só lê IDs e escreve snapshots. Regras duras (detalhe em `docs/architecture/2026-06-19-scraper-escalavel-design.md` §10.5):

- **`spotify_artist_snapshots` é LINHA COMPARTILHADA:** o collector grava `popularity`/`follower`, o scraper grava `monthly_listeners`/`world_rank`. O upsert do scraper é **PARCIAL** (só os 2 campos dele) — mandar `popularity`/`follower` **apagaria** o dado do collector.
- **`on_conflict` sempre EXPLÍCITO** = PK exata de cada tabela.
- **NÃO escrever `spotify_tracks.latest_playcount`** — trigger do Miner propaga sozinho.
- **NÃO fazer DELETE próprio** em `top_cities`/`discovered_on` — são geridas pelo servidor do Miner (merge SCD-2 às 15:30 UTC, poda top_cities 16:00, poda staging discovered 16:10).
- `sync_date` = data UTC única capturada no início da run, carimba tudo.

---

## Quando NÃO mexer

- **Não rodar `DROP TABLE`/`TRUNCATE` em `spotify_*`** — são propriedade do Miner, perda irrecuperável de snapshots.
- **Não desabilitar trigger** sem entender — `latest_playcount` em `spotify_tracks` depende dele.
- **Não trocar PK** das tabelas de snapshot — quebra o `ON CONFLICT` do upsert.
- **Não commitar `.env`** — credenciais expostas.

---

## Future work

Itens conhecidos rastreados no Jira (projeto **SS**, Epic **SS-1**):

- **SS-6 planner:** prioridade por popularidade, portão canário de frescor (só coletar se o Spotify já virou o dia), fila "não coletado hoje".
- **SS-9 sharding multi-IP:** `NODE_COUNT` + `hash(id) % N` quando o catálogo crescer 7-8x (~10M tracks/dia).
- **Outbox real (SQLite)** pra reenviar gravações que falharam (FK `23503` é raro hoje — o Miner não poda catálogo).
- **Fixar `--workers` no comando da task** (em vez de depender do env).
- **`dynamic_timeout` 3600 → 14400** no Coolify (só visibilidade — não afeta o processo).
- **Lado Miner:** tabela `spotify_sync_runs` (observabilidade, MMPDA-125); particionar `spotify_track_snapshots` por mês (MMPDA-82) **antes** do salto 7-8x; write-on-change (BLOQUEADO até consertar `avg_daily_delta` + `mv_genre_stats`).
- **`orjson`** pra parse mais rápido (alívio de CPU).
