from gatekeep.app import get_provider
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.google import GoogleProvider
from gatekeep.providers.ollama import OllamaProvider
from gatekeep.providers.openai import OpenAIProvider


def test_get_provider_returns_anthropic_instance():
    provider = get_provider("anthropic")
    assert isinstance(provider, AnthropicProvider)


def test_get_provider_returns_ollama_instance():
    provider = get_provider("ollama")
    assert isinstance(provider, OllamaProvider)


def test_get_provider_returns_same_instance_across_calls():
    assert get_provider("anthropic") is get_provider("anthropic")
    assert get_provider("ollama") is get_provider("ollama")


def test_get_provider_returns_openai_instance():
    provider = get_provider("openai")
    assert isinstance(provider, OpenAIProvider)


def test_get_provider_returns_google_instance():
    provider = get_provider("google")
    assert isinstance(provider, GoogleProvider)


def test_get_provider_returns_same_instance_across_calls_for_new_providers():
    assert get_provider("openai") is get_provider("openai")
    assert get_provider("google") is get_provider("google")
