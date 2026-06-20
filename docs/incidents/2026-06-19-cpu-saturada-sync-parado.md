---
type: audit
scope: infra
importance: critica
status: partial          # diagnóstico concluído; remediação pendente
tags: [cpu, vps, hostinger, coolify, supabase, orphan-process, capacity, sync, oom]
related_tasks: [MMPDA-125]   # "expor o log estruturado" / observabilidade do sync
related_deploys: [2026-06-15-deploys-falhos]   # 4 de 5 deploys falharam em 15/jun
related_incidents: []
files_changed: []        # investigação 100% read-only — nada foi alterado
commits: []
duration_minutes: 90
requested_by: Bruno
linked_learning: null    # criar versão didática em escola-programacao-* se desejado
---

# Incidente — CPU da VPS saturada e sync diário parado (12–19/jun/2026)

> **Status:** diagnóstico concluído (read-only). Remediação **pendente** de decisão do Bruno.
> **Severidade:** crítica — gravação de snapshots parada há ~6 dias no momento do diagnóstico.
> **Investigado em:** 19/jun/2026, via MCPs Hostinger (VPS), Coolify (`coolify-scraper`) e Supabase (Miner).

---

## 1. Resumo executivo (TL;DR)

A VPS do scraper é **1 vCPU + 4 GB** e roda, **no mesmo núcleo, o Coolify (painel de controle) E o scraper**. Em ~1 mês o catálogo a ser coletado **cresceu ~12×** (de ~108 mil para ~1,29 milhão de tracks/dia). A run diária, que levava **21 min** em 19/mai, passou a levar **2 h+**, saturando CPU (100%) e RAM (~4 GB/4 GB, beirando OOM). A saturação:

1. deixou **processos Python órfãos** rodando soltos (CPU 100%→64% por ~21 h sem nenhum job ativo no Coolify);
2. **desestabilizou o próprio Coolify** (erros de DNS do `coolify-redis`, `SSH exit 255`, job interrompido no startup);
3. fez a **gravação de snapshots parar após 12/jun** (último dado: `track_snapshots` em 12/jun, `artist_snapshots` em 13/jun).

**Correção importante de uma hipótese inicial errada:** o "failed todo dia" que o Coolify mostrava **não significava "não gravou"**. Até 12/jun o dado **entrava normalmente**; o "failed" era o **exit-code falso** (a run terminava e gravava ~100%, mas saía com código 1 porque qualquer 1 item falho fazia exit 1). Isso é justamente o que o **P0** (preparado em outro chat) conserta.

---

## 2. Linha do tempo

| Quando (UTC) | Evento | Evidência |
|---|---|---|
| 20/abr | Primeira run: 0,5 min, 476 snapshots | histórico de execuções Coolify |
| 09–19/mai | Runs saudáveis 14–21 min; catálogo 100k→165k | execuções + tabela `spotify_track_snapshots` |
| **19/mai** | **Última run "success" no Coolify** (21 min, 165.649 snaps) | execução `m4hypd76...` |
| 20/mai → 12/jun | Catálogo dispara (165k→1,29M/dia). Coolify marca "failed" todo dia, **mas o dado continua entrando** | contagem por data no Supabase |
| 22/mai → 15/jun | Runs morrem em **~60 min** = `timeout: 3600` da scheduled task | 25× "ScheduledTaskJob has timed out" |
| 15/jun | Timeout da task **3600s → 14400s** (4 h). 4 de 5 deploys falham | scheduled task `updated_at`; `diagnose_app` |
| 16/jun | Run vai a **334 min** e ainda falha (timeout estendido funcionou, mas não basta) | execução 16/jun |
| 16–18/jun | Runs travam 3–5,5 h com **erros de infra do Coolify** (`coolify-redis` DNS, `SSH exit 255`) | execuções 17 e 18/jun |
| **~13/jun** | **Gravação de snapshots para** | `MAX(date)`: track=12/jun, artist=13/jun |
| 17–19/jun | "Piso" de CPU sobe: **13% → 27% → 64%** (órfãos empilhando) | métricas Hostinger |
| 18/jun 15h → 19/jun 12h | **~21 h de CPU 100%→64% sem nenhum job ativo no Coolify** → processo órfão | métricas Hostinger + execuções |
| 19/jun 12:00 | Run de hoje inicia e fica "running" (não fechou) | execução `pq6ndzs...` |

---

## 3. Evidências

### 3.1 Infraestrutura (Hostinger)

| Item | Valor |
|---|---|
| VPS scraper | id **1597438**, `scraper.spotify`, **KVM 1 = 1 vCPU / 4 GB / 50 GB**, IP 187.127.73.16 |
| VPS miner (para comparação) | id 1579872, `miner.music`, **KVM 4 = 4 vCPU / 16 GB**, IP 147.79.87.195 |
| CPU baseline | ~13% (Coolify + sistema, sem run) |
| CPU durante run | picos a **100%**, sustentado 5–11 h nos dias 16–18/jun |
| "Piso" de CPU | subiu **13% → 27% (17/jun) → 64% (madrugada 19/jun)** |
| RAM baseline / pico | ~1,2 GB / **~4,0 GB de 4,0 GB** (perto de OOM) durante runs |
| Disco | ~5,5–6 GB de 50 GB (ok) |

