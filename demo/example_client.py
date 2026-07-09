"""
Example: How to use Gatekeep in your Python application.

This file shows several patterns for integrating Gatekeep into your code.
Pick the one that best fits your use case.
"""

import asyncio
import os

import httpx


# Configuration
GATEKEEP_URL = os.getenv("GATEKEEP_URL", "http://localhost:8100")
API_KEY = os.getenv("GATEKEEP_API_KEY", "sk-test-key")
DEFAULT_MODEL = "claude-sonnet-5"


# ============================================================================
# Pattern 1: Using httpx directly (simple, no dependencies)
# ============================================================================


async def example_1_basic_request():
    """Basic non-streaming request using httpx."""
    print("\n=== Pattern 1: Basic Request ===")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GATEKEEP_URL}/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "What is Gatekeep?"}],
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        )

        if response.status_code == 200:
            data = response.json()
            print(data["choices"][0]["message"]["content"])
        else:
            print(f"Error: {response.status_code}")


# ============================================================================
# Pattern 2: Streaming responses for real-time updates
# ============================================================================


async def example_2_streaming():
    """Streaming response for real-time token delivery."""
    print("\n=== Pattern 2: Streaming Response ===")

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"{GATEKEEP_URL}/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": "Tell me a short poem about AI",
                    }
                ],
                "stream": True,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        import json

                        data = json.loads(line[6:])
                        delta = data["choices"][0]["delta"].get("content", "")
                        print(delta, end="", flush=True)
                    except (json.JSONDecodeError, KeyError):
                        pass
    print()  # Newline at end


# ============================================================================
# Pattern 3: Using the OpenAI client library (recommended for projects)
# ============================================================================


async def example_3_openai_client():
    """Using the official OpenAI client as a drop-in replacement.

    This is the recommended approach if you're already using the openai package.
    """
    print("\n=== Pattern 3: OpenAI Client Library ===")

    try:
        from openai import AsyncOpenAI
    except ImportError:
        print(
            "OpenAI client not installed. Install with: pip install openai"
        )
        return

    # Create client pointing to Gatekeep instead of OpenAI
    client = AsyncOpenAI(api_key=API_KEY, base_url=f"{GATEKEEP_URL}/v1")

    response = await client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[{"role": "user", "content": "List 3 benefits of using a gateway"}],
    )

    print(response.choices[0].message.content)


# ============================================================================
# Pattern 4: Error handling and retries
# ============================================================================


async def example_4_with_error_handling():
    """Demonstrates proper error handling and retry logic."""
    print("\n=== Pattern 4: Error Handling ===")

    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{GATEKEEP_URL}/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [
                            {"role": "user", "content": "Hello!"}
                        ],
                    },
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=30.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    print("Success:", data["choices"][0]["message"]["content"])
                    break

                elif response.status_code == 429:  # Rate limited
                    if attempt < max_retries - 1:
                        print(f"Rate limited, retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        print("Max retries reached")

                else:
                    error_data = response.json()
                    print(f"API Error: {error_data.get('error', {}).get('message')}")
                    break

        except httpx.ConnectError:
            print("Connection error - Gatekeep server may be down")
            break
        except httpx.TimeoutException:
            print("Request timed out")
            break


# ============================================================================
# Pattern 5: Conversation history (multi-turn)
# ============================================================================


async def example_5_conversation():
    """Demonstrates maintaining conversation state across multiple turns."""
    print("\n=== Pattern 5: Multi-Turn Conversation ===")

    messages = [
        {"role": "system", "content": "You are a helpful AI assistant."},
    ]

    # Turn 1
    messages.append({"role": "user", "content": "What is the capital of France?"})
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GATEKEEP_URL}/v1/chat/completions",
            json={"model": DEFAULT_MODEL, "messages": messages},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assistant_msg = response.json()["choices"][0]["message"]["content"]
        messages.append({"role": "assistant", "content": assistant_msg})
        print(f"AI: {assistant_msg}")

    # Turn 2 - context is preserved
    messages.append({"role": "user", "content": "How far is it from Paris?"})
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GATEKEEP_URL}/v1/chat/completions",
            json={"model": DEFAULT_MODEL, "messages": messages},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assistant_msg = response.json()["choices"][0]["message"]["content"]
        print(f"AI: {assistant_msg}")


# ============================================================================
# Pattern 6: Switching between providers
# ============================================================================


async def example_6_provider_switching():
    """Demonstrates using different providers through Gatekeep."""
    print("\n=== Pattern 6: Provider Switching ===")

    models = [
        ("claude-sonnet-5", "Anthropic's Claude Sonnet"),
        ("claude-opus-4-8", "Anthropic's Claude Opus"),
        ("ollama-llama3.2", "Local Ollama - Llama 3.2"),
    ]

    for model, description in models:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{GATEKEEP_URL}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "user", "content": "Say 'Hello from [model]!'"}
                        ],
                    },
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=10.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    print(f"✅ {description}: {content}")
                else:
                    print(f"❌ {description}: Not available")

        except Exception as e:
            print(f"❌ {description}: {str(e)}")


# ============================================================================
# Main - Run examples
# ============================================================================


async def main():
    """Run all examples."""
    print("🚀 Gatekeep Client Examples")
    print("=" * 50)

    # Note: Modify these calls based on which examples you want to run
    await example_1_basic_request()
    await example_2_streaming()
    await example_3_openai_client()
    await example_4_with_error_handling()
    await example_5_conversation()
    # await example_6_provider_switching()  # Uncomment to test all models

    print("\n" + "=" * 50)
    print("✅ Examples complete!")


if __name__ == "__main__":
    asyncio.run(main())
