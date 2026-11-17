"""``KnowledgeBaseSettings`` : défauts Proposed du §7 et §8.4, validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge_base import KnowledgeBaseSettings


def make(**overrides: float) -> KnowledgeBaseSettings:
    return KnowledgeBaseSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_settings_defaults_match_the_proposed_values() -> None:
    settings = make()

    assert settings.work_backoff_base_seconds == 10
    assert settings.work_backoff_max_seconds == 900
    assert settings.work_lease_ttl_seconds == 300
    assert settings.work_time_limit_seconds == 3600
    assert settings.work_dispatch_interval_seconds == 2
    assert settings.work_dispatch_batch == 50
    assert settings.work_redispatch_after_seconds == 600
    assert settings.work_max_in_flight == 2
    assert settings.work_recover_interval_seconds == 30


def test_settings_heartbeat_defaults_to_a_third_of_the_lease() -> None:
    assert make().heartbeat_interval_seconds == 100
    assert make(work_lease_ttl_seconds=30).heartbeat_interval_seconds == 10


def test_settings_explicit_heartbeat_overrides_the_default() -> None:
    settings = make(work_heartbeat_interval_seconds=60)
    assert settings.heartbeat_interval_seconds == 60


def test_settings_are_read_from_kb_prefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_WORK_LEASE_TTL_SECONDS", "42")
    monkeypatch.setenv("KB_WORK_DISPATCH_BATCH", "7")
    monkeypatch.setenv("KB_WORK_MAX_IN_FLIGHT", "1")

    settings = make()

    assert settings.work_lease_ttl_seconds == 42
    assert settings.work_dispatch_batch == 7
    assert settings.work_max_in_flight == 1


def test_settings_ignore_unprefixed_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_LEASE_TTL_SECONDS", "42")
    assert make().work_lease_ttl_seconds == 300


@pytest.mark.parametrize(
    "field",
    [
        "work_backoff_base_seconds",
        "work_backoff_max_seconds",
        "work_lease_ttl_seconds",
        "work_time_limit_seconds",
        "work_dispatch_interval_seconds",
        "work_dispatch_batch",
        "work_redispatch_after_seconds",
        "work_max_in_flight",
        "work_recover_interval_seconds",
        "work_heartbeat_interval_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_settings_reject_values_that_are_not_strictly_positive(
    field: str, value: float
) -> None:
    with pytest.raises(ValidationError, match=field):
        make(**{field: value})


def test_settings_reject_a_heartbeat_equal_to_the_lease_ttl() -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        make(work_lease_ttl_seconds=30, work_heartbeat_interval_seconds=30)


def test_settings_reject_a_heartbeat_longer_than_the_lease_ttl() -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        make(work_lease_ttl_seconds=30, work_heartbeat_interval_seconds=31)


def test_settings_default_heartbeat_stays_coherent_with_a_short_lease() -> None:
    # Le défaut dérivé (lease / 3) reste valide quelle que soit la lease.
    settings = make(work_lease_ttl_seconds=0.3)
    assert settings.heartbeat_interval_seconds == pytest.approx(0.1)
