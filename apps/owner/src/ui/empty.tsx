import { StyleSheet, Text, View } from "react-native";

import { spacing, type } from "./theme";

type Props = { text: string; testID?: string };

// The empty state every screen has (S5 "Design system").
export function Empty({ text, testID }: Props) {
  return (
    <View style={styles.empty} testID={testID}>
      <Text style={[type.body, styles.text]}>{text}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  empty: { alignItems: "center", padding: spacing.xl },
  text: { textAlign: "center" },
});
