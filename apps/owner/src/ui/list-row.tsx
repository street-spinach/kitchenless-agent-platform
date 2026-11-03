import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, spacing, type } from "./theme";

type Props = {
  title: string;
  subtitle?: string;
  trailing?: string;
  onPress?: () => void;
  // A fixed choice in a sheet (EX-OD10 "pickers wherever a choice is fixed").
  selected?: boolean;
  testID?: string;
};

export function ListRow({ title, subtitle, trailing, onPress, selected, testID }: Props) {
  const content = (
    <>
      <View style={styles.text}>
        <Text style={type.body}>{title}</Text>
        {subtitle ? <Text style={type.caption}>{subtitle}</Text> : null}
      </View>
      {trailing ? <Text style={type.caption}>{trailing}</Text> : null}
      {selected ? <Text style={styles.check}>✓</Text> : null}
    </>
  );
  if (onPress) {
    return (
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ selected: !!selected }}
        onPress={onPress}
        style={({ pressed }) => [styles.row, pressed && styles.pressed]}
        testID={testID}
      >
        {content}
      </Pressable>
    );
  }
  return (
    <View style={styles.row} testID={testID}>
      {content}
    </View>
  );
}

const styles = StyleSheet.create({
  check: { color: colors.primary, fontSize: 16, fontWeight: "700" },
  pressed: { backgroundColor: colors.background },
  row: {
    alignItems: "center",
    borderBottomColor: colors.border,
    borderBottomWidth: StyleSheet.hairlineWidth,
    flexDirection: "row",
    gap: spacing.md,
    minHeight: 48,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  text: { flex: 1, gap: 2 },
});
