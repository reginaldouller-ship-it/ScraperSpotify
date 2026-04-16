# Prompt para Claude Code — Spotify Streams Scraper

## Contexto

Preciso de um scraper Python que colete diariamente dados de músicas do Spotify que **não estão disponíveis na API pública oficial**, especificamente:

- **Play count (streams totais)** de cada track
- **Monthly listeners** de cada artista
- **World rank** do artista
- **Followers** do artista
- **Popularidade** (0-100) de tracks e artistas
- **Daily streams** (calculado pela diferença entre snapshots diários do play count)

## Fontes de dados (usar nesta ordem de prioridade)

### Método 1 — Spotify Internal GraphQL API (Partner API)

O Spotify usa uma API GraphQL interna no endpoint `https://api-partner.spotify.com/pathfinder/v1/query` para alimentar o web player. Essa API retorna play counts reais, monthly listeners, e outros dados ocultos.

**Como funciona:**
- O web player do Spotify faz requests GraphQL autenticadas com um token de acesso obtido via `https://open.spotify.com/get_access_token`
- O token não requer login — pode ser obtido com um simples GET request (client token anônimo)
- As queries GraphQL usam `operationName` e `sha256Hash` (persisted queries)
- Exemplos de operações úteis: `getAlbum` (retorna playcount por track), `queryArtistOverview` (retorna monthlyListeners, worldRank, stats)

**Exemplo de flow:**
```python
# 1. Obter access token anônimo
GET https://open.spotify.com/get_access_token?reason=transport&productType=web-player
# Retorna: {"accessToken": "BQ...", "accessTokenExpirationTimestampMs": ...}

# 2. Fazer query GraphQL
GET https://api-partner.spotify.com/pathfinder/v1/query
  ?operationName=getAlbum
  &variables={"uri":"spotify:album:ALBUM_ID","locale":"","offset":0,"limit":50}
  &extensions={"persistedQuery":{"version":1,"sha256Hash":"HASH_AQUI"}}
Headers:
  Authorization: Bearer {accessToken}
# Retorna: tracks com playCount, nome, duração, etc.
```

**Dados disponíveis por track via getAlbum:**
- `playcount` (número total de streams)
- `name`, `uri`, `duration`, `disc_number`, `track_number`
- `contentRating` (explicit/clean)

**Dados disponíveis por artista via queryArtistOverview:**
- `monthlyListeners`
- `worldRank`
- `followers`
- `topCities` (com listeners por cidade)
- `topTracks` com play counts
- `biography`
- `externalLinks` (social media)
- `stats.monthlyListeners`, `stats.followers`

**⚠️ Importante sobre os sha256Hash:**
Os hashes das persisted queries mudam periodicamente. O scraper precisa:
1. Primeiro tentar com hashes conhecidos
2. Se falhar (HTTP 400), fazer fallback para o Método 2
3. Implementar um mecanismo para descobrir novos hashes (parsear o JS do web player)

**Referência para descobrir hashes atuais:**
- Abrir o Spotify Web Player no navegador
- Na aba Network do DevTools, filtrar por `api-partner`
- Copiar os sha256Hash das requests

### Método 2 — Embed API (Fallback, mais estável)

A página de embed do Spotify retorna um JSON embutido no HTML com o play count.

**Como funciona:**
```python
# Request para a página de embed
GET https://open.spotify.com/embed/track/{TRACK_ID}
# Parsear o HTML para extrair o JSON do __NEXT_DATA__ ou similar
# O JSON contém: playCount, nome, artista, duração, capa

# Para álbuns (retorna play count de todas as tracks):
GET https://open.spotify.com/embed/album/{ALBUM_ID}
```

**Dados disponíveis via Embed:**
- `playCount` por track
- Metadados básicos (nome, artista, álbum, duração)
- Preview URL (30s de áudio)
- Capa do álbum

**⚠️ Limitações:**
- Não retorna monthly listeners (só disponível na GraphQL)
- Rate limits mais rígidos que a GraphQL API
- Pode precisar de proxies residenciais para volume alto

