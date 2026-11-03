import { act, create, type ReactTestRenderer } from "react-test-renderer";

import RootLayout from "../app/_layout";

jest.mock("expo-router", () => {
  const { Text } = jest.requireActual("react-native");
  return { Stack: () => <Text>owner</Text> };
});

test("the root layout renders", async () => {
  let tree: ReactTestRenderer | undefined;
  await act(async () => {
    tree = create(<RootLayout />);
  });
  expect(JSON.stringify(tree?.toJSON())).toContain("owner");
});
