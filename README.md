# Spotify Streams Scraper

Scraper para coletar dados do Spotify **não disponíveis na API pública oficial**:
- Play count (streams totais) de tracks
- Monthly listeners de artistas
- World rank, followers, top cities
- Daily streams (diferença calculada entre snapshots diários)

Usa a **Partner API GraphQL** (`api-partner.spotify.com`) com token anônimo do Web Player.

## Descobertas empíricas importantes

Testado em 2026-04-15 com o álbum `1FYY6MlQ0LmGY7aO8JEpG3` (Samuel Messias — Ainda Tem Promessa):

1. **A Embed API NÃO retorna playcount.** O HTML de `open.spotify.com/embed/album/<id>` e `/embed/track/<id>` tem metadata (nome, duração, artista, track IDs) mas nenhum campo de streams. A única fonte confiável de playcount é a **Partner GraphQL API**.
2. **A Embed é ótima como fonte de `accessToken`.** O `__NEXT_DATA__` da página de embed contém `state.settings.session.accessToken` — o MESMO token anônimo que o endpoint `/get_access_token` retorna. Útil quando o endpoint direto está bloqueado (403) ou com rate limit.
3. **Hashes validados hoje** (podem mudar):
   - `getAlbum`: `46ae954ef2d2fe7732b4b2b4022157b2e18b7ea84f70591ceb164e4de1b5d5d3` ✅
   - `queryArtistOverview`: `35648a112beb1794e39ab931365f6ae4a8d45e65396d641eeda94e4003d41497` ✅

## Status: MVP funcional

- ✅ Autenticação com duas fontes (endpoint direto + fallback via embed `__NEXT_DATA__`)
- ✅ Client GraphQL (getAlbum, queryArtistOverview) com retry/backoff e detecção de hash obsoleto
- ✅ Embed como fallback de metadata (registro de tracks de um álbum quando GraphQL falha)
- ✅ Persistência SQLite com snapshots diários, UPSERT idempotente e cálculo de daily_streams
- ✅ CLI: `add_tracks`, `run_daily`, `export_csv`
- ✅ **Validado**: 8 tracks + 1 artista extraídos com playcount real e monthly_listeners = 2.458.830
- ⚠️ Não suporta ainda: adicionar tracks via URL de playlist ou só artist_id
- ⚠️ Não faz descoberta automática de hashes (hardcoded — atualizar manualmente se quebrar)

## Setup

```bash
# Criar venv e instalar dependências
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
```

## Uso

### 1. Adicionar álbum ao monitoramento

```bash
python -m scripts.add_tracks --album "https://open.spotify.com/album/1FYY6MlQ0LmGY7aO8JEpG3"
# ou só o ID:
python -m scripts.add_tracks --album 1FYY6MlQ0LmGY7aO8JEpG3
```

Isso registra todas as tracks do álbum na tabela `monitored_tracks`, mas ainda não grava snapshot.

### 2. Rodar snapshot diário

```bash
python -m scripts.run_daily
```

Para cada álbum monitorado, busca play count atual de todas as tracks (agrupando por álbum = 1 request por álbum). Para cada artista distinto, busca overview (monthly listeners, followers, world rank). Grava snapshot do dia em `track_snapshots` / `artist_snapshots`. O `daily_streams` é calculado automaticamente como diferença com o snapshot mais recente anterior.

Se rodar duas vezes no mesmo dia, faz UPSERT (não duplica).

### 3. Ver status do DB

```bash
python -m scripts.run_daily --status
```

### 4. Exportar CSV

```bash
python -m scripts.export_csv --output data/report.csv
python -m scripts.export_csv --from 2026-04-01 --to 2026-04-15 --output abril.csv
```

## Atualizando os sha256Hash GraphQL

Quando o Spotify atualiza os hashes das persisted queries, o scraper vai receber `HashOutdatedError` e cair no Embed (que não tem monthly listeners — só play count).

Para atualizar:
1. Abra https://open.spotify.com/album/<qualquer-album> no Chrome
2. DevTools → Network → filtro "api-partner"
3. Encontre a request `getAlbum` ou `queryArtistOverview`
4. Copie o valor de `extensions.persistedQuery.sha256Hash` (parâmetro da URL)
5. Cole em `config/settings.py:GRAPHQL_HASHES`

## Arquitetura

```
scraper-streams/
├── config/settings.py       # Configurações (DB, rate limits, hashes)
├── src/
│   ├── auth.py              # Token anônimo do Web Player
│   ├── graphql.py           # Partner API client
│   ├── embed.py             # Embed API client (fallback)
│   ├── db.py                # SQLite com schema + upserts
│   ├── models.py            # Dataclasses
│   └── scraper.py           # Orquestrador
├── scripts/
│   ├── add_tracks.py        # CLI: adicionar álbuns/tracks
│   ├── run_daily.py         # CLI: snapshot diário
│   └── export_csv.py        # CLI: exportar dados
└── data/spotify_streams.db  # SQLite (criado automaticamente)
```

## Schema do banco

- `monitored_tracks`: tracks em monitoramento
- `monitored_albums`: álbuns em monitoramento (derivado)
- `monitored_artists`: artistas em monitoramento
- `track_snapshots`: snapshots diários de play count (+ `daily_streams` calculado)
- `artist_snapshots`: snapshots diários de monthly listeners, followers, etc.
- `daily_streams` (view): join amigável com percent change

## Notas

- **Rate limiting:** conservador por padrão (0.5–2s entre requests GraphQL, 1–3s entre embed). Se receber 3×429 seguidos, pausa 5min.
- **Legal:** acessa dados públicos do Web Player (sem login, sem dados privados). Pode violar ToS do Spotify — use por sua conta e risco.
- **Idempotência:** rodar `run_daily` 2× no mesmo dia faz UPSERT, não duplica.
