import { StyleSheet, Text, View } from "react-native";

import { spacing, type } from "./theme";

type Props = { label: string; value: string | number; testID?: string };

// One number with its label, such as today's quotes won on Home.
export function Stat({ label, value, testID }: Props) {
  return (
    <View
      accessible
      accessibilityLabel={`${label}: ${value}`}
      style={styles.stat}
      testID={testID}
    >
      <Text style={type.stat}>{value}</Text>
      <Text style={type.caption}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  stat: { flex: 1, gap: spacing.xs, minWidth: 80 },
});
