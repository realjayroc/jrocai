# jrocai

jrocai is a small, runnable AI hub: a workflow bridge dispatches work to named
agents, an omni-router selects an available model provider, and SQLite records
each workflow run locally.

## Requirements and launch

- Python 3.10 or later
- No third-party Python packages

On Windows 11, double-click `run_app.bat` to start the interactive prompt, or
`run_tests.bat` to run the automated tests. At the prompt, enter a task, `manifest`
to inspect configured providers and workflows, or `exit` to quit. The bridge can
also be called from Python:

```python
from jroc_bridge import dispatch_workflow, get_manifest

manifest = get_manifest()
result = dispatch_workflow("main_workflow", {"task": "Summarize this release plan"})
```

The workflow accepts `task`, and optional `agent`, `system_prompt`, and `provider`
fields. The built-in agents are `general` and `planner`. A requested provider is
tried first; configured alternatives remain available as fallbacks.

## Model providers

The router prefers providers in this order unless `JROCAI_PROVIDERS` specifies
another order: OpenRouter, Gemini, Groq, GitHub Models, then local Ollama. Provider
credentials are read from environment variables, never from source files:

| Provider | Credential | Default model |
| --- | --- | --- |
| OpenRouter | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct:free` |
| Google AI Studio | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | `gemini-2.5-flash` |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| GitHub Models | `GITHUB_TOKEN` | `openai/gpt-4o-mini` |
| Ollama | None (local service) | `llama3.2` |

Model names can be changed with `OPENROUTER_MODEL`, `GEMINI_MODEL`, `GROQ_MODEL`,
`GITHUB_MODELS_MODEL`, and `OLLAMA_MODEL`. Ollama's OpenAI-compatible endpoint
defaults to `http://localhost:11434/v1` and can be changed with
`OLLAMA_BASE_URL`. Set `JROCAI_PROVIDERS` to a comma-separated priority list such
as `groq,openrouter,ollama`. The router skips providers whose required credential
is missing and automatically proceeds to the next provider after a provider
error, including HTTP 429 rate limits.

For the model switcher, set a provider's environment variables before launching
`switch_model.bat`. It sets the chosen provider as the first choice for that
launched process; it does not modify persistent shell or machine settings.

## Local state and interface

Workflow, product, inventory, sale, repair, asset, pricing, license, activation,
and order-fulfillment records share the `jroc_core.db` SQLite database. Override
the path with `JROCAI_DB_PATH`; the database and schema are initialized on first
use. The database is local application state and is ignored by Git.

`jroc_bridge.get_manifest()` provides a JSON-serializable contract-v1 manifest;
`workflows` is a list of unique workflow names, currently `["main_workflow"]`.
`dispatch_workflow(workflow, payload)` validates and executes an advertised
workflow, returning a JSON-serializable result and propagating execution errors.
`main_workflow` requires a JSON object with a non-empty `task` string and accepts
optional `agent` (`general` or `planner`), `system_prompt`, and preferred
`provider` strings.

## Business operations and licensing

`business_command.py` manages the product catalog, stock, sales, repair tickets,
drama-series asset records, and bounded price adjustments. A product can set
minimum and maximum prices; `price` uses a 0-100 demand score plus a stock
scarcity/abundance adjustment, clamps the result to those bounds, and records
each applied price change. For example:

```text
python business_command.py product-add --sku APP-1 --name "Desktop App" --category digital_app --price-cents 2500 --min-price-cents 2000 --max-price-cents 3500
python business_command.py price --sku APP-1 --demand-score 75
python business_command.py tickets
python business_command.py revenue
```

Licenses are HMAC-SHA256 signed `JRL1` tokens. `LicenseEngine.validate_license`
checks signature and expiry offline; online validation additionally checks the
revocation registry and, when supplied, machine activation. Online activation
uses a keyed fingerprint instead of storing a raw machine identifier. Offline
validation cannot observe revocation or activation counts. Keep
`JROCAI_LICENSE_SECRET` stable (at least 32 bytes) to preserve existing licenses.

