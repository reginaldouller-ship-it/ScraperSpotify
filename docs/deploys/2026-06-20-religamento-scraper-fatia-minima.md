---
type: hibrida              # infra (caps/compose/cron) + backend (sync/contrato/flush incremental)
scope: infra
importance: critica
status: done
tags: [scraper, sync, religamento, flush-incremental, oom, ram, flock, singleton, caps, docker, coolify, supabase, self-host, contrato-miner, workers, exit-code, deploy-manual]
related_incidents: [2026-06-19-cpu-saturada-sync-parado]
related_tasks: [SS-3, SS-6, MMPDA-125]
related_deploys: []
files_changed:
  - scripts/sync_from_supabase.py
  - src/buffered_writer.py
  - src/snapshot_dedup.py
  - src/singleton_lock.py
  - src/sync_status.py
  - docker-compose.yml
commits:
  - 29f5f88   # chore(sync): hardening pós-incidente (trava flock, exit-code por taxa, log robusto)
  - 1861a5e   # fix(sync): conformidade com o contrato do Miner (date UTC, on_conflict, dedup max, pula vazio)
  - 7d2e797   # chore(infra): caps CPU/RAM no compose + TZ=UTC + SYNC_WORKERS configurável
  - 1933d7f   # perf(sync): fase de artista incremental (mata a 2ª bomba de RAM)
duration_minutes: 240
requested_by: Bruno
linked_learning: null      # criar versão didática em escola-programacao-* se desejado
---

# Deploy — Religamento do scraper com a "fatia mínima-segura" (20/jun/2026)

> **Status:** concluído. **Coleta RELIGADA** após ~6 dias parada (incidente 12–19/jun).
> **Tipo:** deploy manual no Coolify (NÃO há auto-deploy no push — o webhook não dispara).
> **Validado:** run completo de ~50 min em 20/jun escreveu as **4 tabelas** em nível de dia cheio, **sem OOM**.

---

## 1. Contexto — por que esse deploy existe

A coleta diária estava **parada desde ~13/jun** (último dado: track/top_cities/discovered em 12/jun, artist em 13/jun). A causa foi diagnosticada no incidente [2026-06-19](../incidents/2026-06-19-cpu-saturada-sync-parado.md): a VPS do scraper é **1 vCPU / 4 GB** e roda **no mesmo núcleo o Coolify (painel) e o scraper**. O catálogo cresceu ~12× num mês (108 mil → 1,29 milhão de tracks/dia), a run passou de 21 min para 2 h+, saturou CPU (100%) e RAM (~4 GB de 4 GB), deixou **processos Python órfãos** e desestabilizou o próprio Coolify.

Em vez de fazer logo o redesenho grande (spec [2026-06-19-scraper-escalavel-design](../architecture/2026-06-19-scraper-escalavel-design.md)), optou-se por uma **fatia mínima-segura**: o conjunto **mínimo** de mudanças que (a) impede o incidente de se repetir e (b) deixa a coleta voltar a entrar diariamente, sem reescrever a arquitetura. As fases maiores (planner priorizado, sharding multi-IP, outbox) ficam para depois (Jira SS, Epic SS-1).

---

## 2. O que foi feito (as 4 mudanças)

São os 4 commits que entraram em `main` e foram deployados manualmente:

### 2.1 `29f5f88` — Hardening pós-incidente (SS-3)
- **Trava de instância única (`flock`)** em `src/singleton_lock.py`: se uma 2ª run for disparada enquanto a 1ª roda, ela **sai na hora** em vez de empilhar. Empilhamento foi a causa do incidente de 19/jun (duas runs no mesmo núcleo). **Validado em prod:** dois disparos saíram "sem empilhar".
- **Exit-code por taxa de falha** em `src/sync_status.py`: a run só sai com código 1 (`degraded`) se **mais de 1%** dos itens falhar. Um blip de rede isolado não vira mais "failed" — corrige o "failed falso" que mascarava runs que na verdade gravavam ~100%.
- **Log estruturado robusto**: cada run escreve `data/sync_runs/<timestamp>.json` (status, workers, contagens, `rows_skipped_bad`) no início e reescreve no fim.

### 2.2 `1861a5e` — Conformidade com o contrato do Miner
As tabelas `spotify_*` são **propriedade do Miner**; o scraper só lê IDs e escreve snapshots. Esse commit alinha o scraper às regras duras da spec (§10.5):
- **`sync_date` = data UTC única capturada no INÍCIO da run** e carimbada em todas as linhas (estável mesmo se a janela cruzar a meia-noite UTC; bate com o `date` UTC do collector).
- **Remove o DELETE próprio** em `top_cities`/`discovered_on` — a poda/merge é do servidor (merge SCD-2 15:30 UTC, poda top_cities 16:00, poda staging discovered 16:10). UPSERT por PK numa data nova basta.
- **`on_conflict` EXPLÍCITO** = PK exata de cada tabela.
- **Upsert PARCIAL** em `spotify_artist_snapshots` (LINHA COMPARTILHADA): o scraper escreve **só** `monthly_listeners`/`world_rank`; mandar `popularity`/`follower_count` apagaria o que o collector grava.
- **Dedup de track por MAIOR playcount** (`src/snapshot_dedup.py`) — o guard do Miner é por data, não por valor.
- **Pula artista vazio** (não gera snapshot fantasma).

