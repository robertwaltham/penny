"""Runtime configuration parameter definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from penny.database import Database

# Auto-populated by ConfigParam.__post_init__
RUNTIME_CONFIG_PARAMS: dict[str, ConfigParam] = {}

# Group names (display order)
GROUP_CHAT = "Chat"
GROUP_BACKGROUND = "Background"
GROUP_MEMORY = "Memory"
GROUP_BROWSE = "Browse"
GROUP_SEND = "Send"
GROUP_EMAIL = "Email"

# Ordered list for display
CONFIG_GROUPS: list[str] = [
    GROUP_CHAT,
    GROUP_BACKGROUND,
    GROUP_MEMORY,
    GROUP_BROWSE,
    GROUP_SEND,
    GROUP_EMAIL,
]


@dataclass
class ConfigParam:
    """Definition of a runtime-configurable parameter.

    Automatically registers itself into RUNTIME_CONFIG_PARAMS on creation.
    """

    key: str
    description: str
    type: type  # int, float, or str
    default: int | float | str | bool  # Default value (single source of truth)
    validator: Callable[[str], int | float | str | bool]  # Parses and validates value from string
    group: str = GROUP_CHAT  # Display group for /config listing

    def __post_init__(self) -> None:
        RUNTIME_CONFIG_PARAMS[self.key] = self


def get_params_by_group() -> list[tuple[str, list[ConfigParam]]]:
    """Return params grouped by category in display order.

    Within each group, params are sorted alphabetically by key.
    """
    groups: dict[str, list[ConfigParam]] = {g: [] for g in CONFIG_GROUPS}
    for param in RUNTIME_CONFIG_PARAMS.values():
        groups[param.group].append(param)
    return [(g, sorted(groups[g], key=lambda p: p.key)) for g in CONFIG_GROUPS if groups[g]]


def _validate_positive_int(value: str) -> int:
    """Validate positive integer."""
    try:
        parsed = int(value)
    except ValueError as e:
        raise ValueError("must be a positive integer") from e

    if parsed <= 0:
        raise ValueError("must be a positive integer")

    return parsed


def _validate_non_negative_int(value: str) -> int:
    """Validate a non-negative integer (0 allowed — e.g. to disable a feature)."""
    try:
        parsed = int(value)
    except ValueError as e:
        raise ValueError("must be a non-negative integer") from e

    if parsed < 0:
        raise ValueError("must be a non-negative integer")

    return parsed


def _validate_positive_float(value: str) -> float:
    """Validate positive float."""
    try:
        parsed = float(value)
    except ValueError as e:
        raise ValueError("must be a positive number") from e

    if parsed <= 0:
        raise ValueError("must be a positive number")

    return parsed


def _validate_non_empty_string(value: str) -> str:
    """Validate non-empty string."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must be a non-empty string")
    return stripped


def _validate_bool(value: str) -> bool:
    """Validate a runtime boolean value."""
    normalized = value.strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    raise ValueError("must be a boolean (true/false, yes/no, on/off, or 1/0)")


DOMAIN_MODE_RESTRICT = "restrict"
DOMAIN_MODE_ALLOW_ALL = "allow_all"
_VALID_DOMAIN_MODES = {DOMAIN_MODE_RESTRICT, DOMAIN_MODE_ALLOW_ALL}


def _validate_domain_mode(value: str) -> str:
    """Validate domain permission mode."""
    stripped = value.strip().lower()
    if stripped not in _VALID_DOMAIN_MODES:
        raise ValueError(f"must be one of: {', '.join(sorted(_VALID_DOMAIN_MODES))}")
    return stripped


def _validate_unit_float(value: str) -> float:
    """Validate float in (0.0, 1.0] range for similarity thresholds."""
    try:
        parsed = float(value)
    except ValueError as e:
        raise ValueError("must be a number between 0 and 1") from e

    if not (0.0 < parsed <= 1.0):
        raise ValueError("must be a number between 0 and 1")

    return parsed


# ── Chat — foreground conversation ───────────────────────────────────────────

ConfigParam(
    key="MAX_STEPS",
    description=(
        "Max agent loop steps per chat message cycle. Equal to "
        "BACKGROUND_MAX_STEPS by default: teaching (the demonstration the "
        "run-end skill extractor snapshots) runs in a chat turn, so any sequence "
        "longer than chat's budget would be unteachable while a collector "
        "could still execute it — equal budgets give both entry points the "
        "same power."
    ),
    type=int,
    default=20,
    validator=_validate_positive_int,
    group=GROUP_CHAT,
)

ConfigParam(
    key="MESSAGE_CONTEXT_LIMIT",
    description="Max recent conversation messages injected into chat context",
    type=int,
    default=20,
    validator=_validate_positive_int,
    group=GROUP_CHAT,
)

# ── Background — every background agent (thinking, notify, extractors) ───────

