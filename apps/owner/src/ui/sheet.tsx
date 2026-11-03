import type { ReactNode } from "react";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, radius, spacing, type } from "./theme";

type Props = {
  visible: boolean;
  title: string;
  onClose: () => void;
  children?: ReactNode;
  testID?: string;
};

// A bottom sheet of fixed choices: Home → discount card → this sheet → one tap saves (XG13).
export function Sheet({ visible, title, onClose, children, testID }: Props) {
  return (
    <Modal animationType="slide" onRequestClose={onClose} transparent visible={visible}>
      <Pressable
        accessibilityLabel="Close"
        accessibilityRole="button"
        onPress={onClose}
        style={styles.scrim}
      />
      <View accessibilityViewIsModal style={styles.sheet} testID={testID}>
        <Text style={styles.title}>{title}</Text>
        {children}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { backgroundColor: colors.scrim, flex: 1 },
  sheet: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: radius.lg,
    borderTopRightRadius: radius.lg,
    paddingBottom: spacing.xl,
    paddingTop: spacing.lg,
  },
  title: { ...type.heading, paddingBottom: spacing.sm, paddingHorizontal: spacing.lg },
});