The order webhook accepts only authenticated `paid` events, signed as
`sha256=<hex HMAC-SHA256>` in `X-Jroc-Signature` using `JROCAI_WEBHOOK_SECRET`
(at least 32 bytes). Its JSON object has exactly these fields:
`event_id`, `order_id`, `customer_id`, `status`, `currency`, `amount_cents`, and
`items`; each item has exactly `sku`, `quantity`, and `unit_price_cents`.
Line totals, product currencies, stock, and license requirements are validated
inside one SQLite transaction. Repeating the same signed event returns the
original fulfillment; reusing an event or order ID with changed content is
rejected. This is a signed-event interface, not a payment processor integration.

## Ecosystem hub

Double-click `launch_hub.bat` or run `python ecosystem_hub.py`. The CLI can list
bridge repositories, dispatch an advertised workflow, inspect business records,
or start the API. Sibling repositories are discovered from `JROC_REPO_PATHS`
(semicolon-separated paths on Windows) and `JROC_REPO_ROOTS` (roots whose
immediate children are scanned); jrocai itself is always included. Repositories
must expose a valid bridge-v1 manifest. Configured sibling bridge scripts run in
an isolated Python subprocess with a five-second manifest timeout, a 45-second
dispatch timeout, and bounded output. Configure only repositories whose code
you trust. Child processes receive a sanitized environment; pass specific
variable names through `JROC_HUB_FORWARD_ENV` only when needed.

The API binds exclusively to `127.0.0.1`. It requires
`Authorization: Bearer <token>` on every API route, rejects non-JSON or oversized
bodies, duplicate JSON fields, unknown fields, and query parameters, and limits
each request socket to ten seconds, request bodies to 64 KiB, and responses to
2 MiB. Set `JROCAI_HUB_API_TOKEN` to a random
32-character-or-longer token; if unset, the server generates and prints an
ephemeral token at startup.

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/workflows` | List discovered repositories and advertised workflows |
| `POST /api/v1/workflows/dispatch` | Dispatch `{"repo":"...","workflow":"...","payload":{...}}` |
| `POST /api/v1/agents/dispatch` | Dispatch `{"repo":"...","agent":"...","payload":{...}}` through the repository's advertised `main_workflow` |
| `GET /api/v1/business/products` | Read active products |
| `POST /api/v1/business/products` | Create a product |
| `GET /api/v1/business/sales` | Read recent sales |
| `GET /api/v1/business/revenue` | Summarize sales totals, units, and orders by currency |
| `POST /api/v1/business/inventory` | Adjust tracked stock with `sku` and non-zero `delta` |
| `GET, POST /api/v1/business/repair-tickets` | List or create repair tickets |
| `POST /api/v1/business/repair-tickets/update` | Advance a ticket status |
| `GET, POST /api/v1/business/drama-assets` | List or register drama assets |
| `POST /api/v1/business/pricing` | Apply a bounded dynamic price |
| `POST /api/v1/licenses/validate` | Offline or online license validation |
| `POST /api/v1/licenses/activate` | Activate a license for a machine |
| `POST /api/v1/licenses/revoke` | Revoke a registered license |
| `POST /api/v1/webhooks/orders` | Process a signed order event; also requires the webhook signature header |

Run `python ecosystem_hub.py --list` to inspect repositories,
`python ecosystem_hub.py --agent <repo> <agent> --input '{"task":"..."}'` to run
an advertised agent, or
`python ecosystem_hub.py --serve --port 8765` to start the API. Copy
`.env.example` to `.env` for local configuration; the dependency-free loader only
sets variables not already present in the process environment. `.env` is ignored
by Git, and the example intentionally contains no secret values.

## Tests

Run `python -m unittest discover -s tests -v` or double-click `run_tests.bat`.
