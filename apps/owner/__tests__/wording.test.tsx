// Plain wording in the owner app (#434): no route folder names, no raw refs as titles, no raw
// states or browser timestamps, and Account in plain words.
import { readFileSync } from "fs";
import { join } from "path";

import { modeName } from "../app/(tabs)/account";
import { DISCOUNT_HELP } from "../app/(tabs)/index";
import { rowSubtitle, rowTitle, stateLabel } from "../app/(tabs)/orders";
import type { OrderRow } from "../src/api";
import { when } from "../src/when";

const ROW: OrderRow = {
  ref: "K7Q2M9XD",
  kind: "quote",
  state: "countered",
  amount_minor: 4080,
  currency: "USD",
  updated_at: "2026-11-05T14:03:00",
  rationale: "Matched your discount limit.",
};
const NOW = new Date("2026-11-05T18:00:00");

test("the root stack hides its header, so '(tabs)' is never shown", () => {
  const layout = readFileSync(join(__dirname, "..", "app", "_layout.tsx"), "utf8");
  expect(layout).toContain("headerShown: false");
});

test("an order row says what it is; the ref is only a labelled second line", () => {
  expect(rowTitle(ROW)).toBe("Price request · Counter-offer");
  expect(rowTitle({ ...ROW, kind: "order", state: "placed" })).toBe("Order · Placed");
  expect(rowTitle(ROW)).not.toContain(ROW.ref);
  expect(rowSubtitle(ROW, NOW)).toBe("Matched your discount limit.\nRef K7Q2M9XD · 2:03 pm");
  expect(rowSubtitle({ ...ROW, rationale: null }, NOW)).toBe("Ref K7Q2M9XD · 2:03 pm");
});

test("unknown states never show a raw name", () => {
  expect(stateLabel("some_new_state")).toBe("In progress");
  expect(stateLabel("rejected")).toBe("Not chosen");
});

test("times read like a person would say them", () => {
  expect(when("2026-11-05T09:05:00", NOW)).toBe("9:05 am");
  expect(when("2026-11-05T12:30:00", NOW)).toBe("12:30 pm");
  expect(when("2026-11-04T21:00:00", NOW)).toBe("Yesterday, 9:00 pm");
  expect(when("2026-11-01T08:15:00", NOW)).toBe("Sun 1 Nov, 8:15 am");
  expect(when("not a date", NOW)).toBe("");
});

test("account and discount wording is plain", () => {
  expect(modeName("real")).toBe("Live: real orders");
  expect(modeName("mock")).toBe("Practice: simulated orders");
  expect(DISCOUNT_HELP).toMatch(/your agent can go down by up to this much/);
});
