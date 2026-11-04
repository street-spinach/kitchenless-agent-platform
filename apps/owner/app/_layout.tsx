import { Stack } from "expo-router";

// The root stack has no header of its own: the tabs show each screen's title. Without this
// the browser showed the route group's folder name, "(tabs)" (#434).
export default function RootLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}
