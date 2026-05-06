# RUNBOOK — Diagnóstico e operações

Guia operacional: o que fazer quando algo dá errado em produção.

## Sumário rápido

| Erro / Sintoma | Pula pra |
|---|---|
| `57014 statement timeout` | [Timeout 57014](#timeout-57014) |
| `PGRST103 Requested range not satisfiable` | [PGRST103](#pgrst103) |
| `HashOutdatedError` / `PersistedQueryNotFound` | [Hash desatualizado](#hash-desatualizado) |
| Snapshots/dia caindo aos poucos | [Snapshots incompletos](#snapshots-incompletos) |
| `403` em `/get_access_token` | [Token bloqueado](#token-bloqueado) |
| `429 Rate Limited` consecutivos | [Rate limit Spotify](#rate-limit-spotify) |

## Comandos de diagnóstico

### Listar app no Coolify
```bash
curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/applications/bd2yfhivgp2tiv6vdflem0ab" | jq
```

### Disparar deploy manual
```bash
curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/deploy?uuid=bd2yfhivgp2tiv6vdflem0ab&force=false"
```

### Ver scheduled tasks
```bash
curl -s -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "http://187.127.73.16:8000/api/v1/applications/bd2yfhivgp2tiv6vdflem0ab/scheduled-tasks" | jq
```

### Saúde dos snapshots no Supabase
```sql
-- Tendência diária
SELECT date, COUNT(*) AS snaps
FROM spotify_track_snapshots
WHERE date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY date
ORDER BY date DESC;

-- Estado atual
SELECT
  (SELECT COUNT(*) FROM spotify_tracks) AS tracks,
  (SELECT COUNT(*) FROM spotify_artists) AS artists,
  (SELECT COUNT(DISTINCT album_id) FROM spotify_tracks WHERE album_id IS NOT NULL) AS albums,
  (SELECT MAX(date) FROM spotify_track_snapshots) AS last_snapshot;
```

### Statement timeout dos roles
```sql
SELECT rolname, rolconfig FROM pg_roles
WHERE rolname IN ('authenticator','authenticated','anon','service_role');
```

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

Coolify auto-deploya. Próxima run da task funciona.

**Manual fallback (se script falhar):**
1. Abrir `https://open.spotify.com/album/<qualquer-id>` no Chrome.
2. DevTools → Network → filtro `api-partner`.
3. Achar a request `getAlbum`/`queryArtistOverview` etc.
4. Copiar `extensions.persistedQuery.sha256Hash` da URL.
5. Colar em `config/settings.py:GRAPHQL_HASHES`.

---

## Snapshots incompletos

**Sintoma:** o número de `spotify_track_snapshots` por dia está consistentemente menor que o esperado, sem erros explícitos. Pode estar caindo gradualmente.

**Causa típica:** cliente está parando de paginar antes do fim. Pode ser:
- Timeout silencioso (retry esgotou).
- Loop quebrando em `len(batch) < page_size` quando PostgREST trunca.
- Bug na lógica de cursor.

**Diagnóstico:**
```sql
-- Comparar com o estado real
SELECT
  date,
  COUNT(*) AS snaps,
  (SELECT COUNT(*) FROM spotify_tracks WHERE album_id IS NOT NULL) AS expected_max
FROM spotify_track_snapshots
WHERE date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY date ORDER BY date DESC;
```

Se `snaps` << `expected_max`, problema está na paginação. Ver logs da última run em `data/sync_runs/`.

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

## Aplicar nova migration no Supabase

**Sempre:**
1. Escrever SQL com `BEGIN; ... ROLLBACK;` localmente pra testar.
2. Documentar header com motivo + SQL de rollback.
3. Smoke test com `EXPLAIN (ANALYZE, BUFFERS)` em transação.
4. Aplicar via MCP do Claude (`apply_migration`) ou Supabase Studio.
5. Salvar arquivo em `miner-integration/supabase/migrations/<timestamp>_<nome>.sql`.
6. Commit do arquivo (sem precisar rebuild do app).

**Rollback de uma migration:** o header de cada migration tem o SQL de reversão. Basta rodar.

---

## Checklist quando o sync falha

1. ✅ A task rodou? (Ver Coolify → Scheduled Tasks → última execução.)
2. ✅ Qual o erro exato no log?
3. ✅ É um dos casos cobertos acima? Segue o procedimento.
4. ✅ Se não, ver `data/sync_runs/<timestamp>.json` no container pra detalhes.
5. ✅ Logs de Postgres: `mcp__supabase__get_logs service=postgres`.
6. ✅ Status do Supabase: dashboard.supabase.com → projeto `suzcbyzidnzzahwrkveh` → Logs.

---

## Quando NÃO mexer

- **Não rodar `DROP TABLE`/`TRUNCATE` em `spotify_*`** — são propriedade do Miner, perda irrecuperável de snapshots.
- **Não desabilitar trigger** sem entender — `latest_playcount` em `spotify_tracks` depende dele.
- **Não trocar PK de `spotify_track_snapshots`** — quebra o ON CONFLICT do upsert.
- **Não commitar `.env`** — credenciais expostas.
