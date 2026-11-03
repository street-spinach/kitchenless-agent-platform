// Insights (experience.md §11, EX-OD12): this week's busiest delivery hour, most-asked
// constraint and quotes won. A hidden cell reads "Not enough orders yet"; sample data is
// always labelled.
import { useCallback, useEffect, useState } from "react";
import { ScrollView, StyleSheet } from "react-native";

import { api, type Insights as Data } from "../../src/api";
import { Banner, Card, Empty, ErrorState, ListRow, Skeleton, spacing } from "../../src/ui";

export const HIDDEN_TEXT = "Not enough orders yet";
export const SAMPLE_TEXT = "Sample data";

const TITLES: Record<string, string> = {
  busiest_hour: "Busiest",
  top_constraint: "Most asked",
  quotes_won: "Quotes won",
};

export default function Insights() {
  const [data, setData] = useState<Data | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setFailed(false);
    try {
      setData(await api.insights());
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (failed) return <ErrorState onRetry={load} testID="insights-error" />;
  if (!data) return <Skeleton testID="insights-loading" />;
  if (data.cells.length === 0) return <Empty testID="insights-empty" text="No insights yet." />;
  return (
    <ScrollView contentContainerStyle={styles.page}>
      {data.sample ? <Banner testID="sample-banner" text={SAMPLE_TEXT} /> : null}
      <Card title={`This week (${data.week})`}>
        {data.cells.map((cell) => (
          <ListRow
            key={cell.metric}
            testID={`cell-${cell.metric}`}
            title={cell.hidden ? (TITLES[cell.metric] ?? cell.metric) : cell.label}
            trailing={cell.hidden ? HIDDEN_TEXT : cell.value}
          />
        ))}
      </Card>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { gap: spacing.md, padding: spacing.md },
});
