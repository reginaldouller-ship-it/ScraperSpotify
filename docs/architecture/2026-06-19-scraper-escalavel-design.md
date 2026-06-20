---
type: design
scope: backend
importance: critica
status: draft            # spec em revisão — nada implementado
tags: [escalabilidade, scraper, sharding, sqlite, rate-limit, multi-vps, supabase, pipeline, incidente-2026-06-19]
related_incidents: [2026-06-19-cpu-saturada-sync-parado]
related_tasks: [MMPDA-125]
requested_by: Bruno
---

# Design — Scraper Spotify escalável (pipeline priorizado + sharding multi-IP)

> **Status:** rascunho de spec para revisão do Bruno. **Nada implementado ainda.**
> **Objetivo:** redesenhar a coleta diária para (1) parar de saturar a VPS, (2) escalar
> para 7-8× o volume atual em curto prazo, e (3) crescer adicionando VPSs/IPs por
> configuração, sem reescrever código.

---

## 1. Contexto e problema

- O scraper lê IDs no Supabase do Miner, busca dados não-públicos do Spotify (Partner GraphQL)
  e grava snapshots de volta no Supabase do Miner (self-host `supabase.minermusic.com.br`).
- O catálogo cresceu **~12× em ~1 mês** (108 mil → 1,29 milhão de tracks/dia). A run diária,
  que levava 21 min, passou a levar **2 h+** num batch único, saturando a VPS de **1 vCPU / 4 GB**
  (CPU 100%, RAM no teto de OOM), deixando **processos órfãos** e desestabilizando o Coolify
  (incidente [2026-06-19](../incidents/2026-06-19-cpu-saturada-sync-parado.md)).
- Previsão: **mais 7-8×** em curto prazo (~10 milhões de tracks/dia, ~3M álbuns/dia).

### Correções de fato (apuradas ao vivo, não suposição)
- **"failed" no Coolify ≠ "não gravou".** O dado entrou diariamente no self-host até 18/jun.
  Os dois motivos do "failed": (a) timeout de 1h da task e (b) exit-code 1 a qualquer 1 falha.
- **O gargalo do 7-8× não é banco nem CPU — é a taxa de requests que o Spotify tolera por IP.**

---

## 2. Metas e não-metas

**Metas**
1. Cobertura **diária de todo o catálogo** (requisito de produto confirmado pelo Bruno).
2. Carga **baixa e constante** (sem pico de 100% / OOM) — cabe no 1 vCPU hoje.
3. **Escalável por configuração**: adicionar VPS/IP = mudar um número, sem reescrever.
4. **Resiliência**: não perder dado se um envio falhar; não empilhar runs; retomar de onde parou.
5. **Validação de frescor** ancorada em dado real (não gravar leitura stale como snapshot do dia).
6. **Observabilidade**: saber, de qualquer lugar, como cada run/nó se saiu.

**Não-metas (YAGNI por enquanto)**
- Microsserviços / múltiplos bancos. Mantemos **monólito modular** (regra "monolito por padrão").
- Buffer local em Postgres separado (o Supabase do Miner já É o armazenamento durável).
- Frescor intra-diário (sub-dia). O produto pede **diário**.

---

## 3. Dados empíricos que guiam o desenho (medidos em 17→18/jun)

| Medida | Valor | Implicação no desenho |
|---|---|---|
| Tracks com playcount **maior** dia-a-dia (catálogo ATIVO) | **~88%** | Catálogo ativo muda quase todo dia (3 pares saudáveis no self-host: 09→10, 11→12, 15→16 = 12,4/12,2/11,4% iguais). Playcount monotônico: "caiu" = anomalia (0,01%). |
| Tracks com playcount **igual** | **~12% (ativo); ~100% na cauda morta** | ⚠️ **CORRIGIDO:** o "74%" citado antes era **só o dia anômalo 06-18** (provável skip de refresh do Spotify, gravado como duplicata por falta de portão de frescor). No ativo, "igual" é raro; na cauda inativa (futura, no 7-8×) é a norma. |
| Artistas com `monthly_listeners` **mudado** | **94%** (58.224 / 61.642) | É o **termômetro de frescor** (muda quase sempre, p/ os dois lados). |
| Artistas com `world_rank` mudado | 0,4% | Inútil como sinal (fica em 0). |
| Taxa sustentável por IP (stress test 15/abr) | ~**60-75 req/s** | Define quando precisamos de outro IP (§8). |

