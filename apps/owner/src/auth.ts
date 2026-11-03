// Owner sign-in: OIDC authorization code with PKCE to the Keycloak client `owner-app`
// (experience.md S5, EX-I44). Redirect `<origin>/owner/auth`; the token is kept in
// memory and session storage only.
import {
  AuthRequest,
  exchangeCodeAsync,
  fetchDiscoveryAsync,
  ResponseType,
  type DiscoveryDocument,
} from "expo-auth-session";

export const CLIENT_ID = "owner-app";
export const REALM_PATH = "/auth/realms/kitchenless";
export const REDIRECT_PATH = "/owner/auth";

const PENDING_KEY = "owner.auth.pending";
const SESSION_KEY = "owner.auth.session";

export type Session = { accessToken: string; expiresAt: number; idToken?: string };
type Pending = { state: string; codeVerifier: string };

let current: Session | null = null;

function store(): Storage | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage;
}

function readJson<T>(key: string): T | null {
  const raw = store()?.getItem(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function issuer(origin: string): string {
  return origin + REALM_PATH;
}

export function redirectUri(origin: string): string {
  return origin + REDIRECT_PATH;
}

function discover(origin: string): Promise<DiscoveryDocument> {
  return fetchDiscoveryAsync(issuer(origin));
}

/** Starts sign-in: builds the PKCE request, remembers it, and leaves for Keycloak. */
export async function beginSignIn(
  origin: string,
  navigate: (url: string) => void,
): Promise<void> {
  const discovery = await discover(origin);
  const request = new AuthRequest({
    clientId: CLIENT_ID,
    redirectUri: redirectUri(origin),
    scopes: ["openid"],
    responseType: ResponseType.Code,
    usePKCE: true,
  });
  const url = await request.makeAuthUrlAsync(discovery);
  if (!request.codeVerifier) throw new Error("PKCE verifier missing");
  const pending: Pending = { state: request.state, codeVerifier: request.codeVerifier };
  store()?.setItem(PENDING_KEY, JSON.stringify(pending));
  navigate(url);
}

/** Finishes sign-in on `/owner/auth`: checks state, then exchanges the code. */
export async function completeSignIn(
  origin: string,
  query: Record<string, string | undefined>,
): Promise<Session> {
  const pending = readJson<Pending>(PENDING_KEY);
  store()?.removeItem(PENDING_KEY);
  if (query.error) throw new Error(`sign-in failed: ${query.error}`);
  if (!pending || !query.state || query.state !== pending.state) {
    throw new Error("sign-in state mismatch");
  }
  if (!query.code) throw new Error("sign-in code missing");
  const discovery = await discover(origin);
  const token = await exchangeCodeAsync(
    {
      clientId: CLIENT_ID,
      redirectUri: redirectUri(origin),
      code: query.code,
      extraParams: { code_verifier: pending.codeVerifier },
    },
    discovery,
  );
  if (!token.expiresIn) throw new Error("token has no expiry");
  const session: Session = {
    accessToken: token.accessToken,
    expiresAt: (token.issuedAt + token.expiresIn) * 1000,
    idToken: token.idToken,
  };
  current = session;
  store()?.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

/** The access token, or null when signed out or expired (fail closed). */
export function accessToken(now: number = Date.now()): string | null {
  const session = current ?? readJson<Session>(SESSION_KEY);
  if (!session || typeof session.expiresAt !== "number" || session.expiresAt <= now) {
    current = null;
    store()?.removeItem(SESSION_KEY);
    return null;
  }
  current = session;
  return session.accessToken;
}

export function signOut(): void {
  current = null;
  store()?.removeItem(SESSION_KEY);
  store()?.removeItem(PENDING_KEY);
}
