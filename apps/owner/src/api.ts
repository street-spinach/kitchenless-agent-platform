// The owner API client (experience.md §11): `/app/*` on the same origin, with the signed-in
// owner's bearer token and organisation.
import { accessToken, signOut } from "./auth";

export type Rules = { org_id: string; max_discount_pct: number | null };
export type Summary = { quotes_today: number; won_today: number; orders_today: number };
export type OrderRow = {
  ref: string;
  kind: "order" | "quote";
  state: string;
  amount_minor: number | null;
  currency: string;
  updated_at: string;
  rationale: string | null;
};
// One turn of the negotiation sheet (showcase.md §6).
export type Turn = {
  at: string;
  by: "you" | "them" | "system";
  kind: "request" | "offer" | "counter" | "accept" | "reject" | "cancel" | "courier" | "status";
  text?: string;
  amount_minor?: number;
  rationale?: string | null;
  floor_minor?: number;
};
export type Turns = { ref: string; state: string; live: boolean; turns: Turn[] };
export type Notice ={ kind: string; text: string; at: string };
export type InsightCell =
  | { metric: string; label: string; value: string; hidden?: undefined }
  | { metric: string; hidden: true };
export type Insights = { week: string; sample: boolean; cells: InsightCell[] };
export type Connection = { vendor: string; mode: "real" | "mock"; set_at: string | null };

export class SignedOut extends Error {}

/** The token's claims, unverified: the api verifies every request. */
function claims(token: string): Record<string, unknown> {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(part.padEnd(part.length + ((4 - (part.length % 4)) % 4), "=")));
  } catch {
    return {};
  }
}

/** The owner's organisation: the token's `org_id`, or the seeded owner's username
 * `owner.<org_id>` (identity-authz §10, seeded owners only). Null when neither is there. */
export function orgOf(token: string): string | null {
  const c = claims(token);
  if (typeof c.org_id === "string" && c.org_id) return c.org_id;
  const name = c.preferred_username;
  if (typeof name === "string" && name.startsWith("owner.") && name.length > 6) {
    return name.slice(6);
  }
  return null;
}

export class NotFound extends Error {}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = accessToken();
  const org = token ? orgOf(token) : null;
  if (!token || !org) throw new SignedOut("signed out");
  const sep = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${path}${sep}org_id=${encodeURIComponent(org)}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });
  if (resp.status === 401) {
    signOut();
    throw new SignedOut("token refused");
  }
  if (resp.status === 404) throw new NotFound(path);
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return (await resp.json()) as T;
}

export const api = {
  rules: () => call<Rules>("/app/rules"),
  setDiscount: (pct: number) =>
    call<Rules>("/app/rules", {
      method: "POST",
      body: JSON.stringify({ max_discount_pct: pct }),
    }),
  summary: () => call<Summary>("/app/orders?summary=1"),
  orders: () => call<{ orders: OrderRow[] }>("/app/orders").then((r) => r.orders),
  turns: (ref: string) => call<Turns>(`/app/orders/${encodeURIComponent(ref)}/turns`),
  notices: (limit: number) =>
    call<{ notices: Notice[] }>(`/app/notices?limit=${limit}`).then((r) => r.notices),
  insights: () => call<Insights>("/app/insights"),
  // Null when the organisation has no store connected (404 `no_agents`).
  connection: () =>
    call<Connection>("/app/connection").catch((e) => {
      if (e instanceof NotFound) return null;
      throw e;
    }),
};

export function money(minor: number | null, currency: string): string {
  if (minor === null) return "";
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(minor / 100);
  } catch {
    return `${(minor / 100).toFixed(2)} ${currency}`;
  }
}