---

## Arquitetura do projeto

```
spotify-scraper/
├── config/
│   ├── settings.py          # Configurações (DB, rate limits, proxies)
│   └── tracks.csv           # Lista de track/album/artist IDs para monitorar
├── src/
│   ├── __init__.py
│   ├── auth.py              # Obtenção e renovação de tokens anônimos
│   ├── graphql.py           # Client para a Partner API GraphQL
│   ├── embed.py             # Client para a Embed API (fallback)
│   ├── scraper.py           # Orquestrador principal
│   ├── models.py            # Dataclasses / Pydantic models
│   └── db.py                # Persistência (SQLite ou PostgreSQL)
├── scripts/
│   ├── run_daily.py         # Entry point para execução diária
│   ├── export_csv.py        # Exportar dados para CSV
│   └── add_tracks.py        # Adicionar tracks/artists ao monitoramento
├── tests/
│   ├── test_auth.py
│   ├── test_graphql.py
│   └── test_embed.py
├── requirements.txt
├── docker-compose.yml       # Para rodar com scheduler
├── Dockerfile
└── README.md
```

## Requisitos técnicos detalhados

### 1. Módulo de autenticação (`auth.py`)
```python
class SpotifyAuth:
    """
    Gerencia tokens anônimos do Spotify Web Player.
    - Obtém token via GET https://open.spotify.com/get_access_token
    - Renova automaticamente quando expirar
    - Implementa retry com backoff exponencial
    - Rotaciona User-Agents para evitar bloqueio
    """
```

### 2. Client GraphQL (`graphql.py`)
```python
class SpotifyGraphQL:
    """
    Client para a Partner API GraphQL do Spotify.
    
    Métodos principais:
    - get_track_playcount(track_id) -> int
    - get_album_tracks_playcount(album_id) -> List[TrackPlaycount]
    - get_artist_overview(artist_id) -> ArtistOverview
      (monthlyListeners, worldRank, followers, topCities, topTracks)
    - get_artist_top_tracks(artist_id) -> List[TrackPlaycount]
    
    Implementar:
    - Pool de tokens (rotacionar entre múltiplos tokens anônimos)
    - Rate limiting inteligente (respeitar 429, backoff exponencial)
    - Cache de respostas (evitar requests duplicados no mesmo dia)
    - Fallback automático para Embed API quando GraphQL falhar
    - Logging detalhado de erros e rate limits
    """
```

### 3. Client Embed (`embed.py`)
```python
class SpotifyEmbed:
    """
    Client para a Embed API do Spotify (fallback).
    
    Métodos:
    - get_track_playcount(track_id) -> int
    - get_album_tracks_playcount(album_id) -> List[TrackPlaycount]
    
    Implementar:
    - Parsear HTML da página embed para extrair JSON
    - Buscar no <script id="__NEXT_DATA__"> ou equivalente
    - Headers realistas (User-Agent, Accept, etc.)
    - Rate limiting conservador (1-2 req/segundo)
    """
```

### 4. Modelos de dados (`models.py`)
```python
from dataclasses import dataclass
from datetime import date

@dataclass
class TrackSnapshot:
    track_id: str
    track_name: str
    artist_id: str
    artist_name: str
    album_id: str
    album_name: str
    playcount: int           # Total streams acumulados
    daily_streams: int | None  # Diferença com snapshot anterior
    popularity: int | None   # 0-100
    snapshot_date: date
    source: str              # "graphql" ou "embed"

@dataclass
class ArtistSnapshot:
    artist_id: str
    artist_name: str
    monthly_listeners: int
    world_rank: int | None
    followers: int
    top_cities: list         # [{"city": "São Paulo", "listeners": 123456}, ...]
    snapshot_date: date

@dataclass 
class DailyStreamReport:
    track_id: str
    track_name: str
    artist_name: str
    date: date
    total_streams: int       # Acumulado
    daily_streams: int       # Streams naquele dia
    daily_change_pct: float  # Variação percentual
```

