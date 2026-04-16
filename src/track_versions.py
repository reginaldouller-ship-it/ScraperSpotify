"""
Detecção de "outras versões" da mesma música.

O Spotify não expõe `otherVersions` como query GraphQL — a UI do web player
que mostra "Também disponível em..." é computada client-side combinando:
  1. A discografia completa do artista (queryArtistDiscographyAll)
  2. Fuzzy matching de nome de track + duração aproximada

Esta utility replica essa lógica.

Casos cobertos:
  - "Ainda Tem Promessa" (single) vs "Ainda Tem Promessa (Ao Vivo)" (álbum)
  - "Song Name" vs "Song Name - Remastered 2011"
  - "Track" (álbum A) vs "Track" (álbum B, compilation)
  - "Track" vs "Track (Acoustic Version)"

Casos NÃO cobertos:
  - Versões de artistas diferentes (covers, features novos)
  - Traduções ("Despacito" vs "Despacito (Spanish Remix)")

Uso típico no Miner:
  Para evitar contar a mesma música múltiplas vezes num ranking ou feed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# Sufixos que geralmente indicam uma versão derivada (não a "original")
VERSION_SUFFIX_PATTERNS = [
    r"\(ao vivo\)",
    r"\(live(?: at [^)]+)?\)",
    r"\(acoustic(?:\s+version)?\)",
    r"\(ac[uú]stic[oa](?:\s+vers[aã]o)?\)",
    r"\(remaster(?:ed)?(?:\s+\d{4})?\)",
    r"\(remix(?:ed)?\)",
    r"\(radio edit\)",
    r"\(single version\)",
    r"\(album version\)",
    r"\(extended(?:\s+version)?\)",
    r"\(clean\)",
    r"\(explicit\)",
    r"\(bonus track\)",
    r"\(deluxe(?:\s+version)?\)",
    r"\(sped up\)",
    r"\(slowed(?:\s+down)?\)",
    r"- ao vivo",
    r"- live(?:\s+at\s+[^-]+)?",
    r"- remaster(?:ed)?(?:\s+\d{4})?",
    r"- \d{4}\s+remaster(?:ed)?",  # "- 2011 Remaster"
    r"- remix(?:ed)?",
    r"- radio edit",
    r"- acoustic",
    r"- ac[uú]stic[oa]",
    r"- instrumental",
    r"- demo",
    r"- single version",
]
_SUFFIX_RE = re.compile("|".join(VERSION_SUFFIX_PATTERNS), re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")


def normalize_title(title: str) -> str:
    """
    Normaliza um título para comparação de 'mesma música'.
    - Lowercase
    - Remove sufixos de versão (live, acoustic, remaster, etc.)
    - Remove tudo entre parênteses/colchetes
    - Remove "feat. X", "with X"
    - Normaliza espaços
    """
    if not title:
        return ""
    t = title.lower().strip()

    # Remove "feat.", "ft.", "with"
    t = re.sub(r"\s+(feat\.|ft\.|with)\s+.+$", "", t, flags=re.IGNORECASE)

    # Remove sufixos reconhecidos explicitamente
    t = _SUFFIX_RE.sub("", t)

    # Remove qualquer (...) ou [...] restante
    t = _PAREN_RE.sub(" ", t)

    # Normaliza espaços e pontuação
    t = re.sub(r"[^\w\sáéíóúâêîôûãõç]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


@dataclass
class TrackVariant:
    track_id: str
    track_name: str
    album_id: str
    album_name: str
    album_type: str  # "album" | "single" | "compilation" | "appears_on"
    duration_ms: Optional[int]
    playcount: Optional[int]

    @property
    def normalized_title(self) -> str:
        return normalize_title(self.track_name)


def group_same_song(variants: list[TrackVariant], duration_tolerance_ms: int = 30_000) -> list[list[TrackVariant]]:
    """
    Agrupa variantes que são "a mesma música".
    Critério: normalized_title igual E duração dentro de tolerance_ms.

    Retorna lista de grupos (cada grupo = lista de variantes).
    Grupos de tamanho 1 = música sem duplicatas.
    """
    groups: dict[tuple[str, int], list[TrackVariant]] = {}

    for v in variants:
        norm = v.normalized_title
        if not norm:
            continue
        # Duração quantizada em bucket para agrupar aproximadamente
        dur_bucket = (v.duration_ms or 0) // max(duration_tolerance_ms, 1)
        key = (norm, dur_bucket)

        # Tolerância: considera match se caiu em bucket adjacente também
        matched_key = None
        for existing_key in list(groups.keys()):
            ex_norm, ex_bucket = existing_key
            if ex_norm == norm and abs(ex_bucket - dur_bucket) <= 1:
                matched_key = existing_key
                break

        if matched_key:
            groups[matched_key].append(v)
        else:
            groups[key] = [v]

    return list(groups.values())


def pick_canonical(group: list[TrackVariant]) -> TrackVariant:
    """
    Escolhe a variante 'canônica' de um grupo (a que você manteria num ranking).

    Regra de prioridade:
      1. Maior playcount (se disponível)
      2. Tipo = "album" > "single" > "compilation" > "appears_on"
      3. Título mais curto (geralmente a versão original tem título curto)
    """
    if len(group) == 1:
        return group[0]

    type_order = {"album": 0, "single": 1, "compilation": 2, "appears_on": 3}

    def sort_key(v: TrackVariant):
        return (
            -(v.playcount or 0),
            type_order.get(v.album_type, 99),
            len(v.track_name),
        )

    return sorted(group, key=sort_key)[0]


def find_duplicates(variants: list[TrackVariant]) -> list[dict]:
    """
    Facade: recebe lista de variantes, retorna lista de duplicatas detectadas.

    [
        {
            "canonical": TrackVariant,   # a que manter
            "duplicates": [TrackVariant, ...],  # as que filtrar
            "normalized_title": str,
        },
        ...
    ]
    """
    groups = group_same_song(variants)
    result = []
    for g in groups:
        if len(g) < 2:
            continue
        canonical = pick_canonical(g)
        dups = [v for v in g if v.track_id != canonical.track_id]
        result.append({
            "canonical": canonical,
            "duplicates": dups,
            "normalized_title": canonical.normalized_title,
        })
    return result
