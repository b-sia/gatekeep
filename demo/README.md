# Demo chat app

A small standalone chat web app that exercises Gatekeep the way a real client
would, rather than as a single curl call. Useful for sanity-checking a local
gateway and for seeing the integration patterns in context.

## Running it

The gateway must already be up (`docker-compose up -d`) and you need a key from
`scripts/init-test-key.sh`.

```bash
export GATEKEEP_API_KEY=gk-your-key
python demo/app.py
```

Open `http://localhost:8200` and chat. Use the model dropdown to switch between
providers and the toggle to turn streaming on and off. The page's "How this works"
section walks through the same integration examples as `example_client.py`.

`scripts/run-demo.sh` wraps the same steps.

## Configuration

Read from the environment, and from `.env` if present:

| Variable | Default | Purpose |
|---|---|---|
| `GATEKEEP_URL` | `http://localhost:8100` | Address of the gateway |
| `GATEKEEP_API_KEY` | none (required) | Key created via `init-test-key.sh` |
| `DEFAULT_MODEL` | `claude-sonnet-5` | Model used when none is specified |

## Files

| File | Contents |
|---|---|
| `app.py` | The web app: serves the UI and proxies chat requests to the gateway |
| `example_client.py` | Runnable, standalone integration patterns - basic request, streaming, retries, multi-turn, provider switching |
| `static/` | Frontend assets for the chat UI |
