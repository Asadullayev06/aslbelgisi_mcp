"""Asl Belgisi — standalone MCP server.

A small, self-contained MCP (SSE over HTTP, bearer-auth) that lets an AI agent
(Hermes) answer read-only questions against the ASL Belgisi / xTrace
track-and-trace API:

  * gtin_ostatok  — how much of a product (GTIN) is left
  * km_info       — full details of a KM code (owner, product, status, …)
  * km_owner      — quick "who owns this KM code"
  * sscc_contents — the KM codes inside a box (SSCC)
  * mod_list      — the company's MOD / business-place list

It has NO dependency on the TracePro platform — its own tiny ASL client, and
credentials from env (ASL_INN + ASL_API_KEY).

Run
---
    pip install -r requirements.txt
    export ASL_INN=900071021
    export ASL_API_KEY=xxxxxxxx-xxxx-....
    export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"   # Hermes sends this as Bearer
    export MCP_PORT=8900
    python server.py

Hermes connects to  https://<public-host>/sse  with header
`Authorization: Bearer <MCP_AUTH_TOKEN>`.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import zipfile
from collections import Counter
from typing import Any, Optional

import anyio
import requests
from mcp.server.fastmcp import FastMCP

# ──────────────────────────── config (env) ───────────────────────────────
ASL_BASE = os.environ.get("ASL_API_BASE", "https://aslbelgisi.uz").rstrip("/")
ASL_INN = os.environ.get("ASL_INN", "").strip()
ASL_API_KEY = os.environ.get("ASL_API_KEY", "").strip()
TIMEOUT = int(os.environ.get("ASL_TIMEOUT", "60"))

COD = f"{ASL_BASE}/public/api/cod"

STATUS_LABELS = {
    "EMITTED": "Emissiya", "APPLIED": "Nanesyon",
    "INTRODUCED": "Aylanmaga kiritilgan", "IN_CIRCULATION": "Aylanmada",
    "WITHDRAWN": "Aylanmadan chiqarilgan", "RETIRED": "Hisobdan chiqarilgan",
    "DISAGGREGATED": "Dis-agregatsiya", "AGGREGATED": "Agregatsiya",
    "WAIT_FOR_EMISSION": "Emissiya kutmoqda", "RESERVED": "Rezerv",
}

mcp = FastMCP("aslbelgisi")


# ─────────────────────────── tiny ASL client ─────────────────────────────
def _headers(api_key: str, accept_zip: bool = False) -> dict:
    h = {"Authorization": f"Bearer {api_key}",
         "Accept": "application/zip" if accept_zip else "application/json"}
    if not accept_zip:
        h["Content-Type"] = "application/json;charset=UTF-8"
    return h


def _resolve(inn: str, api_key: str, need_inn: bool = False):
    """Pick the credentials for this call, falling back to env defaults.

    Hermes can pass `inn` / `api_key` on EVERY call — so a different company
    or a rotated key works immediately, with no server env change and no
    restart. If omitted, the ASL_INN / ASL_API_KEY env values are used.
    Returns (inn, api_key, error_or_None)."""
    rinn = (inn or "").strip() or ASL_INN
    rkey = (api_key or "").strip() or ASL_API_KEY
    if not rkey:
        return "", "", "api_key berilmagan (ASL_API_KEY env ham sozlanmagan)"
    if need_inn and not rinn:
        return "", "", "inn berilmagan (ASL_INN env ham sozlanmagan)"
    return rinn, rkey, None


def _loc(value: Any) -> str:
    """Localized text: {en,ru,uz} dict → first non-empty; else str."""
    if isinstance(value, dict):
        for k in ("en", "ru", "uz"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    return value.strip() if isinstance(value, str) else ""


def _deep(node: Any, *path: str) -> str:
    cur = node
    for k in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(k)
    return str(cur).strip() if cur not in (None, "", {}, []) else ""


def _pick_record(raw: Any) -> dict:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    if isinstance(raw, dict):
        for key in ("results", "data"):
            v = raw.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
            if isinstance(v, dict):
                rr = v.get("results")
                if isinstance(rr, list) and rr and isinstance(rr[0], dict):
                    return rr[0]
                return v
        return raw
    return {}


def normalize_sscc(v: str) -> str:
    v = (v or "").strip()
    if len(v) == 18 and v.isdigit():
        return "00" + v
    if len(v) == 20 and v.startswith("00") and v[2:].isdigit():
        return v
    return v


def _code_detail(api_key: str, code: str) -> dict:
    """POST /public/api/cod/private/codes — full detail for one code."""
    try:
        r = requests.post(f"{COD}/private/codes", headers=_headers(api_key),
                          data=json.dumps({"codes": [code]}), timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"ASL HTTP {r.status_code}", "raw": r.text[:400]}
        return {"ok": True, "raw": r.json()}
    except requests.RequestException as e:
        return {"ok": False, "error": f"tarmoq xatosi: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"javob xatosi: {e}"}


def _parse_code(raw: Any, code: str) -> dict:
    d = _pick_record(raw)
    cd = d.get("codeData", {}) if isinstance(d, dict) else {}
    pd = d.get("productData", {}) if isinstance(d, dict) else {}
    pk = d.get("packageData", {}) if isinstance(d, dict) else {}
    to = d.get("turnoverData", {}) if isinstance(d, dict) else {}
    own = to.get("ownerInfo", {}) if isinstance(to, dict) else {}
    status = _deep(cd, "status") or _deep(d, "status") or _deep(d, "cisStatus")
    return {
        "code": _deep(cd, "code") or _deep(d, "code") or code,
        "status": status,
        "status_label": STATUS_LABELS.get(status, ""),
        "gtin": _deep(pd, "gtin") or _deep(d, "gtin"),
        "product_name": (_loc(pd.get("name")) or _loc(pd.get("productName"))
                         or _loc(pd.get("label")) or _deep(pd, "fullName")),
        "product_series": _deep(pd, "productSeries") or _deep(d, "batch"),
        "production_date": _deep(pd, "productionDate"),
        "expiration_date": _deep(pd, "expirationDate"),
        "owner_inn": _deep(own, "ownerTin") or _deep(own, "inn") or _deep(d, "ownerInn"),
        "owner_name": _loc(own.get("ownerName")) or _loc(own.get("name")) or _deep(d, "ownerName"),
        "package_type": _deep(pk, "packageType") or _deep(d, "packageType"),
        "parent_code": _deep(pk, "parentCode"),
        "is_aggregated": bool(pk.get("children")) if isinstance(pk, dict) else False,
    }


def _flatten_children(payload: Any) -> list[str]:
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k in ("childCodes", "children", "nestedCodes", "consumptionCodes", "codes"):
                child = node.get(k)
                if isinstance(child, list):
                    for c in child:
                        walk(c)
            for k in ("code", "consumerCode", "consumptionCode", "cis"):
                v = node.get(k)
                if isinstance(v, str) and v.startswith("01") and len(v) >= 31:
                    out.append(v); return
        elif isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, str):
            if node.startswith("01") and len(node) >= 31:
                out.append(node)
    root = payload.get("data", payload) if isinstance(payload, dict) else payload
    walk(root)
    # dedup, keep order
    seen: set[str] = set(); uniq: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return uniq


def _normalize_rows(payload: Any) -> list[dict]:
    items = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        for k in ("results", "codes", "data"):
            if isinstance(payload.get(k), list):
                items = payload[k]; break
        else:
            if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("results"), list):
                items = payload["data"]["results"]
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cd = it.get("codeData", {}) if isinstance(it.get("codeData"), dict) else {}
        pd = it.get("productData", {}) if isinstance(it.get("productData"), dict) else {}
        pk = it.get("packageData", {}) if isinstance(it.get("packageData"), dict) else {}
        to = it.get("turnoverData", {}) if isinstance(it.get("turnoverData"), dict) else {}
        own = to.get("ownerInfo", {}) if isinstance(to.get("ownerInfo"), dict) else {}
        code = _deep(cd, "code") or str(it.get("code", "")).strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "status": _deep(cd, "status") or str(it.get("status", "")).strip(),
            "package_type": _deep(pk, "packageType") or str(it.get("packageType", "")).strip(),
            "gtin": _deep(pd, "gtin") or str(it.get("gtin", "")).strip(),
            "product_name": _loc(pd.get("name") or pd.get("productName") or pd.get("label")),
            "product_series": _deep(pd, "productSeries") or str(it.get("productSeries", "")).strip(),
            "owner_tin": _deep(own, "ownerTin") or str(it.get("ownerTin", "")).strip(),
            "owner_name": _loc(own.get("ownerName") or it.get("ownerName", "")),
        })
    return rows


async def _in_thread(fn, *a):
    return await anyio.to_thread.run_sync(fn, *a)


# ─────────────────────────────── tools ───────────────────────────────────
@mcp.tool()
async def km_info(codes: list[str], inn: str = "", api_key: str = "") -> dict:
    """Full details for one or more KM (marking) codes: current owner, product,
    status, aggregation (which box), dates. Up to 50 codes.

    Pass the company `api_key` (and `inn`) to query a specific company; omit to
    use the server's env defaults."""
    def _impl() -> dict:
        _rinn, rkey, err = _resolve(inn, api_key)
        if err:
            return {"ok": False, "error": err}
        clean = [c.strip() for c in codes if c and c.strip()][:50]
        if not clean:
            return {"ok": False, "error": "Kamida bitta KM kod kiriting"}
        results = []
        for c in clean:
            det = _code_detail(rkey, c)
            if not det.get("ok"):
                results.append({"code": c, "found": False, "error": det.get("error")})
            else:
                parsed = _parse_code(det["raw"], c)
                parsed["found"] = bool(parsed.get("status"))
                results.append(parsed)
            time.sleep(0.25)
        return {"ok": True, "total": len(results),
                "found": sum(1 for r in results if r.get("found")), "results": results}
    return await _in_thread(_impl)