> **Prova dos órfãos:** entre 18/jun ~15 h e 19/jun ~12 h (~21 h), a CPU ficou 100% e depois 64% **enquanto o Coolify não tinha nenhuma scheduled task ativa**. Só pode ser processo Python sobrevivente de runs anteriores.

### 3.2 Histórico de execuções (Coolify scheduled task `sync-diario`)

- 67 execuções: 48 "failed", 17 "success", **2 "running" nunca fechadas** (14/jun e 19/jun) — 14/jun chegou a disparar **duas vezes**.
- Última "success": **19/mai**. Depois, sequência de timeouts (60 min até 15/jun) e travadas longas (3–5 h) com erros de infra.
- O kill em ~60 min era o `timeout: 3600` da própria task (corrigido p/ 14400 em 15/jun).

### 3.3 Realidade do dado (Supabase Miner — `suzcbyzidnzzahwrkveh`)

Snapshots **gravados por dia** (prova de que o dado entrava apesar do "failed"):

| Data | `track_snapshots` |
|---|---|
| 15/mai | 108.111 |
| 19/mai | 165.649 |
| 22/mai | 494.689 |
| 27/mai | 1.084.791 |
| 01/jun | 1.233.584 |
| **12/jun** | **1.287.267** |

Última data com dado por tabela: `track_snapshots` **12/jun**, `top_cities` **12/jun**, `discovered_on` **12/jun**, `artist_snapshots` **13/jun**. **13–19/jun: sem dado.**

Tamanho atual das tabelas (estimativa `pg_class.reltuples`):

| Tabela | Linhas (est.) |
|---|---|
| spotify_track_snapshots | ~24,98 M |
| spotify_artist_discovered_on_snapshots | ~12,51 M |
| spotify_artist_snapshots | ~1,47 M |
| spotify_tracks | ~1,29 M |
| spotify_artist_top_cities_snapshots | ~283 k |
| spotify_artists | ~61 k |

> **O driver-raiz:** `spotify_tracks` saltou de ~108 k para ~1,29 M num mês (~12×). Como o sync gera 1 snapshot por track/dia, a carga diária multiplicou junto.

---

## 4. Causa-raiz

```
Catálogo cresce ~12× (108k → 1,29M tracks/dia)
        │
        ▼
Run diária: 21 min (19/mai) → 2 h+ de CPU 100% e RAM ~4 GB
        │
        ▼
VPS de 1 núcleo compartilhada com o Coolify  ◄── gargalo estrutural
        │
        ├──► processos Python órfãos sobrevivem aos kills → empilham CPU/RAM
        ├──► RAM beira OOM → kills / swap → mais CPU
        └──► Coolify (mesmo núcleo) começa a falhar (redis DNS, ssh 255, startup)
                    │
                    ▼
            Gravação de snapshots PARA (~13/jun)
```

Fatores que **amplificam** (mas não são a raiz):

- **Flush "tudo no fim":** o script acumula todos os snapshots em listas na RAM e só grava no final (`flush_to_supabase`). Isso (a) infla a RAM a ~4 GB e (b) faz "tudo ou nada" — se a run morre antes do fim, perde o lote inteiro.
- **20 workers async num único núcleo:** não há paralelismo real (asyncio = 1 thread); muitos workers só concentram o parse de JSON em rajadas que cravam 100% e sufocam o Coolify.
- **Parse com `json` padrão:** parsear ~1,29 M de tracks é o maior custo de CPU; um parser mais rápido (`orjson`) cortaria isso sem perder rendimento.

---

## 5. O que **NÃO** é a causa (mitos derrubados na investigação)

| Hipótese | Veredito |
|---|---|
| "Não grava dado desde 19/mai" | ❌ **Falso.** Dado entrou diariamente até 12/jun. "failed" do Coolify ≠ "não gravou". |
| "É o `miner-integration/services/`" | ❌ Não. É um **bundle pra colar no monorepo do Miner** (roda no collector do Miner, não na VPS do scraper). |
| "Monarx (scanner) está comendo CPU" | ❌ Improvável. Scan só começou 19/jun 15:01; o piso de 64% é anterior. |
| "Muitos workers" é a causa principal | ⚠️ Parcial. Contribui pros picos, mas a raiz é a **carga 12× num núcleo compartilhado**. |

---

## 6. Opções de remediação

> Nenhuma aplicada. Prioridade sugerida: **estancar (0) → otimizar (1) → distribuir (2) → estrutural (3)**.

