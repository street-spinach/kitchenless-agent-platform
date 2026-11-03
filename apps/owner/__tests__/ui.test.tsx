// EX-I43: a render test for each shared component.
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { Text } from "react-native";

import {
  Banner,
  Card,
  Empty,
  ErrorState,
  ListRow,
  Sheet,
  Skeleton,
  Stat,
  theme,
} from "../src/ui";

async function render(node: React.ReactElement): Promise<ReactTestRenderer> {
  let tree: ReactTestRenderer | undefined;
  await act(async () => {
    tree = create(node);
  });
  if (!tree) throw new Error("not rendered");
  return tree;
}

const text = (tree: ReactTestRenderer) => JSON.stringify(tree.toJSON());

test("theme has the shared tokens", () => {
  expect(theme.colors.primary).toMatch(/^#/);
  expect(theme.spacing.lg).toBeGreaterThan(theme.spacing.sm);
  expect(theme.type.stat.fontSize).toBeGreaterThan(theme.type.body.fontSize);
});

test("Card renders its title and children, and is one tap target with onPress", async () => {
  const onPress = jest.fn();
  const tree = await render(
    <Card title="Most discount you'll give: 10%" onPress={onPress} testID="card">
      <Text>Floor on Margherita: $9.50</Text>
    </Card>,
  );
  expect(text(tree)).toContain("Most discount you'll give: 10%");
  expect(text(tree)).toContain("Floor on Margherita");
  await act(async () => {
    tree.root.findByProps({ testID: "card" }).props.onPress();
  });
  expect(onPress).toHaveBeenCalledTimes(1);
});

test("Card without onPress is not a button", async () => {
  const tree = await render(<Card title="Today" />);
  expect(tree.root.findAll((n) => n.props.accessibilityRole === "button")).toHaveLength(0);
});

test("Stat renders its value and label", async () => {
  const tree = await render(<Stat label="Quotes won" value={7} />);
  expect(text(tree)).toContain("Quotes won");
  expect(text(tree)).toContain("7");
});

test("ListRow renders title, subtitle, trailing text and the selected mark", async () => {
  const onPress = jest.fn();
  const tree = await render(
    <ListRow title="20%" subtitle="Floor $8.00" trailing="now" selected onPress={onPress} testID="row" />,
  );
  expect(text(tree)).toContain("20%");
  expect(text(tree)).toContain("Floor $8.00");
  expect(text(tree)).toContain("✓");
  await act(async () => {
    tree.root.findByProps({ testID: "row" }).props.onPress();
  });
  expect(onPress).toHaveBeenCalledTimes(1);
});

test("Sheet shows its title and choices when visible", async () => {
  const onClose = jest.fn();
  const tree = await render(
    <Sheet visible title="Most discount" onClose={onClose}>
      <ListRow title="10%" />
      <ListRow title="20%" />
    </Sheet>,
  );
  expect(text(tree)).toContain("Most discount");
  expect(text(tree)).toContain("20%");
  await act(async () => {
    tree.root.findByProps({ accessibilityLabel: "Close" }).props.onPress();
  });
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("Banner renders its text in both tones", async () => {
  expect(text(await render(<Banner text="Sample data" />))).toContain("Sample data");
  expect(text(await render(<Banner text="Offline" tone="warning" />))).toContain("Offline");
});

test("Empty renders its message", async () => {
  expect(text(await render(<Empty text="Not enough orders yet" />))).toContain(
    "Not enough orders yet",
  );
});

test("ErrorState offers Try again", async () => {
  const onRetry = jest.fn();
  const tree = await render(<ErrorState onRetry={onRetry} />);
  expect(text(tree)).toContain("Try again");
  await act(async () => {
    tree.root.findByProps({ accessibilityRole: "button" }).props.onPress();
  });
  expect(onRetry).toHaveBeenCalledTimes(1);
});

test("Skeleton renders the requested number of bars and is busy", async () => {
  const tree = await render(<Skeleton lines={4} testID="sk" />);
  const [root] = tree.root.findAll((n) => typeof n.type === "string" && n.props.testID === "sk");
  expect(root.props.accessibilityState).toEqual({ busy: true });
  expect(root.props.accessibilityLabel).toBe("Loading");
  expect(root.children).toHaveLength(4);
});
