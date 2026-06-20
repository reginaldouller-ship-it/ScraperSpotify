# Incidentes

Postmortems de produção. 1 arquivo por incidente, nomeado `YYYY-MM-DD-slug.md` (kebab-case, data no início).

Cada doc tem frontmatter YAML (`type`, `scope`, `importance`, `status`, `tags`, etc.) e segue a estrutura: resumo → linha do tempo → evidências → causa-raiz → remediação → lições.

| Data | Incidente | Severidade | Status |
|---|---|---|---|
| 2026-06-19 | [CPU da VPS saturada e sync diário parado](2026-06-19-cpu-saturada-sync-parado.md) | crítica | diagnóstico concluído / remediação pendente |
