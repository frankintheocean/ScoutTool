# ==========================
# CENTRALIZED INPUT VALIDATION
# ==========================
# Pydantic models for query/body validation shared across routes, so
# constraints (limits, ranges, allowed values) live in one place instead
# of being re-checked ad hoc inside each route function. pydantic is
# already a dependency (see requirements.txt / main.py's existing
# BaseModel bodies) — nothing new to install.
#
# Scope: this module covers the new endpoints added alongside it (search
# history, recently viewed, suggestions) plus a couple of reusable
# validators. Existing routes' inline checks (e.g. `if not (1 <= score <=
# 5)`) were left as-is per "modify only required" / "avoid rewrites" —
# converting them carries real regression risk for no behavior change,
# since they already reject the same inputs. New endpoints use this layer
# from the start instead.

from zoneinfo import available_timezones

from pydantic import BaseModel, Field, field_validator

from errors import AppError, ErrorCode

# Resolved once at import time (available_timezones() re-scans the tzdata
# package on every call, and the set is fixed for the life of the
# process) — same "compute once, not per-request" reasoning as the
# frontend's cached Intl.supportedValuesOf("timeZone") list this mirrors.
_VALID_TIMEZONES = available_timezones()


def validate_timezone(timezone):
    """Validates a manually-entered IANA timezone name against Python's
    own zoneinfo database before it reaches storage — set_location()
    previously accepted any string, which meant a typo or garbage value
    would sit in the DB and then silently fail the frontend's
    Intl.DateTimeFormat(...) calls (falling back to showing the raw,
    un-parsed string instead of a formatted local time). None/"" are
    left alone (both are meaningful — see set_location's None-vs-empty-
    string semantics) since only a non-empty candidate value can be
    invalid."""
    if not timezone:
        return timezone
    if timezone not in _VALID_TIMEZONES:
        raise AppError(
            status_code=422,
            detail=f"'{timezone}' is not a recognized IANA timezone name",
            code=ErrorCode.VALIDATION_ERROR,
        )
    return timezone


class DiscoverSearchHistoryEntry(BaseModel):
    """Body for POST /api/discover/history — the filter combo to remember.
    Mirrors the query params /api/discover already accepts; validated as
    a loose dict of primitives rather than one field per filter so this
    stays in sync with /api/discover without duplicating its shape."""

    filters: dict = Field(default_factory=dict)

    @field_validator("filters")
    @classmethod
    def _non_empty_and_small(cls, value):
        if not value:
            raise ValueError("filters must not be empty")
        if len(value) > 20:
            raise ValueError("too many filter keys")
        return value


class LimitQuery(BaseModel):
    """Shared bounds for the `limit` query param used by the new list
    endpoints below — keeps one definition of "how many is too many"
    instead of a different magic number per route."""

    limit: int = Field(default=10, ge=1, le=100)


def validate_limit(limit):
    """Validates a `limit` query param using LimitQuery, raising a
    structured 422 AppError on failure instead of letting a bad value
    (e.g. limit=0 or limit=100000) reach the database layer."""
    try:
        return LimitQuery(limit=limit).limit
    except Exception as e:
        raise AppError(
            status_code=422,
            detail="limit must be between 1 and 100",
            code=ErrorCode.VALIDATION_ERROR,
        ) from e


def validate_discover_filters(filters: dict):
    """Validates + normalizes a Discover filters dict for history storage:
    drops blank/None values so equivalent searches (e.g. category='' vs
    omitted) collapse to the same history entry, then enforces the same
    non-empty/size bounds as DiscoverSearchHistoryEntry."""
    cleaned = {
        k: v for k, v in (filters or {}).items()
        if v is not None and v != "" and not (isinstance(v, bool) and v is False)
    }
    try:
        validated = DiscoverSearchHistoryEntry(filters=cleaned)
    except Exception as e:
        raise AppError(
            status_code=422,
            detail="At least one non-empty filter is required to save a search",
            code=ErrorCode.VALIDATION_ERROR,
        ) from e
    return validated.filters
