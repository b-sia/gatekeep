import pytest

from gatekeep.app import get_provider
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.google import GoogleProvider
from gatekeep.providers.ollama import OllamaProvider
from gatekeep.providers.openai import OpenAIProvider


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
