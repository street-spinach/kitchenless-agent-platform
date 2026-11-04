// Every owner screen has loading, empty and error states (experience.md §11, EX-I50).
import type { ReactElement } from "react";

import Account, { modeName, vendorName } from "../app/(tabs)/account";
import Home from "../app/(tabs)/index";
import Insights from "../app/(tabs)/insights";
import Orders from "../app/(tabs)/orders";
import { api } from "../src/api";
import { cleanup, flush, has, press, render, text } from "../testing/screens";

jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return {
    useFocusEffect: (effect: () => undefined | (() => void)) => React.useEffect(effect, [effect]),
  };
});

jest.mock("../src/api", () => ({
  ...jest.requireActual("../src/api"),
  api: {
    rules: jest.fn(),
    setDiscount: jest.fn(),
    summary: jest.fn(),
    notices: jest.fn(),
    orders: jest.fn(),
    insights: jest.fn(),
    connection: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;
const never = () => new Promise<never>(() => {});

type Screen = {
  name: string;
  node: () => ReactElement;
  prefix: string;
  pending: () => void;
  empty: () => void;
  fail: () => void;
  emptyID: string;
};

const SCREENS: Screen[] = [
  {
    name: "Home",
    node: () => <Home />,
    prefix: "home",
    pending: () => mocked.rules.mockImplementation(never),
    empty: () => mocked.notices.mockResolvedValue([]),
    fail: () => mocked.summary.mockRejectedValue(new Error("offline")),
    emptyID: "notices-empty",
  },
  {
    name: "Orders",
    node: () => <Orders />,
    prefix: "orders",
    pending: () => mocked.orders.mockImplementation(never),
    empty: () => mocked.orders.mockResolvedValue([]),
    fail: () => mocked.orders.mockRejectedValue(new Error("offline")),
    emptyID: "orders-empty",
  },
  {
    name: "Insights",
    node: () => <Insights />,
    prefix: "insights",
    pending: () => mocked.insights.mockImplementation(never),
    empty: () => mocked.insights.mockResolvedValue({ week: "2026-W45", sample: false, cells: [] }),
    fail: () => mocked.insights.mockRejectedValue(new Error("offline")),
    emptyID: "insights-empty",
  },
  {
    name: "Account",
    node: () => <Account />,
    prefix: "account",
    pending: () => mocked.connection.mockImplementation(never),
    empty: () => mocked.connection.mockResolvedValue(null),
    fail: () => mocked.connection.mockRejectedValue(new Error("offline")),
    emptyID: "account-empty",
  },
];

beforeEach(() => {
  jest.resetAllMocks();
  mocked.rules.mockResolvedValue({ org_id: "o-1", max_discount_pct: 4 });
  mocked.summary.mockResolvedValue({ quotes_today: 0, won_today: 0, orders_today: 0 });
  mocked.notices.mockResolvedValue([]);
  mocked.orders.mockResolvedValue([]);
  mocked.insights.mockResolvedValue({ week: "2026-W45", sample: false, cells: [] });
  mocked.connection.mockResolvedValue(null);
});
afterEach(cleanup);

describe.each(SCREENS)("$name", (screen) => {
  test("loading", async () => {
    screen.pending();
    const tree = await render(screen.node());
    expect(has(tree, `${screen.prefix}-loading`)).toBe(true);
  });

  test("empty", async () => {
    screen.empty();
    const tree = await render(screen.node());
    await flush();
    expect(has(tree, screen.emptyID)).toBe(true);
  });

  test("error, and Try again", async () => {
    screen.fail();
    const tree = await render(screen.node());
    await flush();
    expect(has(tree, `${screen.prefix}-error`)).toBe(true);
    expect(text(tree)).toContain("Try again");
    jest.resetAllMocks();
    screen.empty();
    mocked.rules.mockResolvedValue({ org_id: "o-1", max_discount_pct: 4 });
    mocked.summary.mockResolvedValue({ quotes_today: 0, won_today: 0, orders_today: 0 });
    await press(tree, `${screen.prefix}-error`);
    await flush();
    expect(has(tree, `${screen.prefix}-error`)).toBe(false);
  });
});

test("Account shows the connection, read-only, and always offers sign out", async () => {
  mocked.connection.mockResolvedValue({
    vendor: "square_restaurant",
    mode: "mock",
    set_at: null,
  });
  const tree = await render(<Account />);
  await flush();
  expect(text(tree)).toContain("Square (restaurant)");
  expect(text(tree)).toContain("simulated");
  expect(has(tree, "sign-out")).toBe(true);
  expect(modeName("real")).toBe("Live: real orders");
  expect(vendorName("unknown_vendor")).toBe("Your store");
});
