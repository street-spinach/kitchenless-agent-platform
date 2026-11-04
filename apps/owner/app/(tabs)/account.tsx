// Account (experience.md §11): the store's connection status, read-only, and sign out.
import { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet, Text } from "react-native";

import { api, type Connection } from "../../src/api";
import { signOut } from "../../src/auth";
import { when } from "../../src/when";
import { Card, Empty, ErrorState, ListRow, Skeleton, spacing, type } from "../../src/ui";

const VENDORS: Record<string, string> = {
  square_restaurant: "Square (restaurant)",
  square_retail: "Square (retail)",
  kroger_catalog: "Kroger",
  kroger_ordering: "Kroger",
  doordash: "DoorDash Drive",
  mock: "Kitchenless demo store",
};

export function vendorName(vendor: string): string {
  return VENDORS[vendor] ?? "Your store";
}

// `mock` is shown as "simulated" (§11 Account), in plain words (#434).
export function modeName(mode: string): string {
  return mode === "real" ? "Live: real orders" : "Practice: simulated orders";
}

export function leave() {
  signOut();
  window.location.assign("/owner/");
}

export default function Account() {
  const [data, setData] = useState<Connection | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setFailed(false);
    try {
      setData(await api.connection());
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const signOutCard = (
    <Card accessibilityLabel="Sign out" onPress={leave} testID="sign-out">
      <Text style={type.body}>Sign out</Text>
    </Card>
  );

  let status;
  if (failed) status = <ErrorState onRetry={load} testID="account-error" />;
  else if (data === undefined) status = <Skeleton testID="account-loading" />;
  else if (data === null) status = <Empty testID="account-empty" text="No store connected yet." />;
  else
    status = (
      <Card title="Connection">
        <ListRow testID="connection-vendor" title="Store" trailing={vendorName(data.vendor)} />
        <ListRow testID="connection-mode" title="Orders" trailing={modeName(data.mode)} />
        <ListRow
          testID="connection-set-at"
          title="Last changed"
          trailing={data.set_at ? when(data.set_at) : "Never"}
        />
      </Card>
    );

  return (
    <ScrollView contentContainerStyle={styles.page}>
      {status}
      {signOutCard}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { gap: spacing.md, padding: spacing.md },
});
