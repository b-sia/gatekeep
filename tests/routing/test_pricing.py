from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gatekeep.routing.pricing import (
    ModelPrice,
    PricingIntegrityError,
    PricingTable,
    compute_models_digest,
    get_pricing_table,
    is_billed_provider,
    is_unpriced,
    transform_litellm,
)

# The models `accounting.calculate_cost` used to hardcode in MODEL_PRICING
# before the cutover to the vendored JSON table, with their known-good prices
# (issue #25). These are now the "local" entries in model_prices.json.
_CURRENT_MODELS: dict[str, tuple[str, float, float]] = {
    "claude-sonnet-5": ("anthropic", 2.0, 10.0),
    "claude-haiku-4-5-20251001": ("anthropic", 1.0, 5.0),
    "gpt-4o": ("openai", 2.5, 10.0),
    "gpt-4o-mini": ("openai", 0.15, 0.6),
    "gemini-2.5-pro": ("google", 1.25, 10.0),
    "gemini-flash-latest": ("google", 1.5, 9.0),
}


# --- vendored baseline coverage -----------------------------------------------


@pytest.mark.parametrize(("model", "expected"), _CURRENT_MODELS.items())
def test_baseline_covers_every_current_model_with_matching_price(model, expected):
    """The vendored baseline prices every model the deployment actually
    serves, at the value the old hardcoded table used - so the cutover in
    accounting.calculate_cost is a no-op for existing models."""
    provider, input_price, output_price = expected
    table = PricingTable.load()
    price = table.lookup(provider, model)
    assert price is not None, f"vendored baseline is missing {provider}/{model}"
    assert price.input_per_1m == input_price
    assert price.output_per_1m == output_price


def test_current_models_are_hand_maintained_local_entries():
    """The invented/in-use models must be 'local' so a LiteLLM refresh (which
    does not know them) can never drop or overwrite them."""
    table = PricingTable.load()
    for model, (provider, _, _) in _CURRENT_MODELS.items():
        assert table.lookup(provider, model).source == "local"


# --- billed-provider / unpriced classification --------------------------------


def test_is_billed_provider():
    assert is_billed_provider("anthropic")
    assert is_billed_provider("openai")
    assert is_billed_provider("google")
    assert not is_billed_provider("ollama")


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("anthropic", "not-a-real-model", True),  # paid provider miss
        ("anthropic", "claude-sonnet-5", False),  # priced model
        # a self-hosted Ollama model is never billed, so an absent price is
        # expected, not a governance gap.
        ("ollama", "llama3-totally-local", False),
    ],
)
def test_is_unpriced(provider, model, expected):
    assert is_unpriced(provider, model) is expected


# --- lookup semantics --------------------------------------------------------


def test_lookup_miss_returns_none_not_zero():
    table = PricingTable({"openai/gpt-4o": ModelPrice(2.5, 10.0, "local")})
    assert table.lookup("openai", "gpt-4o") is not None
    assert table.lookup("openai", "not-a-real-model") is None


def test_lookup_is_provider_scoped():
    """Same bare model id under two providers resolves independently."""
    table = PricingTable(
        {
            "openai/shared": ModelPrice(1.0, 2.0, "local"),
            "anthropic/shared": ModelPrice(3.0, 4.0, "local"),
        }
    )
    assert table.lookup("openai", "shared").input_per_1m == 1.0
    assert table.lookup("anthropic", "shared").input_per_1m == 3.0


def test_model_price_cost_is_per_million():
    price = ModelPrice(input_per_1m=2.0, output_per_1m=10.0, source="local")
    assert price.cost(1_000_000, 1_000_000) == 12.0
    assert price.cost(500_000, 0) == 1.0


# --- override overlay --------------------------------------------------------


def test_overrides_win_over_baseline():
    baseline_key = "openai/gpt-4o"
    table = PricingTable.load(overrides={baseline_key: ModelPrice(99.0, 99.0, "override")})
    price = table.lookup("openai", "gpt-4o")
    assert price.input_per_1m == 99.0
    assert price.source == "override"