**Campos coletados por tabela** (o que existe pra validar):
- `spotify_track_snapshots`: `playcount` (bigint, monotônico).
- `spotify_artist_snapshots`: `monthly_listeners`, `world_rank` (escritos pelo scraper) + `popularity`, `follower_count` (escritos pelo collector do Miner).
- `spotify_artist_top_cities_snapshots`: `rank, city, country, region, listeners`.
- `spotify_artist_discovered_on_snapshots`: `spotify_playlist_id, playlist_name, owner_*, position`.

---

## 4. Visão da arquitetura

Monólito **modular** (um codebase, módulos com responsabilidade única), rodando como um
**pipeline regulado por taxa** dentro de uma janela (madrugada agora, esticável até 24h),
**pronto para sharding multi-IP por configuração**.

```
FONTE DA VERDADE: Supabase do Miner (IDs + marcador "latest_playcount_date" = o que já foi feito hoje)

  POR NÓ (VPS/IP) — configurado com NODE_INDEX e NODE_COUNT
  ┌────────────────────────────────────────────────────────────────────────────┐
  │  freshness gate ─(Spotify já virou o dia?)─┐                                 │
  │                                            ▼                                 │
  │  planner ───────▶ [SQLite: fila + outbox] ───────▶ collector ───────▶ writer │
  │  (prioriza,            ▲                           (Spotify           (grava │
  │   filtra shard,        │ retry de falhas            GraphQL,           incre- │
  │   "não feito hoje")    └────────────────────────────┘  ▲  tokens)     mental)│
  │                              outbox reenvia o que falhou                  │  │
  └───────────────────────────────────────────────────────────────────────────┼──┘
       NODE_COUNT=1 hoje → 1 VPS.  Subir NODE_COUNT + clonar a VPS = +IPs (só config).
                                                                                │
  SAÍDA ─▶ snapshots incrementais ─▶ Supabase do Miner (spotify_*_snapshots)    ◀┘
       └─▶ resumo de cada run ─────▶ spotify_sync_runs (observabilidade)
```

### Módulos (responsabilidade única, testáveis isolados)
- **planner** — calcula a fila do dia: itens "ainda não coletados hoje" (via `latest_playcount_date`),
  ordenados por **prioridade**, **filtrados pelo shard do nó**. Não fala com Spotify.
- **collector** — pega item da fila, chama o Spotify, faz parse. Não conhece banco.
- **writer** — grava snapshots **incrementalmente** no Miner (lotes pequenos). Em falha, manda pro outbox.
- **tokens** — gerencia o token anônimo do Spotify (já existe em `src/auth.py`).
- **freshness gate** — decide se "o Spotify já atualizou hoje" antes de coletar em massa.
- **store** (SQLite local) — fila de trabalho + outbox de gravações que falharam. Não guarda cópia dos 10M snapshots.

---

## 5. Prioridade ("camadas")

Score por item, **mais importante primeiro**, derivado de colunas que já existem:

```
prioridade = normaliza(popularity) + normaliza(follower_count) + normaliza(latest_playcount)
desempate de cobertura: latest_playcount_date / last_synced_at mais ANTIGO primeiro
```

Garante que os grandes entrem nas primeiras ondas/horas e que **ninguém fique pra trás**
(o mais atrasado sobe na fila). Tiering é **ordem de processamento**, não exclusão — cobertura
continua sendo de tudo.

---

## 6. Validação de frescor (ancorada nos dados do §3)

**Portão canário (global, barato) — roda antes de coletar em massa:**
- Mede ~20-50 **canários** de alto tráfego (artistas + top tracks). Sinal principal:
  `monthly_listeners` dos artistas canário (muda em 94% → mais sensível) e/ou playcount dos top tracks.
- Se os canários **mudaram** vs o último valor que temos → "Spotify virou o dia" → **libera** a coleta.
- Se **não** mudaram → ainda não atualizou → **espera e tenta de novo** mais tarde na janela
  (não queima milhões de requests gravando duplicata stale).
- Usa um conjunto (não 1 só), porque até top track tem dia parado (visto na amostra).

**Aceitação por item, ao gravar** ("aceita" = **grava a linha do dia**, mantendo registro diário completo no Miner):
- Track: playcount **maior** → grava. **Igual** → **grava igual mesmo** (dia-parado real; é 74% do catálogo; **não** reagenda, **não** pula — sem buracos). **Menor** → rejeita (anomalia); writer nunca regride `latest_playcount`.
- Artista: grava `monthly_listeners`/`world_rank` lidos (o portão já garantiu frescor do dia).