ConfigParam(
    key="BACKGROUND_MAX_STEPS",
    description=(
        "Max agent loop steps per background-agent cycle. Equal to the chat "
        "MAX_STEPS by default — teaching happens in chat, so the two entry "
        "points share one step budget (teachable == executable)."
    ),
    type=int,
    default=20,
    validator=_validate_positive_int,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="COLLECTOR_TICK_INTERVAL",
    description=(
        "Seconds between Collector dispatcher ticks (idle-gated).  Each tick "
        "the dispatcher checks which collection is most overdue based on its "
        "per-row collector_interval_seconds and runs that one.  Should be "
        "smaller than the smallest per-collection interval — otherwise that "
        "collection waits up to TICK_INTERVAL past its readiness."
    ),
    type=float,
    default=30.0,
    validator=_validate_positive_float,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="COLLECTOR_THROTTLE_AFTER",
    description=(
        "Consecutive idle cycles (no entries written / messages sent) before a "
        "collector backs off — its interval doubles, then the counter resets.  "
        "A productive cycle snaps the interval back to the user's set cadence.  "
        "0 disables auto-throttle."
    ),
    type=int,
    default=3,
    validator=_validate_non_negative_int,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="COLLECTOR_MAX_INTERVAL",
    description=(
        "Ceiling (seconds) for auto-throttle backoff — a collector's interval "
        "never doubles past this.  Default 604800 (one week)."
    ),
    type=int,
    default=604800,
    validator=_validate_positive_int,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="COLLECTOR_RUN_HISTORY",
    description=(
        "How many of a collector's own recent run summaries (the ``done`` summary "
        "of each past cycle) to show it at the top of every cycle, so it knows what "
        "its previous invocations did.  0 disables the section."
    ),
    type=int,
    default=10,
    validator=_validate_non_negative_int,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="IDLE_SECONDS",
    description="Seconds of silence before idle-gated background agents become eligible",
    type=float,
    default=60.0,
    validator=_validate_positive_float,
    group=GROUP_BACKGROUND,
)

ConfigParam(
    key="EMBEDDING_BACKFILL_BATCH_LIMIT",
    description="Max items per embedding backfill cycle on startup",
    type=int,
    default=50,
    validator=_validate_positive_int,
    group=GROUP_BACKGROUND,
)

# ── Memory tool — collection dedup thresholds ────────────────────────────────
#
# A candidate write is a duplicate if ANY signal hits its strict threshold,
# OR if any TWO signals hit their relaxed thresholds.  Three signals:
#   1. key TCR (token-containment ratio; lexical)
#   2. key embedding cosine (paraphrase)
#   3. content embedding cosine
# Strict catches obvious matches on one axis; relaxed catches weak-on-every-
# axis matches a single-signal gate would miss.

