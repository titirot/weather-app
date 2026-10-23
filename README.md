# Weather application

## Azure infrastructure

The [Bicep templates and architecture/deployment guide](infra/README.md) describe
a European, zone-resilient PaaS environment with Front Door/WAF, App Service,
private PostgreSQL, managed identity, Key Vault, autoscale and monitoring.
The networking-only Azure foundation has been deployed; the application,
database and release runner have not. GitHub Actions remains disabled.
Local development is unchanged. See the [infrastructure status](infra/README.md#status-and-scope).

## GitHub Actions CI/CD

The [deployment workflow](.github/workflows/deploy.yml) runs CI on pull requests
and pushes to `main`, then deploys approved `main` releases using Azure OIDC.
Follow the [one-time GitHub/Azure setup and Secrets list](infra/README.md#github-actions-cicd)
before pushing: production requires a protected GitHub environment and a trusted
private-network release runner. Saving the workflow locally does not deploy anything.

## Local browser development

Configure `.env` using `.env.example`, including your own OpenWeather API key
and a local PostgreSQL password. Never share or commit `.env`.
Start the persistent local PostgreSQL database and the web server:

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe run_local.py
```

Open http://127.0.0.1:8765/ or run the VS Code task **Run local weather app**.
The local runner loads `.env` before creating the application. Restart it after
changing those settings. `AUTH_MODE=local` simulates a signed-in developer;
weather requests use the real OpenWeather API and saved forecasts use PostgreSQL.
Database data persists across server restarts in the Docker volume.
This does not deploy anything to Azure.

The daily forecast stays in one horizontal row (scrollable on small screens).
The hourly forecast supports touch, keyboard scrolling and navigation arrows;
dates and times are explicitly formatted in English. Privacy information and
the account-data deletion action are in the footer.

Selecting a saved city requests `/api/weather?city=...&refresh=true`, bypassing
the server's 10-minute cache and updating it with a fresh OpenWeather response.
Each refresh uses three provider calls and remains subject to the free-tier
quota. Provider failures are shown explicitly, not replaced with old snapshots.
Saved snapshots remain in PostgreSQL, but are no longer displayed by the UI.

## Original stage 1 API notes

A Flask backend for the weather application exercise. This stage does not deploy
Azure resources, create a UI, authenticate users, or connect to PostgreSQL.

## Run locally (PowerShell)

Requires Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:OPENWEATHER_API_KEY = "<your-own-openweather-key>"
.\.venv\Scripts\python.exe app.py
```

Set the environment variable before starting the process. Do not put a real key
in source files, screenshots, chat, or command history shared with others.
`.env.example` documents the variable; the application intentionally does not
automatically load `.env` files.

Open `http://127.0.0.1:5000/api/weather?city=London,GB` in your browser.
City names can contain Unicode. Add a country code (and optionally a US state
code) to disambiguate names, for example `Paris,FR` or `Portland,OR,US`.
The first matching geocoding result is used and its location is returned.

For a WSGI server compatible with Windows and Linux:

```powershell
.\.venv\Scripts\waitress-serve.exe --host=127.0.0.1 --port=8000 app:app
```

The built-in Flask server is for local development only. Azure deployment must
use a production WSGI server behind an HTTPS-terminating service, with the correct
binding for that hosting environment. Debug mode is disabled.

## API contract

`GET /api/weather?city=<city_name>`

Successful responses contain:

- `provider`: `OpenWeather`.
- `units`: `metric` (Celsius temperatures, metres/second wind speeds).
- `location`: resolved name, country, optional state, latitude and longitude.
- `current`: current weather payload, including `dt`, `main` and `weather`.
- `forecast.interval_hours`: `3`.
- `forecast.items`: forecast entries, including `dt`, `main` and `weather`.

`dt` is Unix time in UTC. The free 5-day forecast is in 3-hour intervals, not
five daily summaries. Coverage and entry count come from the provider.
The backend uses geocoding, Current Weather and 5 Day / 3 Hour Forecast,
not the separate One Call product. Verify the free plan's current entitlements
and quotas for your account before deployment.

Each successful request makes **three** provider calls. No automatic retries
are made, avoiding request multiplication against the free quota.
If any call fails, an explicit error is returned rather than partial success.

Errors have a consistent shape:

```json
{
  "error": {
    "code": "city_not_found",
    "message": "City not found."
  }
}
```

| HTTP status | Meaning |
| --- | --- |
| 400 | Missing, empty, repeated, overlong or control-character-containing city |
| 404 | City not found, or unknown application route |
| 405 | Unsupported HTTP method |
| 502 | Provider network failure, unexpected HTTP response or malformed data |
| 503 | Missing server key, provider authentication failure or provider quota |
| 504 | Provider timeout |

An upstream 429 becomes 503 because this is the server's shared provider quota,
not a limit assigned to the individual caller. There is no invented retry time:
the provider's quota reset depends on the account. Provider bodies, URLs,
credentials and exception details are not sent to the caller.

## Security and privacy boundaries

- The OpenWeather key is read only from a server environment variable.
- Only fixed HTTPS provider endpoints are contacted; TLS verification remains on.
  User input is encoded as a query parameter, never used as an outbound URL.
- Redirects are disabled and each provider request has connect/read timeouts.
- Application warnings log fixed error codes, not city queries, credentials or
  raw request exceptions. No user names, accounts or preferences are stored.
- Responses disable caching, MIME sniffing, referring URLs and framing.
- No cross-origin access is enabled. A same-origin frontend can be added later.
- A city search and its coordinates are sent to OpenWeather. This is **not**
  absolute anonymity: the provider sees the search and the server's public IP.
  The city is also in the browser URL/history. Hosting/proxy access logs may
  contain query strings or client IPs; their redaction, retention and access
  controls must be configured during deployment. Flask's local development
  access log can also include the query string.

This endpoint is not ready for unrestricted public production traffic:
authentication, per-user/edge rate limiting and shared weather caching are
future deployment requirements to prevent abuse and protect the shared quota.
There is no availability SLA, failover, database audit trail or regional
deployment at this stage. A regional design and a current cost comparison are
still needed; no claim about the cheapest European region is made here.

Later stages must add identity-based authorization for saved forecasts,
PostgreSQL with private connectivity, secret management (for example Azure Key
Vault references with managed identity), HTTPS/WAF, privacy-aware monitoring,
backups, high availability and deployment automation. These are not provided
by application headers alone.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests mock external calls: they require neither credentials nor internet access,
and cover successful responses, Unicode, invalid cities, quotas, missing keys,
provider errors, malformed data, timeouts, error-log redaction and headers.
A real account/key is required for a separate live provider integration check.

## References

- https://openweathermap.org/api/geocoding-api
- https://openweathermap.org/current
- https://openweathermap.org/forecast5
