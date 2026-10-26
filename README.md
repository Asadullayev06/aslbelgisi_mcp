# aslbelgisi-mcp

A small, **standalone** MCP server (SSE over HTTP, bearer-auth) that lets an AI
agent query the **ASL Belgisi / xTrace** track-and-trace API. No dependency on
the TracePro platform — its own tiny ASL client, credentials from env.

## Tools (6)

| Tool | Answers |
|---|---|
| `gtin_ostatok(gtin, …)` | **how much of a product remains** — stock summary by status/owner/series |
| `km_info(codes[])` | full KM details: owner, product, status, aggregation, dates (≤50) |
| `km_owner(code)` | quick **current owner** of one KM code |
| `km_owners_table(codes[])` | owners of many codes as **ready-to-save CSV** + rows — for "Excel in → Excel out" (≤300) |
| `sscc_contents(sscc)` | child KM codes inside a box + ownership verdict |
| `mod_list()` | the company's MOD / business-place list |

## Credentials — per call or env default

Every tool accepts optional **`inn`** and **`api_key`** arguments. The agent
passes them on each call, so a **different company or a rotated key works
instantly — no server env change, no restart**. If omitted, the `ASL_INN` /
`ASL_API_KEY` env values are used as a fallback default.

(Note: the `api_key` then flows through the agent, so only give it to an agent
you trust. For a personal agent this is the intended, flexible mode.)

## Config (env)

| Var | Purpose |
|---|---|
| `MCP_AUTH_TOKEN` | **required** — bearer token Hermes must send |
| `ASL_INN` | *optional* default company INN (if not passed per call) |
| `ASL_API_KEY` | *optional* default API key (if not passed per call) |
| `ASL_API_BASE` | ASL base URL (default `https://xtrace.aslbelgisi.uz` — valid cert) |
| `MCP_HOST` / `MCP_PORT` | bind (default `0.0.0.0` / `8900`) |

## Run

```bash
pip install -r requirements.txt
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
# optional defaults (callers can also pass inn/api_key per tool call):
# export ASL_INN=900071021
# export ASL_API_KEY=xxxxxxxx-xxxx-....
python server.py
```

Or Docker:

```bash
docker build -t aslbelgisi-mcp .
docker run -p 8900:8900 -e MCP_AUTH_TOKEN=... aslbelgisi-mcp
```

## Connect Hermes

- URL: `https://<public-host>/sse`
- Header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

`GET /health` is open (no auth) for uptime checks.

## Safety

Read-only. No scanning, no submits to ASL, no account actions. The API key
stays server-side and is never returned to the agent.
