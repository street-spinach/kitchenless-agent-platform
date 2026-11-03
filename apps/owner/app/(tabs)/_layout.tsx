// The owner app's bottom tabs (experience.md §11). Signed out, it shows only "Sign in".
import { Tabs } from "expo-router";
import { StyleSheet, Text, View } from "react-native";

import { accessToken, beginSignIn } from "../../src/auth";
import { Card, colors, spacing, type } from "../../src/ui";

export function signIn() {
  const { origin } = window.location;
  return beginSignIn(origin, (url) => window.location.assign(url));
}

export default function TabsLayout() {
  if (!accessToken()) {
    return (
      <View style={styles.page}>
        <Card accessibilityLabel="Sign in" onPress={signIn} testID="sign-in" title="Kitchenless">
          <Text style={type.body}>Sign in</Text>
        </Card>
      </View>
    );
  }
  return (
    <Tabs screenOptions={{ tabBarActiveTintColor: colors.primary }}>
      <Tabs.Screen name="index" options={{ title: "Home" }} />
      <Tabs.Screen name="orders" options={{ title: "Orders" }} />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, justifyContent: "center", padding: spacing.lg },
});
