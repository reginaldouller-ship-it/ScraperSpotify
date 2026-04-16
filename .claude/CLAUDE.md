# CLAUDE.md — Spotify Streams Scraper

Instruções para o Claude Code trabalhando neste projeto. Leia antes de qualquer tarefa.

## Projeto

Scraper diário de dados do Spotify **não disponíveis na API pública oficial**:
- Play count (streams totais) por track
- Monthly listeners, followers, world rank, top cities por artista
- Daily streams calculado como diferença entre snapshots diários

**Stack:** Python 3.12 + httpx + tenacity + rich + SQLite (WAL). Sem async no MVP; será migrado.

## Arquitetura (estado atual)

```
config/settings.py              # 81 hashes GraphQL descobertos, rate limits, álbum fallback
src/auth.py                     # token anônimo (endpoint direto + fallback via embed)
src/graphql.py                  # Partner GraphQL client — única fonte real de playcount
src/embed.py                    # metadata-only (SEM playcount) + fonte de accessToken
src/credits.py                  # REST client /track-credits-view/ — pode retornar vazio
src/track_versions.py           # dedup de "mesma música" (live/acoustic/remaster)
src/db.py                       # SQLite: upsert idempotente, view daily_streams
src/models.py                   # dataclasses
src/scraper.py                  # orquestrador: add_album + run_daily
scripts/add_tracks.py           # CLI: --album / --track / --csv
scripts/run_daily.py            # CLI: snapshot diário + --status
scripts/export_csv.py           # CLI: export c/ filtro de datas
scripts/stress_test.py          # Fase 1 do stress test (rate limit threshold)
scripts/discover_hashes.py      # Descobre sha256Hash GraphQL automaticamente do JS bundle
scripts/test_new_queries.py     # Smoke test das novas queries com Samuel Messias
data/spotify_streams.db         # SQLite — fonte de verdade local
```

## Descobertas empíricas críticas (NÃO esqueça)

Validadas em 2026-04-15. Se o código quebrar, cheque se essas ainda valem antes de mudar a arquitetura.

### 1. A Embed API NÃO retorna playcount

O HTML de `open.spotify.com/embed/album/<id>` e `/embed/track/<id>` contém metadata (nome, duração, artista, track_id), mas **nenhum campo de streams**. A única fonte confiável de playcount é a **Partner API GraphQL**.

Consequência: se GraphQL falhar, embed serve só pra registrar tracks em `monitored_tracks` (sem snapshot). Nunca faça `int(playcount_do_embed or 0)` — vai salvar `0` e apagar o snapshot anterior.

### 2. A Embed É a melhor fonte de accessToken

O `__NEXT_DATA__` da página de embed inclui `state.settings.session.accessToken` com `isAnonymous: true`. É o MESMO token anônimo que o endpoint `/get_access_token` retorna, e funciona na GraphQL Partner API.

**Use isso como fallback quando `/get_access_token` retorna 403** (acontece em algumas redes/IPs sem motivo aparente). Implementado em `src/auth.py:SpotifyAuth._fetch_token_via_embed`.

### 3. Descoberta automática de hashes GraphQL

Implementada em `scripts/discover_hashes.py`. Extrai os `sha256Hash` diretamente do JS bundle do Web Player (`open.spotifycdn.com/cdn/build/web-player/web-player.*.js`) via regex.

**Uso:**
```bash
python -m scripts.discover_hashes            # comparar com settings.py
python -m scripts.discover_hashes --write    # atualizar settings.py
python -m scripts.discover_hashes --deep     # varrer chunks lazy-loaded (mais lento)
python -m scripts.discover_hashes --op queryArtistOverview  # filtrar
```

O bundle tem 80+ operations hoje. Rodar periodicamente (ou quando `PersistedQueryNotFound` aparecer) mantém hashes atualizados sem trabalho manual no DevTools.

### 4. Queries GraphQL implementadas em src/graphql.py

| Método | Operation | Retorna |
|---|---|---|
| `get_album(album_id)` | `getAlbum` | tracks com playcount, metadata |
| `get_artist_overview(artist_id)` | `queryArtistOverview` | monthly_listeners, followers, world_rank, top_cities |
| `get_track(track_id)` | `getTrack` | playcount individual, album, artists |
| `get_artist_discography_all(artist_id)` | `queryArtistDiscographyAll` | albums + singles + compilations + EPs (paginado, sem playcount por track) |
| `get_artist_discovered_on(artist_id)` | `queryArtistDiscoveredOn` | **playlists que impulsionam streams do artista** — dado indisponível na API oficial |
| `get_artist_related(artist_id)` | `queryArtistRelated` | artistas relacionados (**API oficial deprecou em 2024**) |

