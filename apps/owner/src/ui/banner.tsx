import { StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing } from "./theme";

type Props = { text: string; tone?: "info" | "warning"; testID?: string };

// A one-line notice, such as Insights' "Sample data" banner.
export function Banner({ text, tone = "info", testID }: Props) {
  const warning = tone === "warning";
  return (
    <View
      accessibilityRole="alert"
      style={[styles.banner, { backgroundColor: warning ? colors.warning : colors.info }]}
      testID={testID}
    >
      <Text style={[styles.text, { color: warning ? colors.warningText : colors.infoText }]}>
        {text}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: { borderRadius: radius.sm, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  text: { fontSize: 14, fontWeight: "500" },
});
