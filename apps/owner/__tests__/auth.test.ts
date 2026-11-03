// Owner sign-in (EX-I44): PKCE against mocked discovery and token endpoints.
import { createHash, webcrypto } from "crypto";

import { accessToken, beginSignIn, completeSignIn, signOut } from "../src/auth";

// expo-crypto is native; jest has no implementation of its digest, so the PKCE
// challenge would be empty. It is nested under expo-auth-session, hence the path.
// babel-jest hoists this above the imports.
jest.mock("../node_modules/expo-auth-session/node_modules/expo-crypto", () => {
  const nodeCrypto = jest.requireActual("crypto");
  return {
    CryptoDigestAlgorithm: { SHA256: "SHA-256" },
    CryptoEncoding: { BASE64: "base64", HEX: "hex" },
    getRandomValues: (a: Uint8Array) => nodeCrypto.webcrypto.getRandomValues(a),
    digestStringAsync: async (_alg: string, value: string, opts: { encoding: string }) =>
      nodeCrypto.createHash("sha256").update(value).digest(opts.encoding),
  };
});

const ORIGIN = "https://kitchen.example";
const ISSUER = `${ORIGIN}/auth/realms/kitchenless`;
const DISCOVERY = {
  issuer: ISSUER,
  authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
  token_endpoint: `${ISSUER}/protocol/openid-connect/token`,
  end_session_endpoint: `${ISSUER}/protocol/openid-connect/logout`,
};

class MemoryStorage {
  private items = new Map<string, string>();
  getItem(k: string) {
    return this.items.has(k) ? (this.items.get(k) as string) : null;
  }
  setItem(k: string, v: string) {
    this.items.set(k, v);
  }
  removeItem(k: string) {
    this.items.delete(k);
  }
  clear() {
    this.items.clear();
  }
}

type Call = { url: string; body?: string };
let calls: Call[];
let tokenBody: Record<string, unknown>;

function json(body: unknown, status = 200) {
  return {
    ok: status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

beforeEach(() => {
  (globalThis as unknown as { sessionStorage: MemoryStorage }).sessionStorage =
    new MemoryStorage();
  (globalThis as unknown as { crypto: unknown }).crypto ??= webcrypto;
  calls = [];
  tokenBody = {
    access_token: "at-1",
    token_type: "bearer",
    expires_in: 300,
    id_token: "id-1",
  };
  globalThis.fetch = jest.fn(async (input: unknown, init?: { body?: string }) => {
    const url = String(input);
    calls.push({ url, body: init?.body });
    if (url === `${ISSUER}/.well-known/openid-configuration`) return json(DISCOVERY);
    if (url === DISCOVERY.token_endpoint) return json(tokenBody);
    return json({ error: "not_found" }, 404);
  }) as unknown as typeof fetch;
  signOut();
});

async function begin(): Promise<URL> {
  let target = "";
  await beginSignIn(ORIGIN, (u) => {
    target = u;
  });
  return new URL(target);
}

test("beginSignIn sends a PKCE S256 code request to the realm", async () => {
  const url = await begin();
  expect(`${url.origin}${url.pathname}`).toBe(DISCOVERY.authorization_endpoint);
  const q = url.searchParams;
  expect(q.get("client_id")).toBe("owner-app");
  expect(q.get("redirect_uri")).toBe(`${ORIGIN}/owner/auth`);
  expect(q.get("response_type")).toBe("code");
  expect(q.get("code_challenge_method")).toBe("S256");
  expect(q.get("scope")).toContain("openid");
  expect(q.get("state")).toBeTruthy();
  expect(q.get("code_challenge")).toBeTruthy();
  expect(calls[0].url).toBe(`${ISSUER}/.well-known/openid-configuration`);
});

test("completeSignIn exchanges the code with the verifier and keeps the token", async () => {
  const url = await begin();
  const state = url.searchParams.get("state") as string;
  const challenge = url.searchParams.get("code_challenge") as string;

  const session = await completeSignIn(ORIGIN, { code: "c-1", state });
  expect(session.accessToken).toBe("at-1");
  expect(accessToken()).toBe("at-1");

  const tokenCall = calls.find((c) => c.url === DISCOVERY.token_endpoint) as Call;
  const body = new URLSearchParams(tokenCall.body);
  expect(body.get("grant_type")).toBe("authorization_code");
  expect(body.get("code")).toBe("c-1");
  expect(body.get("client_id")).toBe("owner-app");
  expect(body.get("redirect_uri")).toBe(`${ORIGIN}/owner/auth`);
  const verifier = body.get("code_verifier") as string;
  const expected = createHash("sha256")
    .update(verifier)
    .digest("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  expect(expected).toBe(challenge);

  // Only session storage, never local storage; nothing pending is left behind.
  const stored = sessionStorage.getItem("owner.auth.session") as string;
  expect(JSON.parse(stored).accessToken).toBe("at-1");
  expect(sessionStorage.getItem("owner.auth.pending")).toBeNull();
});

test("a state mismatch is refused without calling the token endpoint", async () => {
  await begin();
  await expect(completeSignIn(ORIGIN, { code: "c-1", state: "other" })).rejects.toThrow(
    "state mismatch",
  );
  expect(calls.some((c) => c.url === DISCOVERY.token_endpoint)).toBe(false);
  expect(accessToken()).toBeNull();
});

test("a callback without a pending sign-in is refused", async () => {
  await expect(completeSignIn(ORIGIN, { code: "c-1", state: "s" })).rejects.toThrow(
    "state mismatch",
  );
});

test("an error from Keycloak is refused", async () => {
  const url = await begin();
  const state = url.searchParams.get("state") as string;
  await expect(
    completeSignIn(ORIGIN, { error: "access_denied", state }),
  ).rejects.toThrow("access_denied");
  expect(accessToken()).toBeNull();
});

test("an expired token is not returned and is cleared", async () => {
  const url = await begin();
  await completeSignIn(ORIGIN, { code: "c-1", state: url.searchParams.get("state") as string });
  expect(accessToken(Date.now() + 299_000)).toBe("at-1");
  expect(accessToken(Date.now() + 301_000)).toBeNull();
  expect(sessionStorage.getItem("owner.auth.session")).toBeNull();
});

test("a token without an expiry is refused", async () => {
  delete tokenBody.expires_in;
  const url = await begin();
  await expect(
    completeSignIn(ORIGIN, { code: "c-1", state: url.searchParams.get("state") as string }),
  ).rejects.toThrow("expiry");
  expect(accessToken()).toBeNull();
});

test("signOut clears the token", async () => {
  const url = await begin();
  await completeSignIn(ORIGIN, { code: "c-1", state: url.searchParams.get("state") as string });
  signOut();
  expect(accessToken()).toBeNull();
  expect(sessionStorage.getItem("owner.auth.session")).toBeNull();
});