def test_overrides_can_add_a_new_model():
    table = PricingTable.load(overrides={"openai/ft-custom": ModelPrice(1.0, 2.0, "override")})
    assert table.lookup("openai", "ft-custom").input_per_1m == 1.0


def test_get_pricing_table_uses_overrides_path(tmp_path, monkeypatch):
    override_file = tmp_path / "overrides.json"
    override_file.write_text(
        json.dumps({"models": {"openai/gpt-4o": {"input_per_1m": 42.0, "output_per_1m": 43.0}}})
    )
    monkeypatch.setenv("PRICING_OVERRIDES_PATH", str(override_file))
    from gatekeep.config import get_settings

    get_settings.cache_clear()
    get_pricing_table.cache_clear()
    try:
        price = get_pricing_table().lookup("openai", "gpt-4o")
        assert price.input_per_1m == 42.0
        assert price.source == "override"  # forced regardless of file contents
    finally:
        get_settings.cache_clear()
        get_pricing_table.cache_clear()


# --- LiteLLM transform (pure) ------------------------------------------------


def test_transform_converts_per_token_to_per_million_and_rekeys():
    raw = {
        "gpt-4o": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 2.5e-06,
            "output_cost_per_token": 1e-05,
        }
    }
    entries = transform_litellm(raw)
    assert entries == {
        "openai/gpt-4o": {"input_per_1m": 2.5, "output_per_1m": 10.0, "source": "litellm"}
    }


def test_transform_strips_provider_prefix_from_key():
    raw = {
        "gemini/gemini-2.5-pro": {
            "litellm_provider": "gemini",
            "mode": "chat",
            "input_cost_per_token": 1.25e-06,
            "output_cost_per_token": 1e-05,
        }
    }
    assert "google/gemini-2.5-pro" in transform_litellm(raw)


def test_transform_skips_unmapped_providers_and_non_chat_and_priceless():
    raw = {
        "sample_spec": {"litellm_provider": "openai", "mode": "chat"},
        "bedrock-claude": {  # unmapped provider (re-host)
            "litellm_provider": "bedrock",
            "mode": "chat",
            "input_cost_per_token": 3e-06,
            "output_cost_per_token": 1.5e-05,
        },
        "whisper": {  # not a chat model
            "litellm_provider": "openai",
            "mode": "audio_transcription",
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 1e-06,
        },
        "free-thing": {  # missing prices
            "litellm_provider": "openai",
            "mode": "chat",
        },
    }
    assert transform_litellm(raw) == {}


# --- vendored file integrity -------------------------------------------------


def test_vendored_file_is_well_formed():
    path = Path("gatekeep/data/model_prices.json")
    data = json.loads(path.read_text())
    assert data["models"], "vendored file has no models"
    for key, entry in data["models"].items():
        assert key.count("/") >= 1, f"key {key!r} is not '<provider>/<model>'"
        assert isinstance(entry["input_per_1m"], int | float)
        assert isinstance(entry["output_per_1m"], int | float)
        assert entry["source"] in {"local", "litellm"}


# --- hash pin (integrity of the enforcement table) ---------------------------


def _write_pinned(tmp_path, models, *, pin=None):
    """Write a baseline JSON + sibling .sha256 pin, returning the baseline path.

    `pin` defaults to the correct digest; pass a wrong string to simulate a
    baseline changed without re-pinning.
    """
    base = tmp_path / "model_prices.json"
    base.write_text(json.dumps({"_meta": {"generated_at": "x"}, "models": models}))
    pin_file = tmp_path / "model_prices.sha256"
    pin_file.write_text((pin if pin is not None else compute_models_digest(models)) + "\n")
    return base


