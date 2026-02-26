"""
Invoice vs PO Tax Analyzer — MCP Server (Ariba Integrated)

Run:
    pip install fastmcp fastapi httpx uvicorn
    mcp run server.py
"""

import os
import json
import time
from typing import Any
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP

# ─────────────────────────────────────────────────────────────
# ENV CONFIG (NEVER HARD-CODE SECRETS)
# ─────────────────────────────────────────────────────────────

ARIBA_BASE_URL = "https://api.ariba.com"
ARIBA_TXN_BASE_URL = "https://openapi.ariba.com/api"

REALM = os.getenv("ARIBA_REALM")
CLIENT_ID = os.getenv("ARIBA_CLIENT_ID")
CLIENT_SECRET = os.getenv("ARIBA_CLIENT_SECRET")
ARIBA_API_KEY = os.getenv("ARIBA_API_KEY")
X_ARIBA_NETWORK_ID = os.getenv("ARIBA_NETWORK_ID")

# ─────────────────────────────────────────────────────────────
# MCP APP
# ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="invoice-po-tax-analyzer",
    instructions="Loads POs & Invoices from Ariba and compares tax discrepancies."
)

app = FastAPI()
app.mount("/", mcp.streamable_http_app())

_token_cache = {"access_token": None, "expires_at": 0}
_store = {"invoices": {}, "purchase_orders": {}}

# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    url = f"{ARIBA_BASE_URL}/v2/oauth/token"

    response = httpx.post(
        url,
        data={"grant_type": "client_credentials", "realm": REALM},
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code != 200:
        raise ValueError(f"Token fetch failed: {response.text}")

    token_data = response.json()
    _token_cache["access_token"] = token_data["access_token"]
    _token_cache["expires_at"] = time.time() + token_data["expires_in"]
    return token_data["access_token"]

def _ariba_headers():
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Accept": "application/json",
        "apiKey": ARIBA_API_KEY,
        "X-ARIBA-NETWORK-ID": X_ARIBA_NETWORK_ID
    }

# ─────────────────────────────────────────────────────────────
# ARIBA FETCHERS
# ─────────────────────────────────────────────────────────────

def _build_date_filter(start: str, end: str):
    return (
        f"and startDate eq '{start}' "
        f"and endDate eq '{end}'"
    )

def _fetch_pos(start: str, end: str):
    url = f"{ARIBA_TXN_BASE_URL}/purchase-orders/v1/prod/orders?{_build_date_filter(start,end)}&$top=100"
    r = httpx.get(url, headers=_ariba_headers(), timeout=30)
    if r.status_code != 200:
        raise ValueError(f"PO fetch failed: {r.text}")
    return r.json().get("content", [])

def _fetch_invoices(start: str, end: str):
    url = f"{ARIBA_TXN_BASE_URL}/invoices/v1/prod/invoices?{_build_date_filter(start,end)}&$top=100"
    r = httpx.get(url, headers=_ariba_headers(), timeout=30)
    if r.status_code != 200:
        raise ValueError(f"Invoice fetch failed: {r.text}")
    return r.json().get("content", [])

# ─────────────────────────────────────────────────────────────
# NORMALIZATION
# ─────────────────────────────────────────────────────────────

def _normalize_po(po: dict):
    amount = po.get("poAmount", {}).get("amount", 0)
    return {
        "documentNumber": po.get("documentNumber"),
        "taxAmount": 0,
        "taxRate": 0,
        "taxableAmount": amount,
        "totalAmount": amount,
        "currency": po.get("poAmount", {}).get("currencyCode"),
        "status": po.get("status")
    }

def _normalize_invoice(inv: dict):
    return {
        "documentNumber": inv.get("documentNumber"),
        "taxAmount": inv.get("taxAmount", 0),
        "taxRate": inv.get("taxRate", 0),
        "taxableAmount": inv.get("subTotalAmount", 0),
        "totalAmount": inv.get("invoiceAmount", {}).get("amount", 0),
        "currency": inv.get("invoiceAmount", {}).get("currencyCode"),
        "status": inv.get("status")
    }

# ─────────────────────────────────────────────────────────────
# TAX COMPARISON ENGINE
# ─────────────────────────────────────────────────────────────

def _compare(inv: dict, po: dict):

    diffs = []
    reasons = []

    if inv["taxRate"] != po["taxRate"]:
        diffs.append({
            "field": "taxRate",
            "invoice": inv["taxRate"],
            "po": po["taxRate"]
        })
        reasons.append("Tax rate mismatch.")

    if inv["taxAmount"] != po["taxAmount"]:
        diffs.append({
            "field": "taxAmount",
            "invoice": inv["taxAmount"],
            "po": po["taxAmount"]
        })
        reasons.append("Tax amount differs.")

    if inv["taxableAmount"] != po["taxableAmount"]:
        diffs.append({
            "field": "taxableAmount",
            "invoice": inv["taxableAmount"],
            "po": po["taxableAmount"]
        })
        reasons.append("Taxable base differs.")

    return {
        "hasDifferences": len(diffs) > 0,
        "differences": diffs,
        "reasons": reasons
    }

# ─────────────────────────────────────────────────────────────
# MCP TOOLS
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def load_pos_from_api(start: str, end: str) -> str:
    pos = _fetch_pos(start, end)
    for po in pos:
        normalized = _normalize_po(po)
        _store["purchase_orders"][normalized["documentNumber"]] = normalized
    return json.dumps({"loaded_pos": len(pos)})

@mcp.tool()
def load_invoices_from_api(start: str, end: str) -> str:
    invoices = _fetch_invoices(start, end)
    for inv in invoices:
        normalized = _normalize_invoice(inv)
        _store["invoices"][normalized["documentNumber"]] = normalized
    return json.dumps({"loaded_invoices": len(invoices)})

@mcp.tool()
def compare_tax(invoice_id: str, po_id: str) -> str:
    inv = _store["invoices"].get(invoice_id)
    po = _store["purchase_orders"].get(po_id)

    if not inv:
        raise ValueError("Invoice not found")
    if not po:
        raise ValueError("PO not found")

    return json.dumps(_compare(inv, po), indent=2)

@mcp.tool()
def batch_reconcile() -> str:
    results = []

    for po_id, po in _store["purchase_orders"].items():
        invoice = _store["invoices"].get(po_id)
        if invoice:
            results.append({
                "document": po_id,
                "comparison": _compare(invoice, po)
            })

    return json.dumps(results, indent=2)

@mcp.tool()
def list_documents() -> str:
    return json.dumps({
        "invoices": list(_store["invoices"].keys()),
        "purchase_orders": list(_store["purchase_orders"].keys())
    }, indent=2)

# ─────────────────────────────────────────────────────────────
# LOCAL TEST ENTRY
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Run using: mcp run server.py")