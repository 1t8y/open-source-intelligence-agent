"""Shared request parameter types for the JSON API.

One rule needs stating once, here: **a timestamp query parameter must carry a timezone offset.**

`persistence/base.py:UTCDateTime` refuses to guess which zone a naive timestamp was meant in, and
it is right to. Reading `2026-09-24T00:00:00` as UTC would be a silent assumption; reading it as
local time would make the same URL mean different things on different deployments. Both are worse
than saying no.

What this type changes is *where* the refusal happens. Without it the `ValueError` surfaces from
inside the query builder as a `500`, which tells the caller nothing and looks like an outage. With
it, the same input is rejected at the boundary as a `422` naming the field, which is what
`docs/api-contract.md` §9 promises for an illegal parameter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

_OFFSET_HINT = "timestamp must include a timezone offset, e.g. 2026-09-24T00:00:00Z"


def _require_timezone_offset(value: datetime) -> datetime:
    """Reject a timestamp that does not say which zone it is in."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(_OFFSET_HINT)
    return value


#: A `datetime` that is guaranteed to be timezone-aware before it reaches a query.
UtcDateTime = Annotated[datetime, AfterValidator(_require_timezone_offset)]