**Reagendamento acontece SÓ em 2 casos** (não por "dado igual"):
1. **Portão canário** diz que o Spotify ainda não virou o dia → espera e tenta de novo mais tarde na janela.
2. A chamada ao Spotify **falhou** (rede/5xx/timeout) → vai pro **outbox** e reenvia depois.

---

## 7. Fila auto-esvaziante + resiliência

- **Fila do dia** = itens com `latest_playcount_date < hoje` (ou nulo), do shard do nó, ordenados por prioridade.
  Idempotente e **resumível**: ao gravar, o trigger atualiza `latest_playcount_date` → o item some da fila → se a run cair e voltar, continua de onde parou. **Não precisa de tabela de fila nova.**
- **SQLite local** guarda: (a) progresso/claim local do nó e (b) **outbox** — gravações que falharam (ex: Miner momentaneamente indisponível) pra **reenviar depois**. É pequeno (só falhas), não uma cópia dos 10M.
- **Flush incremental:** writer grava em lotes pequenos **durante** a run → RAM cai de ~4 GB pra MBs, e run interrompida não perde o lote inteiro.
- **Trava de instância única** (`flock`) por nó: a 2ª run no mesmo nó sai na hora (mata o empilhamento que causou o incidente). *(já preparada em `src/singleton_lock.py`)*
- **Throttle por taxa:** o collector respeita um teto de req/s (`SYNC_MAX_RPS`, default conservador ~50) → carga constante, sem pico, sem 429.

---

## 8. Escala horizontal: quando pegar outra VPS e como coordenar

### Janela de coleta
**09:00–23:00 horário do Brasil (BRT, UTC−3) = 12:00 UTC → 02:00 UTC(+1) = 14 h.**
Mantemos o cron atual **`0 12 * * *`** (= 09:00 BRT; o Coolify roda o cron em UTC). O container fica em
**UTC** — **não** mudar pra `America/Sao_Paulo` (isso mis-data perto da meia-noite UTC, que cai DENTRO da
janela). Em vez disso, capturamos **um único `sync_date` = data UTC no INÍCIO da run** e carimbamos todas
as linhas com ele: mantém o "dia" estável mesmo que a janela cruze a meia-noite UTC, e bate com o collector
(que grava `date` em UTC). A fila "não coletado hoje" usa esse mesmo `sync_date`. *(Já implementado em
`sync_from_supabase.py`: `snapshot_date` é capturado uma vez no início e propagado a todos os workers —
nenhum `date.today()` por item.)*

