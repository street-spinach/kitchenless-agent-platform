// Home (EX-I49): the discount changes in two taps (XG13), and every load or save failure
// shows an error state.
import Home, { DISCOUNT_CHOICES, discountText } from "../app/(tabs)/index";
import { api } from "../src/api";
import { cleanup, flush, has, press, render, text } from "../testing/screens";

jest.mock("../src/api", () => ({
  ...jest.requireActual("../src/api"),
  api: {
    rules: jest.fn(),
    setDiscount: jest.fn(),
    summary: jest.fn(),
    notices: jest.fn(),
    orders: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

beforeEach(() => {
  jest.resetAllMocks();
  mocked.rules.mockResolvedValue({ org_id: "o-1", max_discount_pct: 5 });
  mocked.summary.mockResolvedValue({ quotes_today: 7, won_today: 3, orders_today: 2 });
  mocked.notices.mockResolvedValue([
    {
      kind: "offer_expired",
      text: "An offer expired before the buyer accepted it.",
      at: "2026-11-03T10:00:00Z",
    },
  ]);
  mocked.setDiscount.mockImplementation(async (pct) => ({ org_id: "o-1", max_discount_pct: pct }));
});

afterEach(cleanup);

const sheetOpen = (tree: Awaited<ReturnType<typeof render>>) =>
  tree.root.findAll((n) => n.props.testID === "discount-sheet")[0].props.visible;

test("Home shows the discount, today's counts and the latest 3 notices", async () => {
  const tree = await render(<Home />);
  await flush();
  expect(text(tree)).toContain("Most discount you'll give: 5%");
  expect(text(tree)).toContain("An offer expired before the buyer accepted it.");
  for (const id of ["stat-quotes", "stat-won", "stat-orders"]) expect(has(tree, id)).toBe(true);
  expect(text(tree)).toContain("Quotes won: 3");
  expect(mocked.notices).toHaveBeenCalledWith(3);
});

test("two taps: the discount card, then a value, saves it", async () => {
  const tree = await render(<Home />);
  await flush();
  expect(sheetOpen(tree)).toBe(false);

  await press(tree, "discount-card"); // tap 1
  expect(sheetOpen(tree)).toBe(true);
  for (const pct of DISCOUNT_CHOICES) expect(has(tree, `discount-${pct}`)).toBe(true);

  await press(tree, "discount-10"); // tap 2
  await flush();
  expect(mocked.setDiscount).toHaveBeenCalledTimes(1);
  expect(mocked.setDiscount).toHaveBeenCalledWith(10);
  expect(sheetOpen(tree)).toBe(false);
  expect(text(tree)).toContain("Most discount you'll give: 10%");
});

test("a failed save keeps the sheet open with an error and saves nothing locally", async () => {
  mocked.setDiscount.mockRejectedValue(new Error("offline"));
  const tree = await render(<Home />);
  await flush();
  await press(tree, "discount-card");
  await press(tree, "discount-10");
  await flush();
  expect(sheetOpen(tree)).toBe(true);
  expect(has(tree, "discount-save-error")).toBe(true);
  expect(text(tree)).toContain("Most discount you'll give: 5%");
});

test("a failed load shows the error state, and Try again reloads", async () => {
  mocked.rules.mockRejectedValueOnce(new Error("offline"));
  const tree = await render(<Home />);
  await flush();
  expect(has(tree, "home-error")).toBe(true);
  expect(text(tree)).toContain("Try again");
  await press(tree, "home-error");
  await flush();
  expect(has(tree, "home-error")).toBe(false);
  expect(text(tree)).toContain("Most discount you'll give: 5%");
});

test("loading and empty states", async () => {
  mocked.notices.mockResolvedValue([]);
  let resolve: (v: { org_id: string; max_discount_pct: null }) => void = () => {};
  mocked.rules.mockReturnValue(new Promise((r) => (resolve = r)));
  const tree = await render(<Home />);
  expect(has(tree, "home-loading")).toBe(true);
  resolve({ org_id: "o-1", max_discount_pct: null });
  await flush();
  expect(text(tree)).toContain(discountText(null));
  expect(has(tree, "notices-empty")).toBe(true);
});