def test_vendored_baseline_matches_its_committed_pin():
    """The real shipped file loads - proving the committed pin is present and
    correct, so production never trips the fail-closed integrity check."""
    data = json.loads(Path("gatekeep/data/model_prices.json").read_text())
    pin = Path("gatekeep/data/model_prices.sha256").read_text().strip()
    assert compute_models_digest(data["models"]) == pin
    assert PricingTable.load() is not None  # default baseline path, pin enforced


def test_digest_ignores_meta_and_key_order():
    """The pin tracks price content only: reordering keys or bumping the
    generated_at stamp must not change it, but changing a price must."""
    a = {"openai/x": {"input_per_1m": 1.0, "output_per_1m": 2.0, "source": "local"}}
    b = {"openai/x": {"source": "local", "output_per_1m": 2.0, "input_per_1m": 1.0}}
    assert compute_models_digest(a) == compute_models_digest(b)
    c = {"openai/x": {"input_per_1m": 9.0, "output_per_1m": 2.0, "source": "local"}}
    assert compute_models_digest(a) != compute_models_digest(c)


def test_load_accepts_a_matching_pin(tmp_path):
    models = {"openai/x": {"input_per_1m": 1.0, "output_per_1m": 2.0, "source": "local"}}
    base = _write_pinned(tmp_path, models)
    table = PricingTable.load(baseline_path=base)
    assert table.lookup("openai", "x").input_per_1m == 1.0


def test_load_rejects_a_baseline_that_does_not_match_its_pin(tmp_path):
    """A price changed without re-pinning must fail closed, not silently
    redefine spend enforcement (issue #25)."""
    models = {"openai/x": {"input_per_1m": 1.0, "output_per_1m": 2.0, "source": "local"}}
    base = _write_pinned(tmp_path, models, pin="0" * 64)
    with pytest.raises(PricingIntegrityError):
        PricingTable.load(baseline_path=base)


async def test_lifespan_fails_startup_when_pricing_table_is_unloadable(monkeypatch):
    """The pricing table is warmed eagerly in _lifespan (alongside the
    embedding model) so a corrupted baseline or stale pin fails the container
    at startup - loud and before it takes traffic - instead of surfacing as a
    generic 500 on whichever request happens to call enforce_pricing_policy/
    calculate_cost first."""
    import gatekeep.app as app_module

    def _boom():
        raise PricingIntegrityError("simulated corrupt baseline")

    monkeypatch.setattr(app_module, "get_pricing_table", _boom)
    monkeypatch.setattr(app_module, "warm_embedding_model", lambda: None)

    with pytest.raises(PricingIntegrityError):
        async with app_module._lifespan(app_module.app):
            pytest.fail("lifespan should not have completed startup")


async def test_lifespan_starts_and_cleanly_stops_budget_reconciliation(monkeypatch):
    """_lifespan also starts the budget-reconciliation background task
    (issue #27) and must cancel it cleanly on shutdown rather than leaking
    it or letting CancelledError escape."""
    import gatekeep.app as app_module
    from gatekeep.middleware import budget as budget_module

    monkeypatch.setattr(app_module, "warm_embedding_model", lambda: None)
    monkeypatch.setattr(app_module, "get_pricing_table", lambda: None)

    calls = []

    async def _fake_reconcile(session_arg, redis_arg, *, now=None):
        calls.append(1)
        return {}

    monkeypatch.setattr(budget_module, "reconcile_period_spend", _fake_reconcile)

    async with app_module._lifespan(app_module.app):
        await asyncio.sleep(0.05)

    assert calls == [1]


def test_load_allows_an_unpinned_non_default_baseline(tmp_path):
    """Only the shipped baseline requires a pin; an ad-hoc file without a
    sibling lockfile still loads, so test/operator fixtures need not ship one."""
    base = tmp_path / "model_prices.json"
    base.write_text(
        json.dumps(
            {"models": {"openai/x": {"input_per_1m": 1.0, "output_per_1m": 2.0, "source": "local"}}}
        )
    )
    assert PricingTable.load(baseline_path=base).lookup("openai", "x") is not None