### Camada 0 — Estancar (urgente, exige Bruno + prod)
- **Matar processos órfãos** `python -m scripts.sync_from_supabase` vivos no container (libera CPU/RAM na hora). *Precisa de SSH/terminal no host — confirmar com Bruno.*
- **Trava de instância única** (`flock -n /tmp/sync.lock ...`) no comando do cron → impede empilhamento de runs.

### Camada 1 — Cortar CPU/RAM sem perder rendimento ("de graça")
- **Flush incremental:** gravar no Supabase em lotes **durante** a run → derruba RAM de ~4 GB pra MBs e o dado começa a entrar mesmo se a run for cortada.
- **`orjson`** no lugar do `json` padrão → ~3–5× menos CPU no parse, mesmo rendimento.

### Camada 2 — Distribuir a carga
- **Quebrar o job diário** em sub-tarefas (ex.: álbuns / artistas em horários distintos, ou lotes) que caibam no tempo e espalhem a CPU pelo dia.
- **Knob `SYNC_WORKERS`** (env dedicado, default 20) + ajuste fino (medir ~6–8) pra CPU ~80% sem cravar 100%.
- **Limitar CPU do container** no Coolify (ex.: `cpus: 0.7`) → scraper nunca mais derruba o painel.

### Camada 3 — Estrutural (sem custo extra) — **recomendado p/ o problema de CPU**
- **Rodar o scraper na VPS do miner** (4 vCPU / 16 GB, mesma conta, e o dado já vai pro Supabase do miner). Tira a disputa com o Coolify do scraper. A carga de 12× pede mais que 1 núcleo compartilhado.

### Observabilidade (transversal — MMPDA-125)
- Tabela **`spotify_sync_runs`** no Supabase do Miner (resumo de cada run, consultável por SQL/dashboard). *Tabela nova no banco do Miner → drafta migration, alinha com o Miner, Bruno aplica (nunca agente).*
- **SSH no host** (187.127.73.16) pra ler os `.json` de `data/sync_runs/` e checar processos. Hoje a chave foi recusada (`Permission denied (publickey)`).

### Trabalho já preparado em outro chat (P0 — em diff, não commitado)
- **Exit-code por taxa de falha** (`src/sync_status.py`, lógica pura + 8 testes): >1% = `degraded`/exit 1; senão exit 0. Mata o "failed falso".
- **Log estruturado robusto:** `data/sync_runs/*.json` escrito no início (stub `in_progress`) e reescrito no fim; corrige `started_at`.
- **Timeout 3600→14400s** já aplicado em prod (15/jun, confirmado).

---

## 7. Como verificar / queries úteis

```sql
-- Tendência diária de gravação (prova se está entrando dado)
SELECT date, COUNT(*) AS rows
FROM spotify_track_snapshots
WHERE date >= CURRENT_DATE - INTERVAL '10 days'
GROUP BY date ORDER BY date DESC;

-- Última data por tabela
SELECT 'track' t, MAX(date) FROM spotify_track_snapshots
UNION ALL SELECT 'artist', MAX(date) FROM spotify_artist_snapshots
UNION ALL SELECT 'top_cities', MAX(date) FROM spotify_artist_top_cities_snapshots
UNION ALL SELECT 'discovered_on', MAX(date) FROM spotify_artist_discovered_on_snapshots;
```

CPU/RAM da VPS: MCP Hostinger `VPS_getMetricsV1` (id **1597438**).
Execuções do cron: MCP Coolify `coolify-scraper` → `scheduled_tasks list_executions` (task `wynmgo9ssfzwp5h5mmro1511`).

### IDs de referência

| Recurso | ID |
|---|---|
| VPS scraper (Hostinger) | 1597438 |
| VPS miner (Hostinger) | 1579872 |
| Coolify server | gqjk0aowfr8xyh4v031218gn (localhost) |
| Coolify app | bd2yfhivgp2tiv6vdflem0ab |
| Coolify scheduled task | wynmgo9ssfzwp5h5mmro1511 (`sync-diario`, `0 12 * * *`, timeout 14400) |
| Supabase Miner | suzcbyzidnzzahwrkveh (MCP server `379bba10-…`) |

---

## 8. Lições aprendidas

1. **"failed" do orquestrador ≠ "não gravou".** Sempre cruzar o status do Coolify com o **dado real no banco** antes de concluir. (Erro cometido e corrigido nesta própria investigação.)
2. **Não colocar carga pesada no mesmo núcleo do plano de controle.** Quando o scraper satura 1 vCPU, o Coolify (no mesmo núcleo) cai junto — e o diagnóstico fica mais difícil.
3. **Flush no fim = "tudo ou nada".** Em ambiente que pode ser morto por timeout/OOM, gravar incremental é mais robusto e mais leve de RAM.
4. **Processo dentro do container pode sobreviver ao kill do orquestrador** (órfão). Trava de instância única + verificação de processo viva são essenciais.
5. **Crescimento de catálogo é uma mudança de capacidade silenciosa.** 12× em um mês exige replanejar onde/como roda — monitorar a tendência de `COUNT(*)/dia` evita a surpresa.
