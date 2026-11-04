// The negotiation sheet (showcase.md §6): a lime "Live" badge on live rows; tapping a row
// opens the sheet with the turns, refreshed every 5 s only while it is open.
import { StyleSheet } from "react-native";
import { act } from "react-test-renderer";

import Orders, { isLive, REFRESH_MS, turnTitle } from "../app/(tabs)/orders";
import { api, type OrderRow, type Turns } from "../src/api";
import { theme } from "../src/ui";
import { cleanup, flush, has, press, render, text } from "../testing/screens";

jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return {
    useFocusEffect: (effect: () => undefined | (() => void)) => React.useEffect(effect, [effect]),
  };
});

jest.mock("../src/api", () => ({
  ...jest.requireActual("../src/api"),
  api: { orders: jest.fn(), turns: jest.fn() },
}));

const mocked = api as jest.Mocked<typeof api>;

const LIVE: OrderRow = {
  ref: "K7Q2M9XD",
  kind: "quote",
  state: "countered",
  amount_minor: 4800,
  currency: "USD",
  updated_at: "2026-11-04T10:00:00Z",
  rationale: null,
};
const PLACED: OrderRow = { ...LIVE, ref: "AB12CD34", kind: "order", state: "placed" };

const TURNS: Turns = {
  ref: "K7Q2M9XD",
  state: "negotiating",
  live: true,
  turns: [
    { at: "2026-11-04T10:00:00Z", by: "them", kind: "request", text: "2 items · no peanuts" },
    {
      at: "2026-11-04T10:00:01Z",
      by: "you",
      kind: "offer",
      amount_minor: 4800,
      rationale: "Fresh today.",
    },
    {
      at: "2026-11-04T10:00:02Z",
      by: "them",
      kind: "counter",
      amount_minor: 3000,
      floor_minor: 3840,
    },
  ],
};

const sheetOpen = (tree: Awaited<ReturnType<typeof render>>) =>
  tree.root.findAll((n) => n.props.testID === "turns-sheet")[0].props.visible;

beforeEach(() => {
  jest.useFakeTimers();
  jest.resetAllMocks();
  mocked.orders.mockResolvedValue([LIVE, PLACED]);
  mocked.turns.mockResolvedValue(TURNS);
});

afterEach(() => {
  cleanup();
  jest.useRealTimers();
});

async function tick(ms: number) {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
  await flush();
}

test("live rows show a lime Live badge", async () => {
  const tree = await render(<Orders />);
  await flush();
  expect(has(tree, "order-K7Q2M9XD-badge")).toBe(true);
  expect(has(tree, "order-AB12CD34-badge")).toBe(false);
  const [badge] = tree.root.findAll(
    (n) => typeof n.type === "string" && n.props.testID === "order-K7Q2M9XD-badge",
  );
  expect(StyleSheet.flatten(badge.props.style).backgroundColor).toBe(theme.colors.accent);
  expect(text(tree)).toContain("Live");
  expect(isLive({ ...PLACED, state: "negotiating" })).toBe(true);
  expect(isLive(PLACED)).toBe(false);
});

test("tapping a row opens the sheet with the turns, refreshed every 5 s while open", async () => {
  let resolve: (t: Turns) => void = () => {};
  mocked.turns.mockReturnValueOnce(new Promise((r) => (resolve = r)));
  const tree = await render(<Orders />);
  await flush();
  expect(sheetOpen(tree)).toBe(false);
  expect(mocked.turns).not.toHaveBeenCalled();

  await press(tree, "order-K7Q2M9XD");
  expect(sheetOpen(tree)).toBe(true);
  expect(mocked.turns).toHaveBeenCalledWith("K7Q2M9XD");
  expect(has(tree, "turns-loading")).toBe(true);
  await act(async () => resolve(TURNS));
  expect(has(tree, "turns-loading")).toBe(false);
  const shown = text(tree);
  expect(shown).toContain("Request: 2 items · no peanuts");
  expect(shown).toContain("You offered $48.00");
  expect(shown).toContain("Fresh today.");
  expect(shown).toContain("They countered at $30.00 (your floor was $38.40)");

  expect(mocked.turns).toHaveBeenCalledTimes(1);
  mocked.turns.mockResolvedValue({
    ...TURNS,
    turns: [...TURNS.turns, { at: "2026-11-04T10:00:03Z", by: "you", kind: "offer", amount_minor: 3840 }],
  });
  await tick(REFRESH_MS);
  expect(mocked.turns).toHaveBeenCalledTimes(2);
  expect(text(tree)).toContain("You offered $38.40");

  // Closing the sheet stops the refresh.
  await act(async () => {
    tree.root.findByProps({ accessibilityLabel: "Close" }).props.onPress();
  });
  expect(sheetOpen(tree)).toBe(false);
  await tick(REFRESH_MS * 3);
  expect(mocked.turns).toHaveBeenCalledTimes(2);
});

test("empty and error states", async () => {
  mocked.turns.mockResolvedValue({ ...TURNS, turns: [] });
  const tree = await render(<Orders />);
  await flush();
  await press(tree, "order-AB12CD34");
  await flush();
  expect(has(tree, "turns-empty")).toBe(true);
  await act(async () => {
    tree.root.findByProps({ accessibilityLabel: "Close" }).props.onPress();
  });

  mocked.turns.mockRejectedValue(new Error("offline"));
  await press(tree, "order-K7Q2M9XD");
  await flush();
  expect(has(tree, "turns-error")).toBe(true);
  mocked.turns.mockResolvedValue(TURNS);
  await press(tree, "turns-error");
  expect(has(tree, "turns-error")).toBe(false);
  expect(has(tree, "turn-2")).toBe(true);
});

test("turn titles", () => {
  const at = "2026-11-04T10:00:00Z";
  expect(turnTitle({ at, by: "them", kind: "accept", amount_minor: 4080 }, "USD")).toBe(
    "They accepted at $40.80",
  );
  expect(turnTitle({ at, by: "you", kind: "reject", text: "price" }, "USD")).toBe(
    "You declined: price",
  );
  expect(turnTitle({ at, by: "them", kind: "cancel" }, "USD")).toBe("They cancelled");
  expect(turnTitle({ at, by: "system", kind: "courier", text: "Courier booked" }, "USD")).toBe(
    "Courier booked",
  );
  expect(turnTitle({ at, by: "system", kind: "status", text: "In delivery" }, "USD")).toBe(
    "In delivery",
  );
});