**80+ hashes disponíveis em `GRAPHQL_HASHES`** mas não todos têm método dedicado — o client `SpotifyGraphQL._query(op, vars)` aceita qualquer operation name que esteja no dict. Adicionar método novo = 20 linhas (ver `get_track` como referência).

### 5. Credits (REST, não GraphQL)

`src/credits.py:SpotifyCredits.get_track_credits(track_id)` usa o endpoint REST `https://spclient.wg.spotify.com/track-credits-view/v0/experimental/{track_id}/credits` com o mesmo token anônimo.

**⚠️ Comportamento observado (2026-04-15):** o endpoint responde 200 OK mas retorna `trackTitle=""` e `roleCredits[].artists=[]` para muitos tracks, mesmo artistas famosos (testado com Samuel Messias + Pink Floyd "Time" 2011 Remaster — ambos vazios). Possíveis causas:
- Endpoint migrou para versão autenticada (anonymous token pode não ter permissão)
- Spotify descontinuou credits para a maioria do catálogo
- Só funciona com `App-Platform: iOS` / `Android` / outro valor

**Ação recomendada:** manter o código (estrutura correta, pode voltar a funcionar), mas **não depender disso** para features críticas até descobrir o que mudou. Investigar:
1. Tentar com `App-Platform: iOS` / `mobile` no header
2. Tentar endpoint v1 ou v2 (`/v1/` / `/v2/` em vez de `/v0/experimental/`)
3. Tentar com header `Spotify-App-Version` diferente

### 6. Outras versões ("otherVersions")

**NÃO é uma query GraphQL** — é computado client-side. `src/track_versions.py` replica a lógica:
- `normalize_title(str)` — remove sufixos (Ao Vivo, Remaster, Acoustic, feat., etc.)
- `group_same_song(variants)` — agrupa por título normalizado + duração ±30s
- `pick_canonical(group)` — escolhe a versão "principal" (maior playcount > album > single > compilation)
- `find_duplicates(variants)` — retorna lista de duplicatas detectadas com canonical

**Uso típico no Miner:** evitar contar a mesma música N vezes num ranking quando existe "Original", "Ao Vivo", "Acoustic" e "Remaster" separados.

### 7. Benchmark de rate (stress test 2026-04-15, sua rede)

| Métrica | Valor |
|---|---|
| Requests sem 1 único 429 | **474 em 6min** |
| Latência p50 (getAlbum) | **219ms** (muito estável, stdev ~40ms) |
| Throughput teto sequencial | **~4.4 req/s** (limitado por latência, não pelo Spotify) |
| req/s em que apareceu 429 | **nenhuma até 4.4 req/s sustentados** |

**Implicação:** pra 100k tracks (50k req/dia), **1 IP basta**. Proxies residenciais são desnecessários nesse volume. Stage 2/3 do roadmap ficam adiados. Próximo ganho vem de **async concurrency** (3–5 workers async destravam ~15 req/s agregado no mesmo IP).

Resultados em `data/stress_test_20260415_184222/`.

## Regras de operação

### Segurança com a conta do usuário

O usuário tem **playlists valiosas** na sua conta pessoal do Spotify.

- **NUNCA autenticar o scraper com credenciais de usuário.** Só token anônimo.
- **NUNCA adicionar `client_id`/`client_secret` no código.**
- Se precisar de API oficial do Spotify (ex: script de backup de playlists), isso é um projeto separado fora do scraper. Não misturar.
- Scraping em IP residencial é tolerável pra volume atual (< 1k tracks/dia). Pra volumes maiores, recomendar rodar em VPS ou proxy.

### Regra obrigatória: testar com dados reais

Sempre que modificar algo no pipeline (`auth`, `graphql`, `embed`, `scraper`, `db`), você DEVE:

1. Rodar o fluxo de ponta-a-ponta (`add_tracks` + `run_daily` + inspeção via SQL direto no DB)
2. Verificar playcounts reais no banco (não 0, não None pra tracks vindas de GraphQL)
3. Reportar o resultado ao usuário com números concretos (quantas tracks, qual playcount, qual source)

Scripts já existentes pra validação:
```bash
python -m scripts.run_daily --status      # status do DB
python -m scripts.export_csv --output data/check.csv
sqlite3 data/spotify_streams.db "SELECT * FROM daily_streams LIMIT 5"
```

### Não mascarar erros

