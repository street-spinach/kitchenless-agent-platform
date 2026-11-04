// Orders (EX-I49): live orders and negotiations, refreshed every 5 s only while focused.
import { act } from "react-test-renderer";

import Orders, { REFRESH_MS, stateLabel } from "../app/(tabs)/orders";
import { api, money, orgOf, type OrderRow } from "../src/api";
import { cleanup, flush, has, press, render, text } from "../testing/screens";

// A focus switch standing in for the navigator: `useFocusEffect` runs the effect while
// focused and its cleanup on blur.
jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  const listeners: Set<(mockFocused: boolean) => void> = new Set();
  let focused = true;
  return {
    __setFocused: (mockFocused: boolean) => {
      focused = mockFocused;
      listeners.forEach((listener) => listener(mockFocused));
    },
    useFocusEffect: (effect: () => undefined | (() => void)) => {
      const [isFocused, setFocused] = React.useState(focused);
      React.useEffect(() => {
        listeners.add(setFocused);
        return () => {
          listeners.delete(setFocused);
        };
      }, []);
      React.useEffect(() => (isFocused ? effect() : undefined), [isFocused, effect]);
    },
  };
});

jest.mock("../src/api", () => ({
  ...jest.requireActual("../src/api"),
  api: { orders: jest.fn() },
}));

const router = jest.requireMock("expo-router") as { __setFocused: (f: boolean) => void };
const mocked = api as jest.Mocked<typeof api>;

const ROW: OrderRow = {
  ref: "AB12CD34",
  kind: "order",
  state: "placed",
  amount_minor: 4250,
  currency: "USD",
  updated_at: "2026-11-03T10:00:00Z",
  rationale: "Fresh today and in stock.",
};

beforeEach(() => {
  jest.useFakeTimers();
  jest.resetAllMocks();
  router.__setFocused(true);
  mocked.orders.mockResolvedValue([ROW]);
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

test("shows reference, state, amount and the seller's rationale", async () => {
  const tree = await render(<Orders />);
  await flush();
  expect(has(tree, "order-AB12CD34")).toBe(true);
  expect(text(tree)).toContain("Order · Placed");
  expect(text(tree)).toContain("Ref AB12CD34");
  expect(text(tree)).toContain("$42.50");
  expect(text(tree)).toContain("Fresh today and in stock.");
});

test("refreshes every 5 s while focused, and stops when the screen is left", async () => {
  const tree = await render(<Orders />);
  await flush();
  expect(mocked.orders).toHaveBeenCalledTimes(1);
  mocked.orders.mockResolvedValue([{ ...ROW, state: "in_delivery" }]);
  await tick(REFRESH_MS);
  expect(mocked.orders).toHaveBeenCalledTimes(2);
  expect(text(tree)).toContain("Out for delivery");
  await tick(REFRESH_MS);
  expect(mocked.orders).toHaveBeenCalledTimes(3);

  await act(async () => router.__setFocused(false));
  await tick(REFRESH_MS * 3);
  expect(mocked.orders).toHaveBeenCalledTimes(3);

  // Coming back loads at once and polls again.
  await act(async () => router.__setFocused(true));
  await flush();
  expect(mocked.orders).toHaveBeenCalledTimes(4);
  await tick(REFRESH_MS);
  expect(mocked.orders).toHaveBeenCalledTimes(5);
});

test("loading, empty and error states", async () => {
  let resolve: (rows: OrderRow[]) => void = () => {};
  mocked.orders.mockReturnValueOnce(new Promise((r) => (resolve = r)));
  const tree = await render(<Orders />);
  expect(has(tree, "orders-loading")).toBe(true);
  await act(async () => resolve([]));
  expect(has(tree, "orders-empty")).toBe(true);

  mocked.orders.mockRejectedValue(new Error("offline"));
  const failing = await render(<Orders />);
  await flush();
  expect(has(failing, "orders-error")).toBe(true);
  mocked.orders.mockResolvedValue([ROW]);
  await press(failing, "orders-error");
  expect(has(failing, "orders-error")).toBe(false);
  expect(has(failing, "order-AB12CD34")).toBe(true);
});

test("labels and money", () => {
  expect(stateLabel("open")).toBe("Offer sent");
  expect(stateLabel("something_new")).toBe("In progress");
  expect(money(4250, "USD")).toBe("$42.50");
  expect(money(null, "USD")).toBe("");
});

test("the organisation comes from the token", () => {
  const token = (claims: object) =>
    `h.${btoa(JSON.stringify(claims)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")}.s`;
  expect(orgOf(token({ org_id: "square-restaurant-us" }))).toBe("square-restaurant-us");
  expect(orgOf(token({ preferred_username: "owner.kroger-us" }))).toBe("kroger-us");
  expect(orgOf(token({ preferred_username: "sim.us-2.1" }))).toBeNull();
  expect(orgOf(token({ preferred_username: "owner." }))).toBeNull();
  expect(orgOf("not-a-token")).toBeNull();
});
