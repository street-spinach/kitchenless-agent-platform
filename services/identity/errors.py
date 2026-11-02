"""Identity exception classes (identity-authz I1).

Build step 4 adds only the dependency errors that `fga.py` raises. Step 5 adds the rest of
I1 (`IdentityError` and its subclasses, `REASONS` in `claims.py`).
"""


class DependencyUnavailable(Exception):
    """An identity dependency did not answer. Not a refusal (I-G9)."""

    reason: str  # set by each subclass, from the §12 reason codes

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.reason)


class AuthzUnavailable(DependencyUnavailable):
    """OpenFGA timed out or could not be reached (identity-authz §8 step 2, §12)."""

    reason = "authz_unavailable"