@mcp.tool()
async def km_owner(code: str, inn: str = "", api_key: str = "") -> dict:
    """Quick lookup — the CURRENT OWNER (INN + name), product and status of a
    single KM code. Pass `api_key` (and `inn`) for a specific company, or omit
    to use the env defaults."""
    def _impl() -> dict:
        _rinn, rkey, err = _resolve(inn, api_key)
        if err:
            return {"ok": False, "error": err}
        c = (code or "").strip()
        if not c:
            return {"ok": False, "error": "KM kod kiriting"}
        det = _code_detail(rkey, c)
        if not det.get("ok"):
            return {"ok": False, "code": c, "error": det.get("error")}
        p = _parse_code(det["raw"], c)
        return {"ok": bool(p.get("status")), "code": p["code"],
                "owner_inn": p["owner_inn"], "owner_name": p["owner_name"],
                "product_name": p["product_name"], "status": p["status"],
                "status_label": p["status_label"]}
    return await _in_thread(_impl)


@mcp.tool()
async def km_owners_table(codes: list[str], inn: str = "", api_key: str = "") -> dict:
    """Look up the OWNER of many KM codes and return a ready-to-save table.

    Built for the "Excel in → Excel out" flow: give it the list of codes and
    it returns both `rows` (structured) and `csv` (a full CSV string with a
    header row, UTF-8). Save `csv` straight to a .csv/.xlsx — no reformatting.

    Columns: Kod, GTIN, Mahsulot, Seriya, Status, Status (izoh), Egasi INN,
    Egasi nomi. Up to 300 codes per call (ASL is ~1 request per code, so large
    batches are slow and rate-limited — chunk if you have more).

    Pass `api_key` (and `inn`) for a specific company, or omit for env defaults."""
    def _impl() -> dict:
        _rinn, rkey, err = _resolve(inn, api_key)
        if err:
            return {"ok": False, "error": err}
        clean = [c.strip() for c in codes if c and c.strip()][:300]
        if not clean:
            return {"ok": False, "error": "Kamida bitta KM kod kiriting"}

        headers = ["Kod", "GTIN", "Mahsulot", "Seriya", "Status",
                   "Status (izoh)", "Egasi INN", "Egasi nomi"]
        rows: list[dict] = []
        for c in clean:
            det = _code_detail(rkey, c)
            if not det.get("ok"):
                rows.append({"Kod": c, "GTIN": "", "Mahsulot": "", "Seriya": "",
                             "Status": "", "Status (izoh)": "", "Egasi INN": "",
                             "Egasi nomi": f"XATO: {det.get('error', '')}"})
            else:
                p = _parse_code(det["raw"], c)
                rows.append({
                    "Kod": p["code"], "GTIN": p["gtin"],
                    "Mahsulot": p["product_name"], "Seriya": p["product_series"],
                    "Status": p["status"], "Status (izoh)": p["status_label"],
                    "Egasi INN": p["owner_inn"], "Egasi nomi": p["owner_name"],
                })
            time.sleep(0.25)

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
        found = sum(1 for r in rows if r["Status"])
        return {"ok": True, "total": len(rows), "found": found,
                "columns": headers, "rows": rows, "csv": buf.getvalue()}
    return await _in_thread(_impl)


