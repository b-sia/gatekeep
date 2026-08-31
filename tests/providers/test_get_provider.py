import pytest

from gatekeep.app import _build_providers, get_provider
from gatekeep.config import Settings
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.google import GoogleProvider
from gatekeep.providers.ollama import OllamaProvider
from gatekeep.providers.openai import OpenAIProvider
from gatekeep.providers.stub import StubProvider


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("anthropic", AnthropicProvider),
        ("ollama", OllamaProvider),
        ("openai", OpenAIProvider),
        ("google", GoogleProvider),
    ],
)
def test_get_provider_returns_correct_instance(name, cls):
    assert isinstance(get_provider(name), cls)


@pytest.mark.parametrize("name", ["anthropic", "ollama", "openai", "google"])
def test_get_provider_caches_one_instance_per_name(name):
    assert get_provider(name) is get_provider(name)


def _settings(**overrides):
    return Settings(database_url="x", redis_url="y", anthropic_api_key="z", **overrides)


def test_build_providers_excludes_stub_by_default():
    providers = _build_providers(_settings())
    assert "stub" not in providers


def test_build_providers_includes_stub_when_flag_enabled():
    providers = _build_providers(_settings(loadtest_stub_enabled=True))
    assert isinstance(providers["stub"], StubProvider)
