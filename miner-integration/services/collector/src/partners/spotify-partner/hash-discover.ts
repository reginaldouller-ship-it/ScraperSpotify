/**
 * Descoberta automática dos sha256Hash das persisted queries do Spotify Web Player.
 *
 * Extrai do bundle JS público (não requer autenticação). Pode rodar como job
 * periódico (semanal) ou manualmente quando `HashOutdatedError` aparecer.
 *
 * Uso:
 *   await discoverHashes();         // retorna dict { operationName: hash }
 *   await syncHashesToSupabase();   // atualiza tabela spotify_partner_hashes
 */

const SPOTIFY_HOME = 'https://open.spotify.com/';
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36';

// Match de pares operationName + hash no bundle webpack:
//   new X.Y("operationName","query","sha256Hash",null)
const OP_HASH_RE = /new\s+\w+\.\w+\("(\w+)",\s*"query",\s*"([a-f0-9]{64})"/g;

const BUNDLE_URL_RE = /https?:\/\/[^\s"'<>]+\/build\/web-player\/web-player\.[a-f0-9]+\.js/;

export async function discoverHashes(): Promise<Record<string, string>> {
  const homeHtml = await fetchText(SPOTIFY_HOME);
  const bundleUrl = BUNDLE_URL_RE.exec(homeHtml)?.[0];
  if (!bundleUrl) {
    throw new Error('Bundle web-player.*.js não encontrado na home do Spotify');
  }

  const bundleJs = await fetchText(bundleUrl);
  const ops: Record<string, string> = {};
  for (const match of bundleJs.matchAll(OP_HASH_RE)) {
    const [, name, hash] = match;
    if (name && hash && !(name in ops)) {
      ops[name] = hash;
    }
  }

  if (Object.keys(ops).length === 0) {
    throw new Error('Nenhuma operation+hash encontrada no bundle JS. Formato mudou?');
  }
  return ops;
}

async function fetchText(url: string): Promise<string> {
  const resp = await fetch(url, {
    headers: { 'User-Agent': USER_AGENT, Accept: 'text/html,*/*' },
    redirect: 'follow',
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} ao buscar ${url}`);
  return resp.text();
}

// ==========================================================================
// Persistência opcional em Supabase
// ==========================================================================

export interface HashStorage {
  saveMany(hashes: Record<string, string>): Promise<void>;
}

export async function syncHashesToStorage(storage: HashStorage): Promise<number> {
  const hashes = await discoverHashes();
  await storage.saveMany(hashes);
  return Object.keys(hashes).length;
}