@mcp.tool()
async def sscc_contents(sscc: str, inn: str = "", api_key: str = "") -> dict:
    """List the child KM codes inside a box (SSCC) and verify it belongs to the
    company. verdict = owned | forbidden | missing. Pass `inn` + `api_key` for a
    specific company, or omit for env defaults (both are needed here)."""
    def _impl() -> dict:
        rinn, rkey, err = _resolve(inn, api_key, need_inn=True)
        if err:
            return {"ok": False, "error": err}
        canonical = normalize_sscc(sscc)
        try:
            r = requests.post(f"{COD}/nested-codes/owner-check", headers=_headers(rkey),
                              data=json.dumps({"codes": [canonical], "ownerTin": rinn}),
                              timeout=TIMEOUT)
        except requests.RequestException as e:
            return {"ok": False, "error": f"tarmoq xatosi: {e}"}
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"ASL HTTP {r.status_code}", "raw": r.text[:300]}
        data = r.json()
        top = data.get("data", data) if isinstance(data, dict) else data
        forbidden = {str(c) for c in (top.get("forbiddenCodes") or [])} if isinstance(top, dict) else set()
        missing = {str(c) for c in (top.get("missingCodes") or [])} if isinstance(top, dict) else set()
        if canonical in forbidden:
            return {"ok": True, "sscc": canonical, "verdict": "forbidden", "child_count": 0, "children": []}
        if canonical in missing:
            return {"ok": True, "sscc": canonical, "verdict": "missing", "child_count": 0, "children": []}
        children = _flatten_children(top)
        return {"ok": True, "sscc": canonical, "verdict": "owned",
                "child_count": len(children), "children": children[:500]}
    return await _in_thread(_impl)