- **Não faça `playcount = int(tr.get('playcount') or 0)` em fonte embed** — salva dados falsos. Use `None` e pula o snapshot.
- **Não faça retry silencioso** de hash obsoleto — isso deve abortar e alertar o usuário pra atualizar `GRAPHQL_HASHES`.
- **Não faça fallback de playcount pra embed** — embed não tem esse campo. Se GraphQL falhou, a track perdeu o snapshot do dia. Melhor isso do que dado mentiroso.

### Idempotência

`track_snapshots` e `artist_snapshots` têm `UNIQUE(track_id, snapshot_date)` / `UNIQUE(artist_id, snapshot_date)`. Rodar `run_daily` 2× no mesmo dia faz UPSERT, não duplica. Essa invariante deve ser preservada em qualquer refactor.

## Convenções de código

- Commits em **português**, prefixos: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
- Mensagens de log em **português** (para o usuário)
- Docstrings e comentários em **português**
- Nomes de variáveis/funções em **inglês** (convenção Python)
- **NÃO criar arquivos .md desnecessários** — só `README.md` e este `CLAUDE.md`
- **Preferir editar arquivos existentes** a criar novos
- Credenciais/segredos via variáveis de ambiente (`python-dotenv`), nunca hardcoded
- Mantenha o `requirements.txt` enxuto — só dependências realmente usadas
- Use `httpx` (não `requests`) — async-ready e HTTP/2

## Roadmap e prioridades

### Status atual
- ✅ MVP funcional: registra álbuns, faz snapshot diário, exporta CSV
- ✅ Stress test Fase 1: 4.4 req/s sustentado, 0 erros em 474 req
- ⬜ Stage 1: async workers + checkpoint + observability
- ⬜ Stage 2: migração Postgres, auto-discovery de hashes (só se volume justificar)
- ⬜ Stage 3: fila distribuída (só se > 500k tracks)

### Próximo passo planejado (Stage 1)

Ordem sugerida (mas confirme com o usuário antes de começar):

1. `scripts/populate_from_playlist.py` — popular o DB com 500–2000 tracks reais de playlists populares (Top 50 Brasil, etc.)
2. Converter `scraper.run_daily` pra async: `httpx.AsyncClient` + `asyncio.Semaphore(5)`
3. Tabela `scrape_runs(run_id, album_id, status, completed_at)` + flag `--resume` no `run_daily`
4. Pool de 3 tokens anônimos obtidos em paralelo no startup, rotação por worker
5. Structured JSON logging num arquivo separado pra auditoria
6. Detector de hash obsoleto: se > 10% dos álbuns retornam `PersistedQueryNotFound`, aborta tudo

### O que NÃO fazer proativamente

- Não adicionar proxy pool (não precisa nesse volume — medido empiricamente)
- Não migrar pra Postgres (SQLite WAL aguenta 100k tracks tranquilo)
- Não adicionar fila Redis ou Celery (overkill)
- Não implementar auto-discovery de hashes agora (manual tá OK — mudam poucas vezes por ano)
- Não adicionar autenticação OAuth (fora do escopo — é scraper anônimo)
- Não criar docstrings/type hints novos em código que você não modificou

## Ambiente

- **Python:** 3.12 (path `C:\Users\jvict\AppData\Local\Programs\Python\Python312\python`)
- **SO:** Windows 11
- **Shell no Claude Code:** bash (use `/` em paths, não `\`)
- **Diretório do projeto:** `C:\Users\jvict\OneDrive\Documentos\Scraper Streams`

## Como debugar problemas comuns

### "playcount=0 em todas tracks"
→ Fonte foi embed, não GraphQL. Check `auth.py:_fetch_token` — provavelmente `/get_access_token` falhou e a extração via embed quebrou também (mudança no `__NEXT_DATA__`).

### "HashOutdatedError"
→ Atualizar `GRAPHQL_HASHES` em `config/settings.py`. Ver seção "Descobertas empíricas críticas" acima.

### "401 Unauthorized repetido"
→ Token anônimo expirou e o refresh está falhando. Check logs de `_fetch_token` — provavelmente 403 no endpoint direto **e** mudança no formato do embed. Atualizar regex `_NEXT_DATA_RE` ou o caminho `state.settings.session.accessToken`.

### "SQLite database is locked"
→ Outro processo abriu o DB. Se não for óbvio (outro script rodando), pode ser WAL residual — `PRAGMA wal_checkpoint(TRUNCATE)` limpa. Nunca apagar `.db-wal` enquanto o processo principal está rodando.

### Rate limit aparente mas sem 429
→ Throughput capado por latência, não por Spotify. Solução é async, não mais IPs. Ver "Benchmark de rate" acima.
