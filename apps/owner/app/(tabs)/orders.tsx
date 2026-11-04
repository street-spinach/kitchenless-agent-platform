// Orders (experience.md §11): live orders and negotiations, refreshed every 5 s while the
// screen is open. A live row shows a lime "Live" badge; tapping a row opens the negotiation
// sheet (showcase.md §6), refreshed every 5 s while it is open.
import { useFocusEffect } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ScrollView, StyleSheet } from "react-native";

import { api, money, type OrderRow, type Turn, type Turns } from "../../src/api";
import { Card, Empty, ErrorState, ListRow, Sheet, Skeleton, spacing } from "../../src/ui";

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

// §6: an order is live while it is quoting or negotiating; an open or countered quote is a
// negotiation still going on.
const LIVE_STATES = new Set(["quoting", "negotiating"]);

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state.replace(/_/g, " ");
}

export function isLive(row: OrderRow): boolean {
  return row.kind === "quote" || LIVE_STATES.has(row.state);
}

const WHO: Record<Turn["by"], string> = { you: "You", them: "They", system: "" };

/** One timeline line, e.g. "They countered at $40.80 (your floor was $38.40)". */
export function turnTitle(turn: Turn, currency: string): string {
  const who = WHO[turn.by];
  const at = turn.amount_minor === undefined ? "" : money(turn.amount_minor, currency);
  let line: string;
  switch (turn.kind) {
    case "request":
      line = turn.text ? `Request: ${turn.text}` : "Request";
      break;
    case "offer":
      line = at ? `${who} offered ${at}` : `${who} offered`;
      break;
    case "counter":
      line = at ? `${who} countered at ${at}` : `${who} countered`;
      break;
    case "accept":
      line = at ? `${who} accepted at ${at}` : `${who} accepted`;
      break;
    case "reject":
      line = turn.text ? `${who} declined: ${turn.text}` : `${who} declined`;
      break;
    case "cancel":
      line = `${who} cancelled`;
      break;
    default:
      line = turn.text ?? stateLabel(turn.kind);
  }
  line = line.trim();
  if (turn.floor_minor !== undefined) {
    line += ` (your floor was ${money(turn.floor_minor, currency)})`;
  }
  return line;
}

function TurnsSheet({ row, onClose }: { row: OrderRow | null; onClose: () => void }) {
  const ref = row?.ref ?? null;
  const [data, setData] = useState<Turns | null>(null);
  const [failed, setFailed] = useState(false);
  const current = useRef<string | null>(ref);
  current.current = ref;

  const load = useCallback(async () => {
    if (!ref) return;
    try {
      const next = await api.turns(ref);
      if (current.current !== ref) return; // a reply for a sheet already closed
      setData(next);
      setFailed(false);
    } catch {
      if (current.current === ref) setFailed(true);
    }
  }, [ref]);

  // Poll only while the sheet is open; closing it stops the timer.
  useEffect(() => {
    setData(null);
    setFailed(false);
    if (!ref) return undefined;
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [ref, load]);

  const currency = row?.currency ?? "USD";
  let body;
  if (failed && data === null) body = <ErrorState onRetry={load} testID="turns-error" />;
  else if (data === null) body = <Skeleton testID="turns-loading" />;
  else if (data.turns.length === 0) body = <Empty testID="turns-empty" text="No messages yet." />;
  else
    body = (
      <ScrollView style={styles.timeline} testID="turns">
        {failed ? <ErrorState onRetry={load} testID="turns-error" /> : null}
        {data.turns.map((t, i) => (
          <ListRow
            key={`${i}-${t.at}`}
            subtitle={t.rationale ?? undefined}
            testID={`turn-${i}`}
            title={turnTitle(t, currency)}
          />
        ))}
      </ScrollView>
    );
  return (
    <Sheet
      onClose={onClose}
      testID="turns-sheet"
      title={ref ? `Negotiation · ${ref}` : ""}
      visible={ref !== null}
    >
      {ref ? body : null}
    </Sheet>
  );
}

export default function Orders() {
  const [rows, setRows] = useState<OrderRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<OrderRow | null>(null);
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
            badge={isLive(r) ? "Live" : undefined}
            key={`${r.kind}-${r.ref}`}
            onPress={() => setOpen(r)}
            subtitle={r.rationale ?? undefined}
            testID={`order-${r.ref}`}
            title={`${r.ref} · ${stateLabel(r.state)}`}
            trailing={money(r.amount_minor, r.currency)}
          />
        ))}
      </Card>
      <TurnsSheet onClose={() => setOpen(null)} row={open} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { gap: spacing.md, padding: spacing.md },
  timeline: { maxHeight: 420 },
});
