# Providers and model routing

Each file here adapts one upstream API to the common interface in `base.py`, so
`app.py` sees the same request/response shape regardless of which provider serves
a request.

| Adapter | Upstream |
|---|---|
| `anthropic.py` | Anthropic Claude |
| `openai.py` | OpenAI |
| `google.py` | Google Gemini |
| `ollama.py` | Local Ollama models |

## How a model name is resolved

`resolve_route` in `gatekeep/api/translation.py` maps a client-supplied model name
to a provider, in this order:

1. An `openai/` or `google/` prefix routes to that provider, prefix stripped.
2. A name in the `model_aliases` table (`gatekeep/config.py`, overridable via
   `MODEL_ALIASES`) routes to Anthropic under its aliased Claude model - this is
   how `gpt-4` and friends work with no OpenAI key.
3. A name starting with `claude-` routes to Anthropic as-is.
4. Anything else routes to Ollama under that name.

Step 4 means an unrecognised model is handed to Ollama rather than rejected, which
keeps the zero-config demo working but surfaces typos as an Ollama error.

To reach the real OpenAI or Google API instead, prefix the model:

```json
{"model": "openai/gpt-4o", "messages": [...]}
{"model": "google/gemini-flash-latest", "messages": [...]}
```

Prefixed requests require `OPENAI_API_KEY` / `GOOGLE_API_KEY`. With no key
configured they fail with an upstream auth error.

## Cost-based routing (opt-in)

Send `"route_by_cost": true` - optionally with `"quality_floor": 0.9` - alongside
`"prompt_name"`, and the gateway substitutes the cheapest model with a passing eval
run at or above the floor for that prompt. It never overrides an explicit model
choice unless you opt in, and never routes *up* to a costlier model. The
substitution is recorded in `request_logs.routed_from`.

Known limitation: streaming requests are routed the same way but do not record
`routed_from`.

## Native Anthropic endpoint: `POST /v1/messages`

Clients using the `anthropic` SDK directly, rather than an OpenAI-compatible
client, can point `base_url` at Gatekeep and use `POST /v1/messages` with the real
Anthropic Messages API request/response shape - no OpenAI translation involved.

It shares auth, rate limiting, budgets, the tiered cache, the `prompt_name` /
`route_by_cost` extensions, and cost accounting with `/v1/chat/completions`. A
response cached by either endpoint can be served by the other.

Known limitation: the internal `CompletionResult` carries only OpenAI-canonical
stop reasons, so an Anthropic `stop_sequence` hit and a plain `end_turn` are
indistinguishable at this layer - both are reported as `end_turn`.

## Adding a provider

Implement `base.py`'s interface, add the adapter to the `_PROVIDERS` registry in
`gatekeep/app.py`, teach `resolve_route` how to reach it, and add its per-token
pricing to `gatekeep/data/model_prices.json` (or map it in
`pricing._LITELLM_PROVIDER_MAP` if it's a LiteLLM-covered provider) so cost
accounting and cost-based routing stay correct.
