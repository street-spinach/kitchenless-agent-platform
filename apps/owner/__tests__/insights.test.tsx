// Insights (EX-I50): hidden cells and the "Sample data" banner.
import Insights, { HIDDEN_TEXT, SAMPLE_TEXT } from "../app/(tabs)/insights";
import { api, type Insights as Data } from "../src/api";
import { cleanup, flush, has, render, text } from "../testing/screens";

jest.mock("../src/api", () => ({
  ...jest.requireActual("../src/api"),
  api: { insights: jest.fn() },
}));

const mocked = api as jest.Mocked<typeof api>;

const SAMPLE: Data = {
  week: "2026-W45",
  sample: true,
  cells: [
    { metric: "busiest_hour", label: "Busiest", value: "Thursday 18:00–19:00" },
    { metric: "top_constraint", label: "Most asked", value: "nut-free" },
    { metric: "quotes_won", label: "Quotes won", value: "7 of 12" },
  ],
};

beforeEach(() => jest.resetAllMocks());
afterEach(cleanup);

test("shows_sample_banner", async () => {
  mocked.insights.mockResolvedValue(SAMPLE);
  const tree = await render(<Insights />);
  await flush();
  expect(has(tree, "sample-banner")).toBe(true);
  expect(text(tree)).toContain(SAMPLE_TEXT);
  expect(text(tree)).toContain("Thursday 18:00–19:00");
  expect(text(tree)).toContain("nut-free");
  expect(text(tree)).toContain("7 of 12");
  expect(text(tree)).toContain("2026-W45");
});

test("real data has no banner, and a hidden cell reads 'Not enough orders yet'", async () => {
  mocked.insights.mockResolvedValue({
    week: "2026-W45",
    sample: false,
    cells: [
      { metric: "busiest_hour", hidden: true },
      { metric: "top_constraint", hidden: true },
      { metric: "quotes_won", label: "Quotes won", value: "5 of 6" },
    ],
  });
  const tree = await render(<Insights />);
  await flush();
  expect(has(tree, "sample-banner")).toBe(false);
  const busiest = tree.root.findAll((n) => n.props.testID === "cell-busiest_hour")[0];
  expect(busiest.props.trailing).toBe(HIDDEN_TEXT);
  expect(busiest.props.title).toBe("Busiest");
  expect(text(tree).split(HIDDEN_TEXT)).toHaveLength(3);
  expect(text(tree)).toContain("5 of 6");
});
