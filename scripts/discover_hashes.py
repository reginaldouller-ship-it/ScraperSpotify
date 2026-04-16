"""
Descobre os sha256Hash das persisted queries GraphQL do Spotify Web Player.

Fluxo:
  1. Fetch da home https://open.spotify.com/
  2. Extrai URL do bundle JS principal (web-player.<hash>.js)
  3. Fetch do bundle (~4MB)
  4. Regex nos pares new X.Y("operationName","query","sha256Hash",null)
  5. (Opcional) Extrai mapa de chunks e varre chunks específicos pra ops raras
  6. Compara com config/settings.py:GRAPHQL_HASHES, imprime diffs
  7. Com --write, atualiza settings.py (somente seção GRAPHQL_HASHES)

Uso:
  python -m scripts.discover_hashes                    # só imprime resultado
  python -m scripts.discover_hashes --write            # atualiza settings.py
  python -m scripts.discover_hashes --deep             # varre chunks (demora +)
  python -m scripts.discover_hashes --op queryArtistOverview  # filtra operação específica
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

console = Console()

SPOTIFY_HOME = "https://open.spotify.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Padrão do webpack: new X.Y("operationName","query","sha256Hash",null)
OP_HASH_RE = re.compile(
    r'new\s+\w+\.\w+\("(?P<name>\w+)",\s*"query",\s*"(?P<hash>[a-f0-9]{64})"'
)

# Padrão do webpack runtime para mapa de chunks:
# __webpack_require__.u=e=>""+(({ID:"name", ...})[e]||e)+"."+({ID:"hash", ...})[e]+".js"
CHUNK_URL_FN_MARKER = "__webpack_require__.u=e=>"


def fetch_text(url: str, client: httpx.Client) -> str:
    r = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60)
    r.raise_for_status()
    return r.text


def find_main_bundle_url(html: str) -> str:
    """Encontra a URL do web-player.<hash>.js (o bundle principal).

    O bundle principal fica em /cdn/build/web-player/web-player.<hash>.js.
    Precisamos descartar manifest-web-player e outros com nomes similares.
    """
    matches = re.findall(r'https?://[^\s"\'<>]+/build/web-player/web-player\.[a-f0-9]+\.js', html)
    if not matches:
        raise RuntimeError("Bundle principal (web-player.*.js) não encontrado na home")
    # descartar vendor~ e encore~
    main = [u for u in matches if "vendor" not in u and "encore" not in u]
    return main[0] if main else matches[0]


def extract_ops(text: str) -> dict[str, str]:
    """Retorna dict operationName -> sha256Hash extraído do JS."""
    ops: dict[str, str] = {}
    for m in OP_HASH_RE.finditer(text):
        name = m.group("name")
        h = m.group("hash")
        if name not in ops:
            ops[name] = h
    return ops


def extract_chunk_manifest(main_js: str) -> tuple[dict[str, str], dict[str, str]]:
    """Retorna (id->name, id->hash) dos chunks do webpack."""
    idx = main_js.find(CHUNK_URL_FN_MARKER)
    if idx == -1:
        return {}, {}
    chunk = main_js[idx : idx + 50000]

    m_names = re.search(r"\(\{([^}]+)\}\)\[e\]", chunk)
    m_hashes = re.search(r'\+"\."\+\(\{([^}]+)\}\)\[e\]', chunk)
    if not m_names or not m_hashes:
        return {}, {}
    names = dict(re.findall(r'(\d+):"([^"]+)"', m_names.group(1)))
    hashes = dict(re.findall(r'(\d+):"([^"]+)"', m_hashes.group(1)))
    return names, hashes


def scan_chunk(url: str, client: httpx.Client, target_keywords: Optional[list[str]] = None) -> dict[str, str]:
    """Baixa um chunk e extrai suas ops."""
    try:
        text = fetch_text(url, client)
    except httpx.HTTPError:
        return {}
    ops = extract_ops(text)
    if target_keywords:
        ops = {n: h for n, h in ops.items() if any(k.lower() in n.lower() for k in target_keywords)}
    return ops


def discover(deep: bool = False, filter_op: Optional[str] = None) -> dict[str, str]:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        console.print("[cyan]Baixando home...[/]")
        home = fetch_text(SPOTIFY_HOME, client)
        bundle_url = find_main_bundle_url(home)
        console.print(f"[green][OK][/] Bundle: {bundle_url}")

        console.print("[cyan]Baixando bundle principal (~4MB)...[/]")
        main_js = fetch_text(bundle_url, client)
        console.print(f"[green][OK][/] Bundle tamanho: {len(main_js) // 1024}KB")

        ops = extract_ops(main_js)
        console.print(f"[green][OK][/] Operations no bundle principal: {len(ops)}")

        if deep:
            names, hashes = extract_chunk_manifest(main_js)
            console.print(f"[cyan]Chunks mapeados:[/] {len(names)}")
            # Prioriza chunks com nomes sugestivos (routes-track, credits, etc.)
            interesting = [
                (cid, n) for cid, n in names.items()
                if any(k in n.lower() for k in ["track", "credit", "version", "artist", "album"])
            ]
            console.print(f"[cyan]Chunks interessantes a varrer:[/] {len(interesting)}")
            base = bundle_url.rsplit("/", 1)[0]
            for cid, name in interesting:
                h = hashes.get(cid)
                if not h:
                    continue
                url = f"{base}/{name}.{h}.js"
                new_ops = scan_chunk(url, client)
                added = {k: v for k, v in new_ops.items() if k not in ops}
                if added:
                    ops.update(added)
                    console.print(f"  [green]+{len(added)}[/] de {name}.{h[:6]}.js")

    if filter_op:
        ops = {n: h for n, h in ops.items() if filter_op.lower() in n.lower()}

    return ops


def compare_with_current(discovered: dict[str, str]) -> None:
    current = settings.GRAPHQL_HASHES
    all_names = set(current) | set(discovered)
    table = Table(title="[bold]Comparação com config/settings.py[/]", header_style="bold cyan")
    table.add_column("Operation")
    table.add_column("Atual em settings.py")
    table.add_column("Descoberto no bundle")
    table.add_column("Status")
    for name in sorted(all_names):
        cur = current.get(name, "")
        dsc = discovered.get(name, "")
        if cur and dsc and cur == dsc:
            status = "[green]OK[/]"
        elif cur and dsc and cur != dsc:
            status = "[yellow]MUDOU[/]"
        elif dsc and not cur:
            status = "[cyan]NOVO[/]"
        elif cur and not dsc:
            status = "[dim]não encontrado[/]"
        else:
            status = "?"
        table.add_row(name, cur[:16] + "…" if cur else "-", dsc[:16] + "…" if dsc else "-", status)
    console.print(table)


def write_settings(discovered: dict[str, str]) -> None:
    """Atualiza settings.py mesclando hashes descobertos com os atuais."""
    path = Path(settings.__file__)
    text = path.read_text(encoding="utf-8")
    # Merge: preserva tudo que está lá, atualiza hashes divergentes, adiciona novos
    merged = {**settings.GRAPHQL_HASHES, **discovered}
    new_block = "GRAPHQL_HASHES = {\n"
    for name in sorted(merged):
        new_block += f'    "{name}": "{merged[name]}",\n'
    new_block += "}"

    old_pat = re.compile(r"GRAPHQL_HASHES\s*=\s*\{[^}]*\}", re.DOTALL)
    if not old_pat.search(text):
        console.print("[red]Não consegui localizar GRAPHQL_HASHES em settings.py. Abortando write.[/]")
        return
    new_text = old_pat.sub(new_block, text)
    path.write_text(new_text, encoding="utf-8")
    console.print(f"[green][OK][/] settings.py atualizado com {len(merged)} hashes")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--deep", action="store_true", help="varrer também chunks lazy-loaded")
    p.add_argument("--write", action="store_true", help="atualizar settings.py")
    p.add_argument("--op", help="filtrar por substring de operação")
    p.add_argument("--json", action="store_true", help="imprimir JSON com todos os hashes")
    args = p.parse_args()

    discovered = discover(deep=args.deep, filter_op=args.op)

    if args.json:
        console.print(json.dumps(discovered, indent=2))
        return 0

    compare_with_current(discovered)

    if args.write:
        write_settings(discovered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
