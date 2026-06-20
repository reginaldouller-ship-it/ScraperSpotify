# Changelog

Histórico de mudanças significativas. Formato: `## YYYY-MM-DD — Título da mudança` + bullets curtos. Cada entrada referencia o(s) commit(s).

> Mudanças anteriores ao primeiro CHANGELOG: ver `git log --oneline` até `7d0c49b`.

---

## 2026-06-20 — Religamento seguro do scraper (pós-incidente VPS de 19/jun)

Repensada a resiliência do sync diário e religada a coleta **sem repetir** a saturação da VPS de 1 vCPU. Pipeline **validado ponta a ponta** (4/4 tabelas cheias em 06-20: track 1.295.823, artist 61.592, top_cities 286.739, discovered 1.584.722; run ~50 min, sem OOM).

**Commits:** `29f5f88`, `1861a5e`, `7d2e797`, `1933d7f`. Deploy-record: `docs/deploys/2026-06-20-religamento-scraper-fatia-minima.md`.

- **Hardening (SS-3, `29f5f88`):** trava de instância única (`flock` — mata o empilhamento que saturou a VPS), exit-code por **TAXA** de falha (>1%) em vez de 1 erro isolado, log estruturado robusto (stub início/fim em `data/sync_runs/`).
- **Conformidade de contrato (`1861a5e`):** `date` em **UTC** (1 `sync_date` no início), **remove o DELETE próprio** em top_cities/discovered (geridas pelo servidor: merge 15:30 / poda 16:00 UTC), `on_conflict` explícito = PK, dedup de track por **MAIOR** playcount, pula artist_snapshot vazio (protege a linha compartilhada).
- **Caps de recurso + infra (`7d2e797`):** `cpus=0.7` + `mem_limit=3g` + `memswap_limit=3g` no `docker-compose.yml` (o Docker impõe no kernel → VPS não satura nem dá OOM no host), `TZ=UTC`, `SYNC_WORKERS` configurável por env.
- **Flush incremental nas 2 fases (`1933d7f`):** as **2 "bombas de RAM"** (tracks ~3M linhas + artistas ~1,9M linhas, que acumulavam pra gravar no fim → OOM) viraram flush incremental via `BufferedUpserter`. A da fase de artista era a **causa das falhas de top_cities/discovered desde 06-14**.
- **Resiliência de escrita:** `resilient_upsert` isola erro 4xx (FK 23503 / CHECK) **linha-a-linha** em vez de derrubar o lote inteiro.
- **Velocidade:** `SYNC_WORKERS` 8→16 (scraper é **rede-bound**, CPU ~30% — confirmado por métrica da VPS) → run ~2x mais rápido.
- **Alvo de escrita:** self-host `supabase.minermusic.com.br` (o cloud `suzcbyzidnzzahwrkveh` foi aposentado no cutover de 13/jun).
- **Novos módulos:** `src/buffered_writer.py`, `src/snapshot_dedup.py`, `src/sync_status.py`, `src/singleton_lock.py` (+ testes em `tests/`).

## 2026-05-05 — Documentação inicial estruturada

Adiciona estrutura de docs para facilitar manutenção e onboarding.

- README.md reescrito (era de abril/2026 e não mencionava sync_from_supabase / Coolify / Supabase).
- CLAUDE.md do projeto criado (convenções, anti-padrões, lições aprendidas).
- docs/ARCHITECTURE.md criado (diagrama + componentes + decisões).
- docs/RUNBOOK.md criado (diagnóstico de erros comuns + comandos).
- CHANGELOG.md criado (este arquivo).
- ~/.claude/CLAUDE.md global atualizado com aprendizados de hoje.

## 2026-05-05 — Trigger ROW → STATEMENT em spotify_track_snapshots

**Commit:** `710102b` — *fix: trigger ROW → STATEMENT em spotify_track_snapshots (resolve UPSERT timeout)*

Resolve warnings 57014 (statement timeout) durante UPSERT em batches grandes na tabela `spotify_track_snapshots`.

- Migration aplicada: `miner-integration/supabase/migrations/20260505200000_spotify_track_snapshots_trigger_statement_level.sql`.
- Trigger antigo (`FOR EACH ROW`) substituído por dois novos (`FOR EACH STATEMENT` + `REFERENCING NEW TABLE`).
- Função antiga preservada como `_legacy_*` para rollback rápido.
- Smoke test (UPSERT 500 rows): **3.34s** vs ~8s+ antes.
- DDL only — não requer rebuild do app.

## 2026-05-05 — Keyset pagination em select_all

**Commit:** `421175f` — *fix: troca offset por keyset pagination em select_all (resolve PGRST103)*

Resolve erros 416 PGRST103 ("Requested range not satisfiable") quando o offset cruzava o total estimado pelo PostgREST.

- `src/supabase_client.py:select_all()` reescrito de offset+count para **keyset pagination** (cursor por PK).
- Novo parâmetro obrigatório `order_by` (default `"id"`).
- Imune a `db-max-rows` truncar respostas.
- Index Scan na PK → ~1s/página em 99k rows (medido).
- `scripts/sync_from_supabase.py` atualizado para passar `order_by="spotify_id"`.

## 2026-05-05 — Fix inicial do statement_timeout no SELECT

**Commit:** `e79d46b` — *fix: corrige sync_from_supabase parando no meio da paginação por timeout*

Resolve erros 57014 ("canceling statement due to statement timeout") na fase de carga inicial do sync.

- `src/supabase_client.py:select_all()`: header `Prefer` mudado de `count=exact` (~6s seq_scan em 99k rows) para `count=estimated` (instantâneo).
- Condição de saída do loop: `len(batch) < page_size` → `len(batch) == 0`.
- Incremento de offset: `+= page_size` → `+= len(batch)` (robusto a `db-max-rows` truncando).
- Substituído depois por keyset pagination (commit `421175f`).

## 2026-04-19 — Sync Supabase → Partner GraphQL

**Commit:** `7d0c49b` — *feat: adiciona sync Supabase → Partner GraphQL com 20 workers async*

Pipeline diário em produção. Lê IDs do Supabase do Miner, busca dados via Partner GraphQL com 20 workers async, escreve snapshots de volta no Supabase.

## 2026-04-15 — MVP

**Commit:** `822996e` — *feat: Spotify Streams Scraper — MVP completo + integração Miner*

Versão inicial: SQLite local, CLI (`add_tracks`, `run_daily`, `export_csv`), client Partner GraphQL (`getAlbum`, `queryArtistOverview`), embed como fallback de metadata, integração TypeScript com Miner.