### 2.3 `7d2e797` — Caps de recurso + TZ + workers configuráveis
- **Caps no `docker-compose.yml`**: `cpus=0.7`, `mem_limit=3g`, `memswap_limit=3g`. O **Docker impõe isso no kernel** → a VPS não satura nem dá OOM no host (o painel Coolify, no mesmo núcleo, fica protegido), mesmo se o scraper enlouquecer.
- **`TZ=UTC`** no container — não usar `America/Sao_Paulo` (mis-data perto da meia-noite UTC, que cai dentro da janela de coleta).
- **`SYNC_WORKERS` configurável por env** (default 20 no código). Permite ajustar a quantidade de workers sem mexer no código.

### 2.4 `1933d7f` — Fase de artista incremental (mata a 2ª bomba de RAM)
Estende o **flush incremental** (gravar em lotes pequenos DURANTE a run, via `BufferedUpserter` em `src/buffered_writer.py`) também à fase de artistas. Ver as duas bombas de RAM abaixo.

### As 2 bombas de RAM (a história importante)
O scraper acumulava linhas em listas na RAM e só gravava no fim — em escala de 1,29M tracks isso estourava a memória. Havia **duas** bombas:

1. **1ª bomba (tracks):** acumulava ~3 milhões de linhas para gravar no fim → OOM. **Corrigida** com flush incremental na fase de tracks.
2. **2ª bomba (artistas):** acumulava ~1,9 milhão de linhas (1,58M discovered_on + 310k top_cities) → OOM **antes mesmo de gravar os artistas**. Era a **causa das falhas de top_cities/discovered desde 06-14** — o playcount passava porque a fase de tracks já era incremental, mas a fase de artistas morria por falta de RAM. **Corrigida em 20/jun** estendendo o flush incremental à fase de artistas (`1933d7f`).

Com as duas corrigidas, a RAM fica **sempre baixa** nas duas fases.

`src/buffered_writer.py` também traz o `resilient_upsert`: se um lote falha com erro **4xx** (FK 23503 ou CHECK), ele reenvia **linha-a-linha pulando só a ruim** (registrada em `rows_skipped_bad`), sem derrubar a run inteira. Erro **5xx** (servidor/rede) propaga normalmente.

### Achado de performance (de brinde)
A métrica da VPS mostrou que o scraper é **rede-bound** (CPU fica em ~30%, não encosta no cap de 0.7) → **subir workers acelera** sem saturar. `SYNC_WORKERS` foi de **8 → 16** (~2× mais rápido) para a run de validação.

---

## 3. Evidência — a run de validação (20/jun)

Run completo em **~50 min**, escreveu **TODAS as 4 tabelas** para `2026-06-20`, **sem OOM**:

| Tabela | Linhas gravadas (20/jun) | Comparação |
|---|---|---|
| `spotify_track_snapshots` | **1.295.823** (de 1.295.824) | dia cheio (bate o 06-18) |
| `spotify_artist_snapshots` (monthly_listeners) | **61.592** | dia cheio |
| `spotify_artist_top_cities_snapshots` | **286.739** | dia cheio |
| `spotify_artist_discovered_on_snapshots` | **1.584.722** | dia cheio |

Pipeline **validado ponta a ponta**: as 4 tabelas voltaram a nível de dia cheio, sem OOM, com a trava `flock` confirmada (2 disparos não empilharam) e os caps de recurso impondo o teto no kernel.

> **Como conferir (sempre pelo BANCO, não pelo status do Coolify — ver Gotchas):**
> ```sql
> SELECT 'track' t, COUNT(*) FROM spotify_track_snapshots WHERE date = '2026-06-20'
> UNION ALL SELECT 'artist', COUNT(*) FROM spotify_artist_snapshots WHERE date = '2026-06-20' AND monthly_listeners IS NOT NULL
> UNION ALL SELECT 'top_cities', COUNT(*) FROM spotify_artist_top_cities_snapshots WHERE date = '2026-06-20'
> UNION ALL SELECT 'discovered_on', COUNT(*) FROM spotify_artist_discovered_on_snapshots WHERE date = '2026-06-20';
> ```
> Self-host: `ssh miner-vps "docker exec -i supabase-db-bax8nu79nywtkqoxyvb4lhtu psql -U postgres -d postgres"`.