### 5. Banco de dados (`db.py`)
```sql
-- Tabela de tracks monitoradas
CREATE TABLE monitored_tracks (
    track_id TEXT PRIMARY KEY,
    track_name TEXT,
    artist_id TEXT,
    artist_name TEXT,
    album_id TEXT,
    album_name TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Snapshots diários de streams (play count)
CREATE TABLE track_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT NOT NULL,
    playcount BIGINT NOT NULL,
    daily_streams BIGINT,          -- calculado: playcount_hoje - playcount_ontem
    popularity INTEGER,
    snapshot_date DATE NOT NULL,
    source TEXT DEFAULT 'graphql',  -- graphql ou embed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(track_id, snapshot_date)
);

-- Snapshots diários de artistas
CREATE TABLE artist_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id TEXT NOT NULL,
    monthly_listeners BIGINT,
    world_rank INTEGER,
    followers BIGINT,
    top_cities_json TEXT,           -- JSON com top cities
    snapshot_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(artist_id, snapshot_date)
);

-- View para daily streams
CREATE VIEW daily_streams AS
SELECT 
    t.track_id,
    t.track_name,
    t.artist_name,
    ts.snapshot_date,
    ts.playcount as total_streams,
    ts.daily_streams,
    ROUND(
        CASE 
            WHEN prev.playcount > 0 
            THEN (ts.playcount - prev.playcount) * 100.0 / prev.playcount 
            ELSE 0 
        END, 2
    ) as daily_change_pct
FROM track_snapshots ts
JOIN monitored_tracks t ON t.track_id = ts.track_id
LEFT JOIN track_snapshots prev ON prev.track_id = ts.track_id 
    AND prev.snapshot_date = date(ts.snapshot_date, '-1 day')
ORDER BY ts.snapshot_date DESC, ts.daily_streams DESC;
```

### 6. Orquestrador (`scraper.py`)
```python
class SpotifyScraper:
    """
    Orquestrador principal do scraping.
    
    Flow de execução diária:
    1. Carregar lista de tracks/artists do banco
    2. Agrupar tracks por álbum (otimização: 1 request por álbum = N tracks)
    3. Para cada álbum:
       a. Tentar GraphQL API (get_album com playcount de todas as tracks)
       b. Se falhar, tentar Embed API
       c. Se ambos falharem, logar erro e pular
    4. Para cada artista único:
       a. Buscar overview (monthly listeners, followers, world rank)
    5. Calcular daily_streams (diferença com snapshot anterior)
    6. Salvar snapshots no banco
    7. Gerar relatório resumo
    
    Otimizações:
    - Agrupar tracks por álbum reduz requests drasticamente
      (ex: 100k tracks em 20k álbuns = 20k requests em vez de 100k)
    - Paralelizar com asyncio (max 3-5 requests concorrentes)
    - Delay aleatório entre requests (0.5-2s)
    - Rotacionar tokens a cada ~100 requests
    - Retry com backoff: 1s, 2s, 4s, 8s, max 3 retries
    - Se receber 429, pausar por Retry-After header + margem
    
    Rate limiting sugerido:
    - GraphQL: ~1-2 requests/segundo
    - Embed: ~0.5-1 request/segundo
    - Renovar token a cada 30 minutos ou a cada 100 requests
    - Pausar 5 minutos se receber 3 erros 429 consecutivos
    """
```

### 7. Configuração (`settings.py`)
```python
# Banco de dados
DATABASE_URL = "sqlite:///spotify_streams.db"  # ou PostgreSQL

# Rate limiting
GRAPHQL_DELAY_MIN = 0.5       # segundos entre requests
GRAPHQL_DELAY_MAX = 2.0
EMBED_DELAY_MIN = 1.0
EMBED_DELAY_MAX = 3.0
MAX_CONCURRENT_REQUESTS = 3
MAX_RETRIES = 3
BACKOFF_FACTOR = 2

# Token
TOKEN_ROTATION_INTERVAL = 100  # requests antes de renovar token
TOKEN_REFRESH_MARGIN = 300     # segundos antes da expiração

# User Agents (rotacionar)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ...",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...",
]

# Proxies (opcional, recomendado para > 10k requests/dia)
PROXY_LIST = []  # ["http://user:pass@proxy1:port", ...]
```

