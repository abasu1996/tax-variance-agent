"""
Invoice vs PO Tax Analyzer — MCP Server (Ariba Integrated)

Run:
    pip install fastmcp fastapi httpx
    mcp run server.py
"""

import os
import json
import time
import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

# ─────────────────────────────────────────────
# CONFIG (Use ENV Variables)
# ─────────────────────────────────────────────

ARIBA_BASE_URL = "https://api.ariba.com"
ARIBA_TXN_BASE_URL = "https://openapi.ariba.com/api"

REALM = os.getenv("ARIBA_REALM")
CLIENT_ID = os.getenv("ARIBA_CLIENT_ID")
CLIENT_SECRET = os.getenv("ARIBA_CLIENT_SECRET")
ARIBA_API_KEY = os.getenv("ARIBA_API_KEY")
X_ARIBA_NETWORK_ID = os.getenv("ARIBA_NETWORK_ID")

# ─────────────────────────────────────────────
# MCP APP
# ─────────────────────────────────────────────

mcp = FastMCP(
    name="invoice-po-tax-analyzer",
    instructions="""
This MCP server connects to SAP Ariba APIs.

Workflow:
1. Call load_pos_from_api to fetch Purchase Orders.
2. Call load_invoices_from_api to fetch Invoices.
3. Use compare_tax to compare a specific Invoice vs PO.
4. Use batch_reconcile to reconcile all matching document numbers.

Always load documents before comparing.
"""
)

app = FastAPI()
app.mount("/", mcp.http_app())

_token_cache = {"access_token": None, "expires_at": 0}
_store = {"invoices": {}, "purchase_orders": {}}

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

def _get_access_token():
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    response = httpx.post(
        f"{ARIBA_BASE_URL}/v2/oauth/token",
        data={"grant_type": "client_credentials", "realm": REALM},
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code != 200:
        raise ValueError(f"Token error: {response.text}")

    data = response.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data["expires_in"]
    return data["access_token"]

def _headers():
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "apiKey": ARIBA_API_KEY,
        "X-ARIBA-NETWORK-ID": X_ARIBA_NETWORK_ID,
        "Accept": "application/json"
    }

# ─────────────────────────────────────────────
# FETCH HELPERS
# ─────────────────────────────────────────────

def _date_filter(start, end):
    return (
        f"$filter=startDate eq '{start}' "
        f"and endDate eq '{end}'"
    )

def _fetch(endpoint, start, end):
    url = f"{ARIBA_TXN_BASE_URL}/{endpoint}?{_date_filter(start,end)}&$top=100"
    r = httpx.get(url, headers=_headers(), timeout=30)
    if r.status_code != 200:
        raise ValueError(r.text)
    return r.json().get("content", [])

# ─────────────────────────────────────────────
# NORMALIZATION
# ─────────────────────────────────────────────

def _normalize_po(po):
    amt = po.get("poAmount", {}).get("amount", 0)
    return {
        "documentNumber": po.get("documentNumber"),
        "taxAmount": 0,
        "taxRate": 0,
        "taxableAmount": amt,
        "totalAmount": amt,
        "status": po.get("status")
    }

def _normalize_invoice(inv):
    return {
        "documentNumber": inv.get("documentNumber"),
        "taxAmount": inv.get("taxAmount", 0),
        "taxRate": inv.get("taxRate", 0),
        "taxableAmount": inv.get("subTotalAmount", 0),
        "totalAmount": inv.get("invoiceAmount", {}).get("amount", 0),
        "status": inv.get("status")
    }

# ─────────────────────────────────────────────
# COMPARISON ENGINE
# ─────────────────────────────────────────────

def _compare(inv, po):
    diffs = []
    if inv["taxRate"] != po["taxRate"]:
        diffs.append({"field": "taxRate", "invoice": inv["taxRate"], "po": po["taxRate"]})
    if inv["taxAmount"] != po["taxAmount"]:
        diffs.append({"field": "taxAmount", "invoice": inv["taxAmount"], "po": po["taxAmount"]})
    if inv["taxableAmount"] != po["taxableAmount"]:
        diffs.append({"field": "taxableAmount", "invoice": inv["taxableAmount"], "po": po["taxableAmount"]})
    return {"hasDifferences": bool(diffs), "differences": diffs}

# ─────────────────────────────────────────────
# MCP TOOLS WITH CLEAR PROMPTS
# ─────────────────────────────────────────────

@mcp.tool()
def load_pos_from_api(start: str, end: str) -> str:
    """
    Fetch Purchase Orders from SAP Ariba within a date range.

    Use this tool FIRST before performing comparisons.

    Inputs:
    - start: ISO date-time string (YYYY-MM-DDTHH:MM:SS)
    - end:   ISO date-time string (YYYY-MM-DDTHH:MM:SS)
      Maximum allowed range is 31 days.

    This tool:
    - Calls the Ariba Purchase Order API
    - Normalizes the response
    - Stores POs internally keyed by documentNumber

    Returns:
    - Number of POs loaded
    """
    pos = _fetch("purchase-orders/v1/prod/orders", start, end)
    logger.info(f"Fetched {len(pos)} POs from API.")
    for po in pos:
        n = _normalize_po(po)
        _store["purchase_orders"][n["documentNumber"]] = n
    return json.dumps({"loaded_pos": len(pos)})

@mcp.tool()
def load_invoices_from_api(start: str, end: str) -> str:
    """
    Fetch Invoices from SAP Ariba within a date range.

    Use this AFTER loading POs if reconciliation is required.

    Inputs:
    - start: ISO date-time string
    - end:   ISO date-time string

    This tool:
    - Calls the Ariba Invoice API
    - Normalizes the response
    - Stores invoices internally keyed by documentNumber

    Returns:
    - Number of invoices loaded
    """
    invoices = _fetch("invoices/v1/prod/invoices", start, end)
    for inv in invoices:
        n = _normalize_invoice(inv)
        _store["invoices"][n["documentNumber"]] = n
    return json.dumps({"loaded_invoices": len(invoices),"store":_store})

@mcp.tool()
def compare_tax(invoice_id: str, po_id: str) -> str:
    """
    Compare tax details between a specific Invoice and Purchase Order.

    Use this tool AFTER both documents have been loaded.

    Inputs:
    - invoice_id: documentNumber of the invoice
    - po_id:      documentNumber of the purchase order

    Returns:
    - JSON diff showing tax discrepancies
    """
    inv = _store["invoices"].get(invoice_id)
    po = _store["purchase_orders"].get(po_id)
    if not inv:
        raise ValueError("Invoice not loaded.")
    if not po:
        raise ValueError("PO not loaded.")
    return json.dumps(_compare(inv, po), indent=2)

@mcp.tool()
def batch_reconcile() -> str:
    """
    Automatically reconcile all loaded Purchase Orders against
    Invoices with the same documentNumber.

    Use this when performing bulk reconciliation.

    Returns:
    - List of comparison results for matching documents
    """
    results = []
    for po_id, po in _store["purchase_orders"].items():
        inv = _store["invoices"].get(po_id)
        if inv:
            results.append({
                "documentNumber": po_id,
                "comparison": _compare(inv, po)
            })
    return json.dumps(results, indent=2)

@mcp.tool()
def list_documents() -> str:
    """
    List all currently loaded Invoices and Purchase Orders.
    Useful to check available document IDs before comparison.
    """
    return json.dumps({
        "invoices": list(_store["invoices"].keys()),
        "purchase_orders": list(_store["purchase_orders"].keys())
    }, indent=2)

# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Run using: mcp run server.py")