---

## 4. Detalhes de infra (onde/como roda)

| Item | Valor |
|---|---|
| VPS | Hostinger **187.127.73.16** (1 vCPU / 4 GB, Frankfurt), Coolify |
| App | `scraper-spotify`, uuid **bd2yfhivgp2tiv6vdflem0ab** |
| Scheduled task | `sync-diario`, cron **`0 12 * * *`** = 09:00 BRT / 12:00 UTC, timeout **14400s (4 h)** |
| TZ do container | **UTC** (`TZ=UTC`) |
| Caps Docker | `cpus=0.7`, `mem_limit=3g`, `memswap_limit=3g` |
| Banco de destino | Supabase **self-hosted** `supabase.minermusic.com.br` (VPS 147.79.87.195) — **NÃO** o cloud antigo `suzcbyzidnzzahwrkveh` (aposentado pós-cutover de 13/jun) |
| Deploy | **MANUAL** (botão Deploy no Coolify ou via API/MCP). **NÃO há auto-deploy no push** — o webhook não dispara |

**Disparo manual via API:**
```
curl -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/deploy?uuid=bd2yfhivgp2tiv6vdflem0ab&force=false"
```

---

## 5. Rollback

Se a próxima run do cron quebrar ou o comportamento regredir:

1. **Tag de baseline pré-mudança:** `pre-flush-incremental-20260620` (estado de `main` antes dos 4 commits de hoje). `git checkout pre-flush-incremental-20260620` recupera o código anterior.
2. **Origin baseline:** o `main` remoto antes dos 4 commits é o ponto de retorno; reverter com `git revert` dos 4 (`29f5f88 1861a5e 7d2e797 1933d7f`) ou resetar para a tag.
3. **No Coolify:** usar **redeploy do build anterior** (redeploy-previous) — volta o container para a imagem pré-fatia-mínima em ~60s, sem precisar de novo push.

> ⚠️ Como **não há auto-deploy no push**, qualquer rollback de código só vale em prod **após um deploy manual** (ou o redeploy-previous do Coolify).

---

## 6. Itens abertos (próximos passos)

- **`dynamic_timeout` 3600 → 14400:** o setting de SERVIDOR do Coolify (`Deployment timeout (seconds)` / `dynamic_timeout = 3600` = 60 min) **corta o RASTREAMENTO** da execução aos 60 min e mostra "Success", **mas não mata o processo** (segue rodando órfão). Subir para 14400 dá visibilidade real de runs longas. **Ainda não feito.**
- **Fixar `--workers` no comando da task** (hoje depende do env `SYNC_WORKERS`) — torna o número explícito no comando do cron.
- **Cron de amanhã (21/jun, 12:00 UTC) é o 1º teste SEM supervisão.** A run de validação de hoje foi disparada à mão; falta confirmar que o cron agendado roda limpo sozinho. **Verificar pelo BANCO** (contagem por `date`), não pelo status do Coolify.

### Future work (Jira SS, Epic SS-1) — fora do escopo desta fatia
- **SS-6 planner:** prioridade por popularidade, portão canário de frescor (só coletar se o Spotify virou o dia), fila "não coletado hoje".
- **SS-9 sharding multi-IP:** `NODE_COUNT`, `hash(id) % N`, quando o catálogo crescer 7-8× (~10M tracks/dia).
- **Outbox real (SQLite)** para reenviar gravações que falharam (FK 23503 é raro hoje — o Miner não poda catálogo).
- **Lado Miner:** tabela `spotify_sync_runs` (observabilidade, MMPDA-125); particionar `track_snapshots` por mês (MMPDA-82) antes do salto 7-8×; write-on-change (BLOQUEADO até consertar `avg_daily_delta` + `mv_genre_stats`).
- **`orjson`** para parse mais rápido (CPU).

---

## 7. Gotchas conhecidos (para o RUNBOOK)

- **Coolify `dynamic_timeout=3600` mente sobre runs longas:** corta o rastreamento aos 60 min e mostra "Success", mas o processo segue rodando (órfão). **NÃO confiar no status do Coolify para runs longas** — verificar pelo BANCO (contar snapshots por `date` no self-host).
- **`track_snapshots` (~25M) não tem índice em `date` nem partição:** qualquer operação "só por date" vira scan gigante. Para checar progresso, usar `spotify_tracks.latest_playcount_date` (proxy), **nunca** `COUNT` em `track_snapshots` por `date`.
- **Atalhos de erro:** `57014` = query estourou os 8 s do `statement_timeout` (olhar índice/plano). `PGRST103` = offset > total (usar keyset). `HashOutdatedError` = rodar `scripts/discover_hashes.py`. **Snapshots/dia caindo** = run morrendo antes do fim (era a 2ª bomba de RAM).
