"""`fga model transform --file authz/model.fga | python -m authz.check_transform` (I7).

Exits non-zero unless the transform on stdin equals `authz/model.json`. Both sides are
compared after dropping empty values (`""`, `null`, `{}`, `[]`, except a direct relation's
`"this": {}`), which the transformer may or may not print, so only a real model difference
fails `make fga-test`.
"""

import json
import sys
from typing import Any

from authz.apply import MODEL_PATH


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: normalize(v) for k, v in value.items()}
        # `"this": {}` is a direct relation, not an empty value.
        return {k: v for k, v in out.items() if k == "this" or v not in ("", None, {}, [])}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def main() -> None:
    fresh = normalize(json.load(sys.stdin))
    committed = normalize(json.loads(MODEL_PATH.read_text()))
    if fresh != committed:
        print(
            "authz/model.json does not match authz/model.fga; regenerate it with"
            " `fga model transform --file authz/model.fga`",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print("authz/model.json matches authz/model.fga")


if __name__ == "__main__":
    main()
