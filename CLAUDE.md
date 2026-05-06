# CLAUDE.md — Spotify Streams Scraper

> Convenções, anti-padrões e lições aprendidas. **Leia antes de mexer no código.**
> Atualize este arquivo quando aprender algo novo (regra: "PR não merga sem atualizar CLAUDE.md se mudou padrão").

## Contexto rápido

- **O que é:** scraper de dados do Spotify não-públicos (playcount, monthly listeners, etc).
- **Onde escreve:** Supabase do Miner (`suzcbyzidnzzahwrkveh`).
- **Quem dispara:** cron no Coolify às 09:00 SP/dia.
- **Tabela canônica:** as tabelas `spotify_*` no Supabase pertencem ao **Miner** — este scraper só **lê IDs** e **escreve snapshots**. **Não criar/dropar/alterar schema** sem alinhar com o Miner.

## Stack e versões

- Python 3.12 (Dockerfile usa `python:3.12-slim`)
- httpx[http2=False] async + tenacity + rich
- Postgres 17 (Supabase Cloud)
- PostgREST (não cliente oficial Supabase — temos `src/supabase_client.py` próprio)

## Regras críticas (não negociáveis)

### 🔴 Banco de dados

1. **NUNCA usar `Prefer: count=exact`** em SELECT em tabelas > 10k rows. Faz seq_scan que estoura `statement_timeout=8s`. Use `count=estimated` ou nada.
2. **Paginação grande = keyset, não offset.** Já temos `select_all(order_by=...)` em `src/supabase_client.py` — usa cursor por PK. Nunca voltar pra offset+count.
3. **UPSERT em batch + trigger ROW = bomba-relógio.** Triggers `FOR EACH ROW` em batches de 500 com índices estouram timeout. Usar `FOR EACH STATEMENT` com `REFERENCING NEW TABLE` (ver migration `20260505200000_*.sql` como exemplo).
4. **Toda migration deve ter rollback documentado no header.** Função antiga = renomear `_legacy_*`, não dropar. Smoke test antes de declarar pronto.
5. **`SECURITY DEFINER` sempre com `SET search_path = public, extensions`** (já é regra global, mas reforço aqui).

### 🔴 Auth e secrets

1. **Nunca commitar `.env`** ou tokens. `.gitignore` já cobre.
2. **Service role key e Coolify token** ficam só em env vars no Coolify e localmente em `.env`/`.mcp.json`. Nunca em código.
3. **Token anônimo do Spotify** é descartável (auto-renovado), mas mesmo assim não logar.

### 🔴 Mudanças de schema

1. **Tabelas `spotify_*` são propriedade do Miner.** Mudanças estruturais (DROP/ADD column, renomear PK, etc.) precisam alinhamento e migration no repo do Miner também.
2. **OK fazer:** ajustar trigger, criar índice ausente, ajustar constraint, desde que documentado.
3. **NÃO fazer:** mudar nome de tabela, dropar coluna, mudar tipo de PK.

## Padrões obrigatórios

### Cliente Supabase (`src/supabase_client.py`)

- `select_all(table, columns, where, order_by="id", page_size=1000)` — keyset pagination, sempre passar `order_by` explicitamente quando a coluna do cursor não for `id`.
- `upsert(table, rows, batch_size=500)` — batch padrão 500. Se aparecer timeout no upsert, primeiro investigue triggers da tabela; reduzir batch_size só como mitigação.
- `_retry_on_5xx` faz 4 tentativas com backoff exponencial em 5xx/rede. Não retentar em 4xx (FK violation, validação) — é dado errado, não rede.

### GraphQL Spotify (`src/graphql.py`)

- **Hashes em `config/settings.py:GRAPHQL_HASHES`** — quando quebrar, rodar `python -m scripts.discover_hashes --write`.
- **Detecção de hash desatualizado:** se response 400 + `PersistedQueryNotFound`, levantar `HashOutdatedError`.
- **Rate limiting:** delay 0–0.1s entre requests (calibrado em stress test 2026-04-15: 4.4 req/s sustentável). Em 429 com 3 consecutivos, pausa 5min.

