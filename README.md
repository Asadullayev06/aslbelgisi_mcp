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

## Config (env)

| Var | Purpose |
|---|---|
| `ASL_INN` | the company INN (STIR) |
| `ASL_API_KEY` | the company's ASL Business-User API key |
| `ASL_API_BASE` | ASL base URL (default `https://aslbelgisi.uz`) |
| `MCP_AUTH_TOKEN` | **required** — bearer token Hermes must send |
| `MCP_HOST` / `MCP_PORT` | bind (default `0.0.0.0` / `8900`) |

## Run

```bash
pip install -r requirements.txt
export ASL_INN=900071021
export ASL_API_KEY=xxxxxxxx-xxxx-....
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
python server.py
```

Or Docker:

```bash
docker build -t aslbelgisi-mcp .
docker run -p 8900:8900 \
  -e ASL_INN=... -e ASL_API_KEY=... -e MCP_AUTH_TOKEN=... aslbelgisi-mcp
```

## Connect Hermes

- URL: `https://<public-host>/sse`
- Header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

`GET /health` is open (no auth) for uptime checks.

## Safety

Read-only. No scanning, no submits to ASL, no account actions. The API key
stays server-side and is never returned to the agent.