@mcp.tool()
async def mod_list(inn: str = "", api_key: str = "") -> dict:
    """List the company's MOD / business-place entries registered in ASL.
    Pass `api_key` for a specific company, or omit for the env default."""
    def _impl() -> dict:
        _rinn, rkey, err = _resolve(inn, api_key)
        if err:
            return {"ok": False, "error": err}
        for url in (f"{ASL_BASE}/api/v2/participants/mod",):
            try:
                r = requests.get(url, headers=_headers(rkey), timeout=TIMEOUT)
                if r.status_code in (200, 201):
                    data = r.json()
                    items = data if isinstance(data, list) else (data or {}).get("result") or (data or {}).get("data") or []
                    return {"ok": True, "mods": items}
            except requests.RequestException:
                continue
        return {"ok": False, "error": "MOD ro'yxatini olib bo'lmadi"}
    return await _in_thread(_impl)


@mcp.tool()
async def gtin_ostatok(
    gtin: str,
    product_series: str = "",
    statuses: Optional[list[str]] = None,
    package_types: Optional[list[str]] = None,
    emission_date_from: str = "",
    emission_date_to: str = "",
    inn: str = "",
    api_key: str = "",
) -> dict:
    """How much of a product (by GTIN) remains — the "ostatok" check.

    Registers an ASL export, waits for it, and returns a summary: product name,
    total codes, and breakdown by status / owner / series / package type, plus
    a small sample of codes.

    gtin: 14-digit GTIN (digits only, leading zero included).
    Optional filters narrow the result. ASL caps exports at ~10 MB with no
    pagination, so very large GTINs return ERROR — narrow with a series or a
    date window.
    """
    def _impl() -> dict:
        _rinn, rkey, err = _resolve(inn, api_key)
        if err:
            return {"ok": False, "error": err}
        g = (gtin or "").strip()
        if not g.isdigit():
            return {"ok": False, "error": "GTIN faqat raqamlardan iborat bo'lishi kerak"}

        payload: dict[str, Any] = {"gtin": g}
        if package_types: payload["packageType"] = package_types
        if statuses: payload["status"] = statuses
        if product_series.strip(): payload["productSeries"] = product_series.strip()
        if emission_date_from.strip(): payload["emissionDateFrom"] = f"{emission_date_from.strip()}T00:00:00Z"
        if emission_date_to.strip(): payload["emissionDateTo"] = f"{emission_date_to.strip()}T23:59:59Z"

        # register
        try:
            r = requests.post(f"{COD}/exports", headers=_headers(rkey),
                              data=json.dumps(payload), timeout=TIMEOUT)
        except requests.RequestException as e:
            return {"ok": False, "error": f"tarmoq xatosi: {e}"}
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"register HTTP {r.status_code}: {r.text[:300]}"}
        export_id = str((r.json() or {}).get("id", "")).strip()
        if not export_id:
            return {"ok": False, "error": "ASL export ID qaytmadi"}

        # poll (cap ~90s)
        status_value = ""
        for _ in range(36):
            try:
                st = requests.get(f"{COD}/exports/{export_id}/status",
                                 headers=_headers(rkey), timeout=TIMEOUT)
                raw = st.json() if st.headers.get("content-type", "").startswith("application/json") else st.text
                status_value = (raw.get("status") if isinstance(raw, dict) else str(raw)).strip().upper().strip('"')
                if status_value in ("SUCCESS", "ERROR"):
                    break
            except Exception:
                pass
            time.sleep(2.5)

        if status_value == "ERROR":
            return {"ok": False, "status": "ERROR", "export_id": export_id,
                    "error": "Export juda katta (ASL 10MB cheklovi). Seriya yoki sana oralig'i bilan toraytiring."}
        if status_value != "SUCCESS":
            return {"ok": False, "status": status_value or "PENDING", "export_id": export_id,
                    "error": "Export hali tayyor emas — toraytirilgan filtr bilan qayta urining."}

        # result (zip → json)
        try:
            res = requests.get(f"{COD}/exports/{export_id}/result",
                              headers=_headers(rkey, accept_zip=True), timeout=TIMEOUT)
        except requests.RequestException as e:
            return {"ok": False, "error": f"result tarmoq xatosi: {e}"}
        if res.status_code not in (200, 201):
            return {"ok": False, "error": f"result HTTP {res.status_code}: {res.text[:300]}"}
        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".json")]
                if not names:
                    return {"ok": False, "error": "ZIP arxivida JSON yo'q"}
                data = json.loads(zf.read(names[0]).decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"ZIP ni o'qib bo'lmadi: {e}"}

        rows = _normalize_rows(data)
        if product_series.strip():
            want = product_series.strip()
            rows = [r for r in rows if (r.get("product_series", "") or "").strip() == want]

        by_status = Counter(r.get("status", "") for r in rows)
        by_series = Counter(r.get("product_series", "") for r in rows)
        by_pkg = Counter(r.get("package_type", "") for r in rows)
        owners = Counter((r.get("owner_tin", ""), r.get("owner_name", "")) for r in rows)
        product = next((r.get("product_name", "") for r in rows if r.get("product_name")), "")
        return {
            "ok": True, "gtin": g, "product_name": product, "total_codes": len(rows),
            "by_status": dict(by_status), "by_series": dict(by_series),
            "by_package_type": dict(by_pkg),
            "by_owner": [{"tin": t, "name": n, "count": c} for (t, n), c in owners.most_common()],
            "codes_sample": [r.get("code", "") for r in rows[:20]],
        }
    return await _in_thread(_impl)


# ─────────────────────────── HTTP (SSE) + auth ───────────────────────────
def _build_app():
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ("/health", "/"):
                return await call_next(request)
            if token:
                auth = request.headers.get("authorization", "")
                presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                if presented != token:
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = mcp.sse_app()
    app.add_middleware(AuthMiddleware)

    async def health(_req):
        return JSONResponse({"ok": True, "service": "aslbelgisi-mcp"})
    app.add_route("/health", health, methods=["GET"])
    return app


def main() -> None:
    import uvicorn
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8900"))
    if not os.environ.get("MCP_AUTH_TOKEN", "").strip():
        print("WARNING: MCP_AUTH_TOKEN not set — server is UNAUTHENTICATED.")
    if not ASL_API_KEY:
        print("NOTE: ASL_API_KEY env not set — callers must pass api_key (and "
              "inn) on each tool call.")
    uvicorn.run(_build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