ConfigParam(
    key="MEMORY_DEDUP_KEY_TCR_STRICT",
    description="Strict key token-containment threshold for memory dedup",
    type=float,
    default=1.0,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

ConfigParam(
    key="MEMORY_DEDUP_KEY_TCR_RELAXED",
    description=(
        "Relaxed key token-containment threshold (catches abbreviation pairs "
        "like 'applied ai conference' / 'applied ai conf' at exactly 2/3)"
    ),
    type=float,
    default=0.65,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

ConfigParam(
    key="MEMORY_DEDUP_KEY_SIM_STRICT",
    description="Strict key embedding cosine threshold for memory dedup",
    type=float,
    default=0.90,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

ConfigParam(
    key="MEMORY_DEDUP_KEY_SIM_RELAXED",
    description="Relaxed key embedding cosine threshold for memory dedup",
    type=float,
    default=0.75,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

ConfigParam(
    key="MEMORY_DEDUP_CONTENT_SIM_STRICT",
    description="Strict content embedding cosine threshold for memory dedup",
    type=float,
    default=0.90,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

ConfigParam(
    key="MEMORY_DEDUP_CONTENT_SIM_RELAXED",
    description="Relaxed content embedding cosine threshold for memory dedup",
    type=float,
    default=0.75,
    validator=_validate_unit_float,
    group=GROUP_MEMORY,
)

# ── Browse tool ──────────────────────────────────────────────────────────────

ConfigParam(
    key="MAX_QUERIES",
    description="Max parallel queries per browse tool call",
    type=int,
    default=3,
    validator=_validate_positive_int,
    group=GROUP_BROWSE,
)

ConfigParam(
    key="SEARCH_URL",
    description="Base URL for text searches (encoded query is appended)",
    type=str,
    default="https://duckduckgo.com/?q=",
    validator=_validate_non_empty_string,
    group=GROUP_BROWSE,
)

ConfigParam(
    key="DOMAIN_PERMISSION_MODE",
    description="Domain mode: restrict (prompt) or allow_all (auto-allow unknown)",
    type=str,
    default=DOMAIN_MODE_RESTRICT,
    validator=_validate_domain_mode,
    group=GROUP_BROWSE,
)

# ── Send tool — outbound message rate limiting ───────────────────────────────

ConfigParam(
    key="SEND_COOLDOWN_SECONDS",
    description=(
        "Flat cooldown in seconds between autonomous ``send_message`` calls. "
        "Bypassed when the user has replied since the agent's last send (the "
        "next send is conversational, not autonomous)."
    ),
    type=float,
    default=600.0,
    validator=_validate_positive_float,
    group=GROUP_SEND,
)

ConfigParam(
    key="SEND_IMAGE_EXACT_URL_ENABLED",
    description="Attach the captured image from an exact URL cited in the response",
    type=bool,
    default=True,
    validator=_validate_bool,
    group=GROUP_SEND,
)

ConfigParam(
    key="SEND_IMAGE_CITED_DOMAIN_ENABLED",
    description="Attach the closest stored image from a domain cited in the response",
    type=bool,
    default=True,
    validator=_validate_bool,
    group=GROUP_SEND,
)

ConfigParam(
    key="SEND_IMAGE_EMBEDDING_NEAREST_ENABLED",
    description="Attach an embedding-nearest stored image when no cited match is available",
    type=bool,
    default=True,
    validator=_validate_bool,
    group=GROUP_SEND,
)

ConfigParam(
    key="SEND_GENERATED_IMAGE_ENABLED",
    description="Allow images generated by Penny to be sent",
    type=bool,
    default=True,
    validator=_validate_bool,
    group=GROUP_SEND,
)

ConfigParam(
    key="SEND_TOOL_IMAGE_ENABLED",
    description="Allow tool-supplied image attachments to be sent",
    type=bool,
    default=True,
    validator=_validate_bool,
    group=GROUP_SEND,
)

# ── Email tools ──────────────────────────────────────────────────────────────

ConfigParam(
    key="EMAIL_BODY_MAX_LENGTH",
    description="Max character length for email body content",
    type=int,
    default=4000,
    validator=_validate_positive_int,
    group=GROUP_EMAIL,
)

ConfigParam(
    key="EMAIL_SEARCH_LIMIT",
    description="Max email results returned by the search_emails tool",
    type=int,
    default=10,
    validator=_validate_positive_int,
    group=GROUP_EMAIL,
)

ConfigParam(
    key="EMAIL_LIST_LIMIT",
    description="Max email results returned by the list_emails tool",
    type=int,
    default=10,
    validator=_validate_positive_int,
    group=GROUP_EMAIL,
)

ConfigParam(
    key="JMAP_REQUEST_TIMEOUT",
    description="Timeout in seconds for email API requests",
    type=float,
    default=30.0,
    validator=_validate_positive_float,
    group=GROUP_EMAIL,
)


class RuntimeParams:
    """Accessor for runtime-configurable parameters.

    Lookup chain: DB override → env override → ConfigParam.default.
    Supports attribute access with uppercase keys: config.runtime.IDLE_SECONDS
    """

    def __init__(
        self,
        db: Database | None = None,
        env_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self._env_overrides = env_overrides or {}

    def __getattr__(self, name: str) -> Any:
        key = name.upper()
        if key not in RUNTIME_CONFIG_PARAMS:
            raise AttributeError(f"No runtime config param: {name}")

        # 1. Check database
        if self._db is not None:
            db_value = self._get_db_value(key)
            if db_value is not None:
                return db_value

        # 2. Check env overrides (from Config.load)
        if key in self._env_overrides:
            return self._env_overrides[key]

        # 3. Fall back to default
        return RUNTIME_CONFIG_PARAMS[key].default

    def _get_db_value(self, key: str) -> Any:
        """Look up a runtime config override from the database."""
        assert self._db is not None  # Caller guards with `if self._db is not None`
        from sqlmodel import Session, select

        from penny.database.models import RuntimeConfig

        with Session(self._db.engine) as session:
            result = session.exec(select(RuntimeConfig).where(RuntimeConfig.key == key)).first()

        if result is None:
            return None

        param = RUNTIME_CONFIG_PARAMS[key]
        try:
            return param.validator(result.value)
        except ValueError:
            return None

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Read multiple runtime values with one database query.

        This is intended for hot paths that need a coherent snapshot of several
        related settings, such as outbound attachment policy.
        """
        normalized = [key.upper() for key in keys]
        unknown = [key for key in normalized if key not in RUNTIME_CONFIG_PARAMS]
        if unknown:
            raise AttributeError(f"No runtime config param: {unknown[0]}")

        values = {key: RUNTIME_CONFIG_PARAMS[key].default for key in normalized}
        db_keys: set[str] = set()
        if self._db is not None and normalized:
            from sqlmodel import Session, select

            from penny.database.models import RuntimeConfig

            with Session(self._db.engine) as session:
                rows = session.exec(
                    select(RuntimeConfig).where(RuntimeConfig.key.in_(normalized))
                ).all()
            for row in rows:
                param = RUNTIME_CONFIG_PARAMS[row.key]
                try:
                    values[row.key] = param.validator(row.value)
                    db_keys.add(row.key)
                except ValueError:
                    pass

        for key in normalized:
            if key in self._env_overrides and key not in db_keys:
                values[key] = self._env_overrides[key]
        return values


def format_runtime_value(value: int | float | str | bool) -> str:
    """Serialize runtime values consistently for storage and client payloads."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
