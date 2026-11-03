// `/owner/auth`, the sign-in redirect (EX-I44): finishes the PKCE exchange, then Home.
import { router } from "expo-router";
import { useEffect, useState } from "react";

import { completeSignIn } from "../src/auth";
import { ErrorState, Skeleton } from "../src/ui";

export default function AuthCallback() {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const query = Object.fromEntries(new URLSearchParams(window.location.search));
    completeSignIn(window.location.origin, query)
      .then(() => router.replace("/"))
      .catch(() => setFailed(true));
  }, []);

  if (failed) {
    return (
      <ErrorState
        onRetry={() => router.replace("/")}
        testID="auth-error"
        text="Sign-in didn't finish."
      />
    );
  }
  return <Skeleton testID="auth-loading" />;
}
