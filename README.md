# Spotify Streams Scraper

Scraper que coleta do Spotify dados **não disponíveis na API oficial**: playcount por track, monthly listeners, world rank, top cities, "discovered on" playlists. Usa a **Partner GraphQL** (`api-partner.spotify.com`) com token anônimo do Web Player. Roda diariamente em produção via cron no Coolify e escreve em snapshots no Supabase do Miner.

> **Para entender em 30 segundos:** leia [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). **Pra debugar problemas:** [docs/RUNBOOK.md](docs/RUNBOOK.md). **Pra mexer no código:** [CLAUDE.md](CLAUDE.md).

## Onde roda

- **Cron diário:** Coolify VPS (`http://187.127.73.16:8000`) — task `sync-diario`, todo dia 09:00 SP (12:00 UTC)
- **Banco de destino:** Supabase Miner (`suzcbyzidnzzahwrkveh.supabase.co`)
- **Repo deployado:** branch `main`, auto-deploy no push

## Como rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate     # Linux/Mac
# .venv\Scripts\activate      # Windows
pip install -r requirements.txt

# .env precisa de:
#   SUPABASE_URL=https://suzcbyzidnzzahwrkveh.supabase.co
#   SUPABASE_SERVICE_ROLE_KEY=...

# Sync diário (igual o que roda no Coolify)
python -m scripts.sync_from_supabase --dry-run     # smoke test, não escreve
python -m scripts.sync_from_supabase --limit 10    # 10 albums + 10 artists
python -m scripts.sync_from_supabase               # run completa
```

## Comandos úteis (CLI)

| Comando | O que faz |
|---|---|
| `python -m scripts.sync_from_supabase` | Sync diário Supabase ↔ Spotify Partner GraphQL (rodada em prod) |
| `python -m scripts.run_daily` | Snapshot diário usando SQLite local (legacy/dev) |
| `python -m scripts.add_tracks --album <url>` | Adiciona álbum ao monitoramento (SQLite local) |
| `python -m scripts.export_csv --output report.csv` | Exporta dados do SQLite local |
| `python -m scripts.discover_hashes --write` | Redescobre os SHA-256 das persisted queries quando o Spotify atualiza |

## Stack

- **Python 3.12** + `httpx` async + `tenacity` (retry/backoff) + `rich` (logs)
- **PostgREST** (cliente customizado em `src/supabase_client.py` com keyset pagination)
- **20 workers async** em paralelo no sync
- **Docker / Coolify** para deploy

## Estrutura

```
.
├── config/settings.py                   Config: hashes, rate limits, env vars
├── src/
│   ├── auth.py                          Token anônimo (endpoint direto + fallback embed)
│   ├── graphql.py                       Cliente Partner GraphQL síncrono
│   ├── embed.py                         Cliente embed (fallback de metadata)
│   ├── supabase_client.py               Cliente PostgREST (keyset pagination)
│   ├── db.py                            SQLite local (modo legacy/dev)
│   ├── models.py                        Dataclasses
│   └── scraper.py                       Orquestrador modo legacy/dev
├── scripts/
│   ├── sync_from_supabase.py            🔵 PRODUÇÃO: sync diário Supabase ↔ Spotify
│   ├── run_daily.py                     Modo legacy SQLite local
│   ├── add_tracks.py / list_tracks.py   CLIs de gestão (legacy)
│   ├── export_csv.py                    Export do SQLite
│   └── discover_hashes.py               Redescoberta de SHA-256 quando Spotify atualiza
├── miner-integration/                   Cliente TypeScript do Miner (não versionado)
│   └── supabase/migrations/             Migrations aplicadas no Supabase
├── docs/                                ARCHITECTURE.md + RUNBOOK.md
├── CLAUDE.md                            Convenções e anti-padrões (leitura obrigatória)
├── CHANGELOG.md                         Histórico de mudanças
├── Dockerfile, docker-compose.yml       Imagem para Coolify
└── README.md                            (este arquivo)
```

## Sintomas comuns e onde olhar

| Sintoma | Onde investigar |
|---|---|
| Task falha com `57014 statement timeout` | [docs/RUNBOOK.md](docs/RUNBOOK.md#timeout-57014) |
| `PGRST103 Requested range not satisfiable` | [docs/RUNBOOK.md](docs/RUNBOOK.md#pgrst103) |
| `HashOutdatedError` ou `PersistedQueryNotFound` | [docs/RUNBOOK.md](docs/RUNBOOK.md#hash-desatualizado) |
| Snapshots/dia caindo aos poucos | [docs/RUNBOOK.md](docs/RUNBOOK.md#snapshots-incompletos) |

## Notas legais

Usa dados públicos do Web Player (sem login, sem dados privados). Pode violar ToS do Spotify — uso por sua conta e risco.