**Dependência de deploy — ✅ SATISFEITA (merge LIVE em prod, PR #157, 20/jun):** o merge do Miner agora processa
**só dias fechados** (`date < CURRENT_DATE`), aplicado no self-host (SSH→psql, dry-run + verify=t). Antes ele
processava o dia parcial → artistas coletados após 15:30 UTC seriam pulados. Com o fix vivo, o planner (SS-6)
pode "relaxar" a coleta de `discovered_on` (deixar de priorizar antes das 15:30) **sem gap**. Trade-off aceito:
o Miner consolida com 1 dia de atraso determinístico (D+1). *(A ordem de deploy que preocupava — fix do merge
antes do planner relaxado — já está resolvida: o fix landou primeiro.)*
**Trade-off (decisão do Bruno, registrada):** "dias fechados" faz o `discovered_on` refletir sempre com **1 dia
de atraso (D+1)** — em troca de ninguém ser pulado e comportamento determinístico. Para um produto diário é
aceitável. Alternativa descartada: mover o cron do merge pra depois da janela (depende do horário-fim ser estável;
"dias fechados" é mais simples/robusto).

### Quando (gatilho por números) — CORRIGIDO
**Importante:** os ~75 req/s observados eram o **teto da VPS de 1 vCPU** (CPU saturando no parse de JSON),
**não um limite do Spotify** — em 64 runs houve **ZERO 429**. O teto real por IP do Spotify é
**desconhecido (≥75 req/s, provavelmente bem mais)**: nunca chegamos nele porque a CPU travava antes.

Implicação: **otimizar o código** (orjson + flush incremental + espalhar na janela) **aumenta os req/s
na MESMA VPS** (menos CPU por request). Conta com a janela de 14 h:

| req/s sustentado por IP | Capacidade na janela de 14 h | Cobre |
|---|---|---|
| 75 (teto atual da VPS, sem otimizar) | ~3,8 M req | ~7,5× o atual |
| 100 (VPS otimizada) | ~5,0 M req | ~10× |
| 150 (se o Spotify tolerar) | ~7,6 M req | ~15× |

- **Hoje** (~0,5 M req/dia): sobra absurda.
- **No 7-8×** (~4 M req/dia): **provavelmente 1 VPS otimizada já fecha na janela de 13 h.**

**Regra de decisão pra 2ª VPS/IP (entra no plano):** adicionar quando **qualquer** for verdade:
1. **429 sustentado** num IP (aí sim achamos o teto real do Spotify), **ou**
2. a VPS **otimizada** não consegue fechar o catálogo dentro da janela de 13 h.

Ou seja: **primeiro otimizamos e medimos** (subindo req/s com cautela, de olho em 429), e só pegamos a
2ª VPS quando o dado provar necessário — não por um número chutado. Métricas no `spotify_sync_runs` (§9):
`requests/dia`, `req/s`, `429/dia`, `% do catálogo coberto na janela`.

### Como coordenar vários IPs (a melhor forma) — sharding determinístico
- Cada nó recebe **`NODE_INDEX`** (0..N-1) e **`NODE_COUNT`** (N) por env.
- O planner de cada nó processa **só** os itens onde `hash(spotify_id) % NODE_COUNT == NODE_INDEX`.
- **Sem colisão por construção** (shards disjuntos) → rodar simultâneo é seguro, sem coordenação fina.
- **Balanceado** (hash distribui igual) e **elástico**: subir de 1 p/ 3 IPs = clonar a VPS e setar
  `NODE_COUNT=3` + `NODE_INDEX=0/1/2`. **Zero mudança de código.**
- "O que já foi coletado" continua visível a todos via `latest_playcount_date` no Miner (fonte única da verdade).
- **Construímos com `NODE_COUNT=1` desde já** → a 2ª VPS é só configuração no futuro.

> ⚠️ **Trade-off de risco (ver §12):** mais IPs também significa **mais superfície de detecção/ban** pelo
> Spotify (API não-oficial). Escalar IPs exige ritmo conservador por nó, comportamento de navegador real
> e tratar ban como esperado. Escala e anti-detecção andam juntas.

**Evolução opcional (só se precisar de failover automático):** trocar sharding fixo por **fila com
claim atômico** numa tabela no Miner (`UPDATE ... SET claimed_by=nó WHERE ... RETURNING`), aí qualquer
nó assume o trabalho de um nó que caiu. Mais robusto, mais complexo — adiar até virar dor real.

**Por que não "1 serviço por VPS":** álbuns são ~90% dos requests; separar "álbuns numa VPS, artistas
noutra" deixa uma VPS ociosa e a outra no talo. Sharding do trabalho dominante equilibra de verdade.

---

## 9. Observabilidade (MMPDA-125)

- Tabela **`spotify_sync_runs`** no Supabase do Miner: 1 linha por run/onda/nó com
  `node_index, started_at, finished_at, status, duration_s, requests, req_por_s, albums_ok/fail,
  artists_ok/fail, http_429, gate_resultado, escritas_por_tabela`.
- Consultável por SQL de qualquer lugar (Miner, dashboard, eu) e base de **alerta**
  (ex: `429/dia > X` → hora de outro IP; `status=degraded` → investigar).
- **Tabela nova no banco do Miner → eu drafto a migration, alinho com o Miner, e o Bruno aplica
  com dry-run (nunca um agente).**
- Enquanto isso, o `data/sync_runs/*.json` robusto (escrito no início e no fim) + logs de execução do Coolify já dão monitoramento básico. *(P0 já preparado)*

---

## 10. "Gravar só mudanças" — REBAIXADO (não faremos agora)

**Decisão (Bruno):** o dado deve ficar **sempre completo e consistente no Supabase do Miner**, e há
match por **ISRC** a garantir. Então o **default é gravar a linha diária de TODO track, mesmo quando
o playcount não mudou** (replicado/as-is) — registro diário completo, sem buracos.

Contexto pra revisitar no futuro (CORRIGIDO com dado real): o catálogo **ativo** muda quase todo dia —
só **~12%** dos tracks ficam iguais (3 pares saudáveis). O "74%" era o **dia anômalo 06-18**. A economia
de write-on-change só é grande na **cauda morta** (~100% imutável no 7-8×), não no ativo. Além disso, o
Miner confirmou **2 consumidores que QUEBRAM** com dias faltando — `avg_daily_delta` (usa `LAG` em dias
consecutivos) e `mv_genre_stats` (banda fixa ±3 dias) — então write-on-change só **depois** de fixes SQL
do lado Miner (ver "Contrato com o Miner"). O volume real no 7-8× resolve-se com **particionamento
(Timescale, lado Miner)**, não com write-on-change.

---

## 10.5 Contrato com o Miner Music (CONFIRMADO — investigação read-only 19/jun)

Apurado lendo migrations + código do collector + queries read-only no banco vivo (e um workflow read-only do lado Miner). **Nada escrito em prod.**

**Regras duras (o scraper redesenhado DEVE respeitar):**
- `spotify_artist_snapshots` é **linha COMPARTILHADA** (PK `(artist,date)`): collector escreve `popularity`/`follower_count`, scraper escreve `monthly_listeners`/`world_rank`. **Upsert PARCIAL — só as colunas do scraper.** Mandar `popularity`/`follower_count` (mesmo NULL) **apaga** o dado do collector. ✅ O código atual já faz certo — **travar com teste de regressão**.
- **playcount por `spotify_id`**, nunca dedupe por ISRC. ISRC é resolvido na camada canônica (`tracks`, `UNIQUE(isrc)`) pelo Miner via trigger de auto-cura. (14,1% dos ISRCs têm 2+ spotify_id — é normal; máx 142.)
- **NÃO escrever `latest_playcount`/`latest_playcount_date`** — trigger statement-level propaga do snapshot (só avança, nunca regride).
- **NÃO tocar** `last_synced_at`/`discography_synced_at` (collector) nem as tabelas `_runs`/SCD-2. discovered_on/top_cities: escrever **só na staging diária**.
- **NÃO fazer DELETE próprio** em `top_cities`/`discovered_on` — a poda/merge é do servidor. O código tinha um DELETE-then-INSERT; **removido 20/jun** (o UPSERT por PK basta numa data nova). Cross-check no banco: não era corrupção permanente (o merge fecha vigência por **EXISTS**, não por ausência; top_cities fica stale ≤1 dia), mas era risco sem ganho — a única coisa que "resolvia" era a re-run no mesmo dia, que a trava single já elimina.
- **FK**: o `spotify_id` precisa existir em `spotify_tracks`/`spotify_artists` (populado pelo collector) ANTES do snapshot. Tratar dependência de ordem.
- **NUNCA podar/deletar histórico** (keep-forever). Só o Miner poda staging (7d) via cron.
- **service_role** + URL/key por **env** (self-host primário desde 13/06; cloud = fallback congelado, pausa ~início de julho).

**Timing (crons vivos no self-host, confirmados 19/jun):**
- `merge_discovered_on_runs` **15:30 UTC** · `prune_discovered_on_staging` 16:10 UTC · `prune_top_cities` 16:00 UTC · refresh de MVs 06/14/22 UTC.
- ✅ **(Resolvido — PR #157, merge "dias fechados"):** a nota antiga "coletar `discovered_on` antes das 15:30 UTC senão atrasa 1 dia" está **OBSOLETA**. O merge consolida só `date < CURRENT_DATE`, então o `discovered_on` de hoje entra **sempre em D+1, independentemente da hora** da coleta. O planner (SS-6) NÃO precisa priorizar discovered_on de manhã.

**Carry-forward (write-on-change) — BLOQUEADO até o Miner corrigir 2 objetos:**
- Quebram com dias faltando: `avg_daily_delta` (`LAG` em dias consecutivos) e `mv_genre_stats` (banda fixa ±3 dias). Degrada: `get_genre_growth_chart` (soma por dia).
- Seguros (último-valor / delta gap-tolerante via `≤ now-N ORDER BY date DESC LIMIT 1`): a maioria, incl. `get_movement_feed`.
- **Veredito:** manter **linha diária** pra playcount/monthly_listeners (já era a decisão). top_cities/discovered_on já colapsam no servidor (SCD-2 / latest-only) — esses PODEM ser menos-frequentes se precisar.

**Particionamento:** nenhuma tabela de snapshot é particionada hoje; `track_snapshots` (~33,7M, medido `reltuples` 20/jun) **sem índice em `date`**. Plano = **Timescale na Fase 4 (lado Miner)**. A 7-8× é trabalho do Miner — coordenar.

## 11. Plano de implementação (fases)

| Fase | O que | Depende de |
|---|---|---|
| **0 — Hardening (pronto, em diff)** | exit-code por taxa de falha + log robusto + **trava de instância** | OK do Bruno p/ commit + deploy |
| **1 — Pipeline numa VPS** | refatorar em módulos (planner/collector/writer/tokens) + **flush incremental** + **throttle** + **portão canário** + fila auto-esvaziante + SQLite outbox + `NODE_COUNT=1` | Fase 0 |
| **2 — Observabilidade** | tabela `spotify_sync_runs` (migration draftada) | alinhamento Miner + Bruno aplica |
| **3 — Janela/automação** | rodar como ondas na madrugada; esticar janela conforme volume | Fase 1 |
| **4 — Multi-IP (quando §8 disparar)** | clonar VPS + `NODE_COUNT=N` (config only) | gatilho de volume/429 |
| **5 — opcional** | "só-mudanças" (carry-forward) | alinhamento Miner |

Cada fase entra como diff revisável; **nada vai pra prod sem o Bruno, com dry-run.**

---

## 12. Riscos e pontos abertos

### 🔴 Risco nº 1 (existencial) — a camada de acesso ao Spotify é NÃO-OFICIAL e combatida
Todo o serviço depende da **Partner API interna** (`api-partner.spotify.com/pathfinder`) + token
anônimo do Web Player. A **Web API oficial NÃO expõe** playcount, monthly listeners, world rank,
top cities nem discovered on (só `popularity` 0-100) — então **não há migração possível** para o
caminho legítimo nesses dados. Fatos (jun/2026):
- O endpoint direto `get_access_token` passou a exigir **TOTP** (~mar/2025); estamos blindados porque
  usamos o fallback de **embed `__NEXT_DATA__`** por padrão. A nota do RUNBOOK sobre o endpoint direto está desatualizada.
- A Spotify **quebra o mecanismo recorrentemente** (restrições dez/2025; update 06/fev/2026 derrubou scrapers).
  Nosso método sobreviveu (dado entrou até 18/jun/2026), mas é **gato-e-rato**.
- Implementações atuais mandam header **`client-token`** (de `clienttoken.spotify.com`); nós definimos a
  URL mas **não usamos** → ponto de quebra latente.
- A Spotify **removeu endpoints em nov/2024 citando "scraping"** como motivo e restringiu acesso em mai/2025.
  Ou seja: postura ativamente hostil.

**Implicação direta na escala:** **mais IPs (sharding) AUMENTA o risco de detecção/ban.** Escala e
redução-de-risco têm que andar juntas. Mitigações que entram no desenho:
1. Camada de acesso como cidadã de 1ª classe: token robusto (embed→TOTP/`client-token` se quebrar) +
   **preflight que valida token+hash antes da run inteira** + **alerta** quando o método quebrar.
2. Cada nó se comporta como navegador real (UA/headers/pacing) + backoff/rotação; **ritmo conservador**.
3. Tratar ban de IP como **esperado** (detectar, pausar, alternar) — não como exceção rara.
4. Não ampliar a superfície na API frágil: oficial p/ `popularity`/`followers`, Partner só p/ o exclusivo.

**Risco de ToS/negócio:** o método viola os Termos do Spotify (o README já admite). É decisão de
**negócio do Bruno**, não técnica — registrada aqui para visibilidade.

### Outros pontos abertos
- **Alinhamento com o Miner** (donos das tabelas `spotify_*`): tabela `spotify_sync_runs` (§9) e
  a alavanca "só-mudanças" (§10) tocam o banco deles.
- **SSH no host do scraper** (187.127.73.16): hoje sem acesso → configurar pra operar/depurar nós.
- **Janela vs 7-8×:** sem esticar a janela ou adicionar IP, 6 h não comporta o 7-8× (§8).
- **Canário representativo:** escolher canários que realmente refletem o "refresh diário" do Spotify.
- **`hash(spotify_id)` estável** entre nós (usar hash determinístico, ex: hashlib, não `hash()` do Python que varia por processo).

---

## 13. Testes

- **Puro/unit** (sem rede): score de prioridade, decisão de aceitação (subiu/igual/caiu), função de shard
  (`hash % N`), decisão do portão canário, `decide_run_status` *(já testado)*, lock *(já testado)*.
- **Integração** (com mock do Spotify e SQLite real): fila auto-esvaziante, outbox retry, flush incremental.
- **Smoke em prod** pós-deploy: `--limit` + conferir 1 run no `spotify_sync_runs` + contagem no banco.