### Sync (`scripts/sync_from_supabase.py`)

- 20 workers async em paralelo (default).
- Tracks com `playcount=None` são puladas (não viram snapshot). **Nunca usar `int(x or 0)` em playcount** — mascara campo faltando como zero.
- Ao final, escreve log estruturado em `data/sync_runs/<timestamp>.json` para auditoria.

## Anti-padrões aprendidos (não repetir)

| Anti-padrão | Por quê é ruim | Correto |
|---|---|---|
| `Prefer: count=exact` em select_all | COUNT seq_scan estoura timeout | `count=estimated` ou nada + keyset |
| Offset pagination | Inconsistente em tabelas que crescem; PostgREST devolve 416 quando offset > total estimado | Keyset pagination por PK |
| `int(x or 0)` em playcount | Mascara `None` (campo ausente) como zero — gera falsos snapshots | `if x is None: pula; else: int(x)` |
| Trigger `FOR EACH ROW` em tabela com 9 índices | Cada UPSERT em batch faz 500× UPDATE em massa, estoura 8s | `FOR EACH STATEMENT` + `REFERENCING NEW TABLE` |
| Loop while `len(batch) < page_size: break` | PostgREST trunca respostas grandes — quebra cedo | Em keyset: `if not batch: break` |
| `offset += page_size` | Quando `db-max-rows < page_size`, pula linhas | `offset += len(batch)` (ou abandonar offset) |

## Como debugar problemas comuns

Ver [docs/RUNBOOK.md](docs/RUNBOOK.md) para procedimentos passo-a-passo.

Atalho mental:
- **Timeout 57014** → query estourou 8s. Olhar plano + índices.
- **PGRST103** → offset > total. Migrar pra keyset.
- **HashOutdatedError** → rodar `discover_hashes`.
- **Snapshots/dia caindo** → cliente parando antes do fim. Ver paginação.

## Testes antes de mergeear

Antes de declarar "tá pronto":

1. **Sintaxe:** `python -m py_compile src/*.py scripts/*.py`
2. **Smoke local com limit:** `python -m scripts.sync_from_supabase --dry-run` ou `--limit 10`
3. **Se mexeu em SQL/migration:** rodar `EXPLAIN (ANALYZE, BUFFERS)` em uma transação `BEGIN; ... ROLLBACK;` antes de aplicar.
4. **Se mexeu no cliente Supabase:** validar que `select_all` ainda retorna o número correto de rows (compare com `SELECT count(*)` direto).
5. **Após deploy:** monitorar a próxima execução do cron e contar `spotify_track_snapshots` do dia.

## Deploy

- `git push origin main` → Coolify auto-deploya em ~60s.
- Disparar manualmente via API:
  ```
  curl -H "Authorization: Bearer $COOLIFY_TOKEN" \
    "http://187.127.73.16:8000/api/v1/deploy?uuid=bd2yfhivgp2tiv6vdflem0ab&force=false"
  ```
- Logs do app: `application_logs` no Coolify ou via API.

## Convenções de código (PT-BR vs EN)

- **Código, commits, identificadores:** inglês.
- **Comentários:** PT-BR ou inglês — ambos OK, **comentários explicam WHY** (a motivação, o constraint), não WHAT (o que o código faz já fica claro pelo nome).
- **Mensagens de UI/log visíveis ao admin:** PT-BR.
- **Commits:** PT-BR no corpo, descrevendo o "porquê" (não só o "o que").

## Onde estão as coisas

| Pra ver... | Vá em... |
|---|---|
| Migrations aplicadas | `miner-integration/supabase/migrations/*.sql` (parcialmente versionadas) |
| Logs de runs anteriores | `data/sync_runs/*.json` (no container do Coolify) |
| Histórico de mudanças | [CHANGELOG.md](CHANGELOG.md) |
| Diagrama do sistema | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Como debugar | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
