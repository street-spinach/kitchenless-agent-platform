// Orders (experience.md §11): live orders and negotiations, refreshed every 5 s while the
// screen is open.
import { useFocusEffect } from "expo-router";
import { useCallback, useRef, useState } from "react";
import { ScrollView, StyleSheet } from "react-native";

import { api, money, type OrderRow } from "../../src/api";
import { Card, Empty, ErrorState, ListRow, Skeleton, spacing } from "../../src/ui";

export const REFRESH_MS = 5000;

const STATE_LABEL: Record<string, string> = {
  open: "Offer sent",
  countered: "Counter-offer",
  awaiting_approval: "Awaiting approval",
  authorized: "Approved",
  placed: "Placed",
  courier_booked: "Courier booked",
  in_delivery: "Out for delivery",
};

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state.replace(/_/g, " ");
}

export default function Orders() {
  const [rows, setRows] = useState<OrderRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const loaded = useRef(false);

  const load = useCallback(async () => {
    try {
      const next = await api.orders();
      loaded.current = true;
      setRows(next);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  // Poll only while the screen is focused; leaving it stops the timer.
  useFocusEffect(
    useCallback(() => {
      load();
      const timer = setInterval(load, REFRESH_MS);
      return () => clearInterval(timer);
    }, [load]),
  );

  if (failed && !loaded.current) return <ErrorState onRetry={load} testID="orders-error" />;
  if (rows === null) return <Skeleton testID="orders-loading" />;
  if (rows.length === 0) return <Empty testID="orders-empty" text="No live orders right now." />;
  return (
    <ScrollView contentContainerStyle={styles.page}>
      {failed ? <ErrorState onRetry={load} testID="orders-error" /> : null}
      <Card>
        {rows.map((r) => (
          <ListRow
            key={`${r.kind}-${r.ref}`}
            subtitle={r.rationale ?? undefined}
            testID={`order-${r.ref}`}
            title={`${r.ref} · ${stateLabel(r.state)}`}
            trailing={money(r.amount_minor, r.currency)}
          />
        ))}
      </Card>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { gap: spacing.md, padding: spacing.md },
});
