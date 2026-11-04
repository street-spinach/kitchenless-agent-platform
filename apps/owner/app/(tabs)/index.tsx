// Home (experience.md §11): the discount card and its sheet, today's counts, the latest 3
// notices. Two taps change the discount (XG13): the card, then a value.
import { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";

import { api, type Notice, type Rules, type Summary } from "../../src/api";
import {
  Banner,
  Card,
  Empty,
  ErrorState,
  ListRow,
  Sheet,
  Skeleton,
  Stat,
  colors,
  spacing,
  type,
} from "../../src/ui";

// The fixed choices of the discount sheet, in percent.
export const DISCOUNT_CHOICES = [0, 2, 4, 5, 10, 15, 20];

type Data = { rules: Rules; summary: Summary; notices: Notice[] };

export function discountText(pct: number | null): string {
  return pct === null ? "Most discount you'll give: not set" : `Most discount you'll give: ${pct}%`;
}

export default function Home() {
  const [data, setData] = useState<Data | null>(null);
  const [failed, setFailed] = useState(false);
  const [sheet, setSheet] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);

  const load = useCallback(async () => {
    setFailed(false);
    try {
      const [rules, summary, notices] = await Promise.all([
        api.rules(),
        api.summary(),
        api.notices(3),
      ]);
      setData({ rules, summary, notices });
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const choose = async (pct: number) => {
    setSaveFailed(false);
    try {
      const rules = await api.setDiscount(pct);
      setData((d) => (d ? { ...d, rules } : d));
      setSheet(false);
    } catch {
      // Nothing is kept or retried by itself (§16 "Owner app offline").
      setSaveFailed(true);
    }
  };

  if (failed) return <ErrorState onRetry={load} testID="home-error" />;
  if (!data) return <Skeleton testID="home-loading" />;

  const pct = data.rules.max_discount_pct;
  return (
    <ScrollView contentContainerStyle={styles.page}>
      <Card
        accessibilityLabel={`${discountText(pct)}. Change`}
        onPress={() => {
          setSaveFailed(false);
          setSheet(true);
        }}
        testID="discount-card"
        title="Discount"
        tone="hero"
      >
        <Text style={[type.body, styles.heroText]}>{discountText(pct)}</Text>
      </Card>
      <Card title="Today">
        <View style={styles.stats}>
          <Stat label="Quotes" testID="stat-quotes" value={data.summary.quotes_today} />
          <Stat label="Quotes won" testID="stat-won" value={data.summary.won_today} />
          <Stat label="Orders" testID="stat-orders" value={data.summary.orders_today} />
        </View>
      </Card>
      <Card title="Notices">
        {data.notices.length === 0 ? (
          <Empty testID="notices-empty" text="No notices yet." />
        ) : (
          data.notices.map((n) => (
            <ListRow
              key={`${n.kind}-${n.at}`}
              subtitle={new Date(n.at).toLocaleString()}
              title={n.text}
            />
          ))
        )}
      </Card>
      <Sheet
        onClose={() => setSheet(false)}
        testID="discount-sheet"
        title="Most discount you'll give"
        visible={sheet}
      >
        {saveFailed ? (
          <Banner testID="discount-save-error" text="Couldn't save. Try again." tone="warning" />
        ) : null}
        {DISCOUNT_CHOICES.map((choice) => (
          <ListRow
            key={choice}
            onPress={() => choose(choice)}
            selected={choice === pct}
            testID={`discount-${choice}`}
            title={`${choice}%`}
          />
        ))}
      </Sheet>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  heroText: { color: colors.onHero },
  page: { gap: spacing.md, padding: spacing.md },
  stats: { flexDirection: "row", justifyContent: "space-between" },
});
