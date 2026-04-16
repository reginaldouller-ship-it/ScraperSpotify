/**
 * Obtenção do accessToken anônimo do Spotify Web Player.
 *
 * Estratégia:
 *   1. Extrai token do __NEXT_DATA__ da página de embed de um álbum público.
 *      Este é o mesmo token que o iframe do Spotify usa pra chamar a Partner API.
 *   2. (Opcional, via env TRY_DIRECT_TOKEN_ENDPOINT=1) Tenta o endpoint direto
 *      `/get_access_token`. Em IPs residenciais brasileiros retorna 403; em VPS
 *      ou outros ranges pode funcionar.
 *
 * Thread-safe (em Node, single-threaded, isso não importa, mas garantimos
 * que concurrent callers compartilham o mesmo token em memória).
 */
import { TOKEN_FALLBACK_ALBUM_ID, USER_AGENTS } from './queries.js';
import type { AnonymousToken } from './types.js';

const TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000; // renova 5min antes de expirar
const TOKEN_ROTATION_REQUESTS = 100;            // também renova após N usos

const NEXT_DATA_RE = /<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/;

export class SpotifyPartnerAuth {
  private token: AnonymousToken | null = null;
  private requestCount = 0;
  private fetchPromise: Promise<AnonymousToken> | null = null;
  private directEndpointDisabled = false;

  constructor(private readonly opts: { tryDirect?: boolean } = {}) {}

  /** Retorna token válido, renovando se necessário. */
  async getToken(forceRefresh = false): Promise<string> {
    if (!forceRefresh && this.token && !this.isExpired() && this.requestCount < TOKEN_ROTATION_REQUESTS) {
      this.requestCount += 1;
      return this.token.accessToken;
    }
    if (!this.fetchPromise) {
      this.fetchPromise = this.fetchToken().finally(() => { this.fetchPromise = null; });
    }
    const token = await this.fetchPromise;
    this.requestCount = 1;
    return token.accessToken;
  }

  /** Força renovação na próxima chamada (ex: após 401). */
  invalidate(): void {
    this.token = null;
    this.requestCount = 0;
  }

  private isExpired(): boolean {
    if (!this.token) return true;
    return Date.now() + TOKEN_REFRESH_MARGIN_MS >= this.token.expiresAtMs;
  }

  private async fetchToken(): Promise<AnonymousToken> {
    if (this.opts.tryDirect && !this.directEndpointDisabled) {
      const direct = await this.tryDirectEndpoint();
      if (direct) {
        this.token = direct;
        return direct;
      }
    }
    const fromEmbed = await this.fetchFromEmbed();
    this.token = fromEmbed;
    return fromEmbed;
  }

  private async tryDirectEndpoint(): Promise<AnonymousToken | null> {
    try {
      const resp = await fetch(
        'https://open.spotify.com/get_access_token?reason=transport&productType=web-player',
        {
          headers: {
            'User-Agent': randomUA(),
            Accept: 'application/json',
            Referer: 'https://open.spotify.com/',
            Origin: 'https://open.spotify.com',
          },
        },
      );
      if (!resp.ok) {
        if (resp.status === 403) this.directEndpointDisabled = true;
        return null;
      }
      const data = await resp.json() as { accessToken?: string; accessTokenExpirationTimestampMs?: number; isAnonymous?: boolean };
      if (!data.accessToken || !data.accessTokenExpirationTimestampMs) return null;
      return {
        accessToken: data.accessToken,
        expiresAtMs: data.accessTokenExpirationTimestampMs,
        isAnonymous: data.isAnonymous ?? true,
        source: 'direct',
      };
    } catch {
      return null;
    }
  }

  private async fetchFromEmbed(): Promise<AnonymousToken> {
    const url = `https://open.spotify.com/embed/album/${TOKEN_FALLBACK_ALBUM_ID}`;
    const resp = await fetch(url, {
      headers: {
        'User-Agent': randomUA(),
        Accept: 'text/html,application/xhtml+xml',
        Referer: 'https://open.spotify.com/',
      },
    });
    if (!resp.ok) {
      throw new Error(`Embed token fetch failed: HTTP ${resp.status}`);
    }
    const html = await resp.text();
    const match = NEXT_DATA_RE.exec(html);
    if (!match) {
      throw new Error('__NEXT_DATA__ script não encontrado no HTML do embed');
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(match[1]!);
    } catch (err) {
      throw new Error(`Erro parseando __NEXT_DATA__: ${(err as Error).message}`);
    }

    const session = extractSession(parsed);
    if (!session?.accessToken || !session.accessTokenExpirationTimestampMs) {
      throw new Error('Session sem accessToken válido no __NEXT_DATA__');
    }

    return {
      accessToken: session.accessToken,
      expiresAtMs: session.accessTokenExpirationTimestampMs,
      isAnonymous: session.isAnonymous ?? true,
      source: 'embed',
    };
  }
}

interface EmbedSession {
  accessToken?: string;
  accessTokenExpirationTimestampMs?: number;
  isAnonymous?: boolean;
}

function extractSession(root: unknown): EmbedSession | null {
  // Path: root.props.pageProps.state.settings.session
  const asObj = (v: unknown): Record<string, unknown> | null =>
    typeof v === 'object' && v !== null ? (v as Record<string, unknown>) : null;

  const props = asObj(asObj(root)?.props);
  const pageProps = asObj(props?.pageProps);
  const state = asObj(pageProps?.state);
  const settings = asObj(state?.settings);
  const session = asObj(settings?.session);
  if (!session) return null;
  return session as EmbedSession;
}

function randomUA(): string {
  return USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)]!;
}
