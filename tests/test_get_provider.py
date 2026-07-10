from gatekeep.app import get_provider
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.ollama import OllamaProvider


def test_get_provider_returns_anthropic_instance():
    provider = get_provider("anthropic")
    assert isinstance(provider, AnthropicProvider)


def test_get_provider_returns_ollama_instance():
    provider = get_provider("ollama")
    assert isinstance(provider, OllamaProvider)


def test_get_provider_returns_same_instance_across_calls():
    assert get_provider("anthropic") is get_provider("anthropic")
    assert get_provider("ollama") is get_provider("ollama")
