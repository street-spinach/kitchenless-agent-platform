// Shared helpers for the owner screen tests in __tests__/.
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const mounted: ReactTestRenderer[] = [];

export async function render(node: React.ReactElement): Promise<ReactTestRenderer> {
  let tree: ReactTestRenderer | undefined;
  await act(async () => {
    tree = create(node);
  });
  if (!tree) throw new Error("not rendered");
  mounted.push(tree);
  return tree;
}

/** Unmounts every tree `render` made, so no timer or listener outlives its test. */
export function cleanup() {
  act(() => {
    mounted.splice(0).forEach((tree) => tree.unmount());
  });
}

export const text = (tree: ReactTestRenderer) => JSON.stringify(tree.toJSON());

export const has = (tree: ReactTestRenderer, testID: string) =>
  tree.root.findAll((n) => n.props.testID === testID).length > 0;

/** Taps a component by testID: its `onPress`, or an ErrorState's `onRetry` ("Try again"). */
export async function press(tree: ReactTestRenderer, testID: string) {
  const node = tree.root.findAll(
    (n) => n.props.testID === testID && (n.props.onPress || n.props.onRetry),
  )[0];
  if (!node) throw new Error(`no pressable ${testID}`);
  await act(async () => {
    await (node.props.onPress ?? node.props.onRetry)();
  });
}

/** Lets pending promises (the mocked API calls) settle inside act. */
export async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}