## Input: Lista de tracks

O scraper deve aceitar input de 3 formas:

1. **Arquivo CSV** com colunas: `track_id`, `album_id`, `artist_id` (pelo menos uma)
2. **Playlist URL** — extrair todos os tracks de uma playlist do Spotify
3. **Artist URL** — extrair todos os tracks de um artista

Exemplo de CSV:
```csv
track_id,album_id,artist_id
4cOdK2wGLETKBW3PvgPWqT,,
,1XkGORuUX2QGOEIL4EbJKm,
,,7Ln80lUS6He07XvHI8qqHH
```

Se só o `artist_id` for fornecido, buscar todos os álbuns e tracks do artista.
Se só o `album_id` for fornecido, buscar todas as tracks do álbum.

## Output esperado

### 1. Banco de dados SQLite populado diariamente
### 2. CSV diário com resumo:
```csv
date,track_id,track_name,artist_name,total_streams,daily_streams,daily_change_pct,monthly_listeners,artist_world_rank
2026-04-15,4cOdK2wGLETKBW3PvgPWqT,Bohemian Rhapsody,Queen,2145678901,234567,0.011,45678901,12
```

### 3. Log de execução com:
- Total de tracks processadas
- Taxa de sucesso/erro
- Tempo total de execução
- Tokens consumidos/renovados
- Rate limits encontrados

## Dependências sugeridas

```
httpx[http2]          # HTTP client async com HTTP/2
aiosqlite             # SQLite async
pydantic>=2.0         # Validação de dados
tenacity              # Retry logic
python-dotenv         # Variáveis de ambiente
click                 # CLI
rich                  # Logging bonito no terminal
schedule              # Agendamento (alternativa ao cron)
```

## CLI esperado

```bash
# Rodar coleta completa
python -m scripts.run_daily

# Adicionar tracks por playlist
python -m scripts.add_tracks --playlist "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

# Adicionar tracks por artista
python -m scripts.add_tracks --artist "https://open.spotify.com/artist/7Ln80lUS6He07XvHI8qqHH"

# Adicionar tracks por CSV
python -m scripts.add_tracks --csv tracks.csv

# Exportar dados
python -m scripts.export_csv --from 2026-04-01 --to 2026-04-15 --output report.csv

# Status
python -m scripts.run_daily --status  # mostra total de tracks, último snapshot, etc.
```

## Notas importantes

1. **Resiliência**: O scraper precisa ser robusto contra mudanças na API do Spotify. Os hashes GraphQL mudam periodicamente. Implemente fallback para Embed API quando GraphQL falhar.

2. **Rate limiting**: Seja conservador. Melhor demorar 8 horas para rodar do que ser bloqueado. Para 100k tracks agrupadas em ~20k álbuns, a ~1 req/s com GraphQL, leva ~5.5 horas.

3. **Idempotência**: Se rodar duas vezes no mesmo dia, deve atualizar o snapshot existente (UPSERT), não duplicar.

4. **Monitoramento**: Logar todas as falhas e rate limits. Se a taxa de erro ultrapassar 20%, pausar e alertar.

5. **Proxies**: Para > 10k requests/dia, proxies residenciais são altamente recomendados. Sem eles, bloqueio de IP é provável.

6. **Legal**: Este scraper acessa dados publicamente visíveis no Spotify Web Player. Não faz login, não acessa dados privados, não baixa conteúdo protegido. No entanto, pode violar os Terms of Service do Spotify — use por sua conta e risco.

7. **Descoberta de hashes GraphQL**: Incluir um utilitário que faz download do JavaScript do web player e extrai os sha256Hash das persisted queries automaticamente. Isso evita que o scraper quebre quando o Spotify atualiza os hashes.
