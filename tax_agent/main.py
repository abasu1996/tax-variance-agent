"""
Invoice vs PO Tax Analyzer — MCP Server
Uses FastMCP with `mcp` as the entry point.

Install:
    pip install mcp

Run directly via MCP CLI:
    mcp run server.py

Or install into Claude Desktop automatically:
    mcp install server.py
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ─────────────────────────────────────────────────────────────────────────────
# FastMCP app — this is what `mcp run server.py` looks for
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="invoice-po-tax-analyzer",
    instructions=(
        "This server loads Invoice and Purchase Order documents (JSON or XML), "
        "extracts tax fields, and identifies & explains every tax discrepancy between them. "
        "Use load_invoice and load_po first, then compare_tax or explain_differences."
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory document store
# ─────────────────────────────────────────────────────────────────────────────
_store: dict[str, dict] = {"invoices": {}, "purchase_orders": {}}


# ══════════════════════════════════════════════════════════════════════════════
# PARSING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _parse_xml(path: str) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    return _xml_to_dict(root)


def _xml_to_dict(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return element.text.strip() if element.text else ""
    result: dict = {}
    for child in children:
        value = _xml_to_dict(child)
        tag = child.tag.split("}")[-1]          # strip XML namespace
        if tag in result:
            if not isinstance(result[tag], list):
                result[tag] = [result[tag]]
            result[tag].append(value)
        else:
            result[tag] = value
    return result


def _load_document(file_path: str) -> dict:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    ext = p.suffix.lower()
    if ext == ".json":
        return _parse_json(file_path)
    elif ext in (".xml", ".xsd"):
        return _parse_xml(file_path)
    else:
        raise ValueError(f"Unsupported file type '{ext}'. Use .json or .xml")


# ══════════════════════════════════════════════════════════════════════════════
# TAX EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _safe_float(val) -> float:
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _flatten(d: Any, prefix: str = "", sep: str = ".") -> dict:
    items = {}
    if isinstance(d, dict):
        for k, v in d.items():
            new_key = f"{prefix}{sep}{k}".lower() if prefix else k.lower()
            items.update(_flatten(v, new_key, sep))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            items.update(_flatten(v, f"{prefix}[{i}]", sep))
    else:
        items[prefix] = d
    return items


def _find_field(flat: dict, candidates: list[str], as_str: bool = False):
    for key, val in flat.items():
        clean_key = key.replace("_", "").replace(".", "").replace("[", "").replace("]", "")
        for candidate in candidates:
            if candidate in clean_key:
                if as_str:
                    return str(val) if val else ""
                return _safe_float(val)
    return "" if as_str else 0.0


def _deep_get(d: Any, key: str) -> Any:
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == key.lower():
                return v
        for k, v in d.items():
            result = _deep_get(v, key)
            if result is not None:
                return result
    elif isinstance(d, list):
        for item in d:
            result = _deep_get(item, key)
            if result is not None:
                return result
    return None


def _extract_line_items(doc: dict) -> list[dict]:
    for key in ["line_items", "items", "lines", "line", "details", "entries"]:
        val = _deep_get(doc, key)
        if val and isinstance(val, list):
            return [
                {
                    "description": _deep_get(item, "description") or _deep_get(item, "name") or "",
                    "quantity":    _safe_float(_deep_get(item, "quantity") or _deep_get(item, "qty") or 0),
                    "unit_price":  _safe_float(_deep_get(item, "unit_price") or _deep_get(item, "price") or 0),
                    "tax_rate":    _safe_float(_deep_get(item, "tax_rate") or _deep_get(item, "tax") or 0),
                    "tax_amount":  _safe_float(_deep_get(item, "tax_amount") or 0),
                    "line_total":  _safe_float(_deep_get(item, "line_total") or _deep_get(item, "total") or 0),
                }
                for item in val if isinstance(item, dict)
            ]
    return []


def _extract_tax_info(doc: dict) -> dict:
    flat = _flatten(doc)
    tax_info = {
        "tax_amount":     _find_field(flat, ["taxamount", "taxamt", "taxvalue", "vatamount", "gstamount", "taxtotal"]),
        "tax_rate":       _find_field(flat, ["taxrate", "vatrate", "gstrate", "taxpercent", "taxpercentage"]),
        "taxable_amount": _find_field(flat, ["taxableamount", "subtotal", "netamount", "taxablebase"]),
        "tax_type":       _find_field(flat, ["taxtype", "taxcode", "vattype"], as_str=True),
        "total_amount":   _find_field(flat, ["total", "totalamount", "grandtotal", "invoicetotal", "pototal"]),
        "line_items":     _extract_line_items(doc),
    }
    if tax_info["tax_rate"] == 0.0 and tax_info["taxable_amount"] and tax_info["tax_amount"]:
        try:
            tax_info["tax_rate"] = round(
                (tax_info["tax_amount"] / tax_info["taxable_amount"]) * 100, 4
            )
            tax_info["tax_rate_derived"] = True
        except ZeroDivisionError:
            pass
    return tax_info


# ══════════════════════════════════════════════════════════════════════════════
# COMPARISON ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _explain_rate_difference(inv_rate: float, po_rate: float) -> str:
    if inv_rate == 0 and po_rate > 0:
        return "Invoice shows zero tax — vendor may be tax-exempt or applied wrong exemption."
    if po_rate == 0 and inv_rate > 0:
        return "PO was raised tax-exempt but Invoice includes tax — check vendor tax registration."
    return (
        f"Tax rate changed from {po_rate}% (PO) to {inv_rate}% (Invoice). "
        "Common causes: (1) Government rate change between PO and Invoice date, "
        "(2) Wrong tax code on PO, (3) Vendor applied a different jurisdiction, "
        "(4) Goods/services reclassified under a different tax category."
    )


def _explain_amount_difference(inv_amt, po_amt, inv_rate, po_rate, invoice_tax, po_tax) -> str:
    if abs(inv_rate - po_rate) < 0.0001 and abs(invoice_tax["taxable_amount"] - po_tax["taxable_amount"]) > 0.005:
        return (
            "Tax amount differs due to a change in the taxable base, not the rate. "
            "Price negotiation, quantity change, or additional charges (freight, packaging) "
            "may have been added after PO was raised."
        )
    if abs(inv_amt - po_amt) < 1.0:
        return (
            "Small tax amount difference — likely a rounding issue. "
            "PO may round per-line while Invoice rounds at header level."
        )
    return (
        "Significant tax amount difference. Investigate: (1) Extra line items on Invoice not in PO, "
        "(2) Price variance, (3) Different tax rate applied."
    )


def _compare_line_items(inv_lines: list, po_lines: list) -> list[dict]:
    diffs = []
    for i in range(max(len(inv_lines), len(po_lines), 1) - (1 if not inv_lines and not po_lines else 0)):
        inv = inv_lines[i] if i < len(inv_lines) else None
        po  = po_lines[i]  if i < len(po_lines)  else None
        if inv is None:
            diffs.append({"line": i + 1, "issue": "Line exists in PO but missing in Invoice", "po": po})
        elif po is None:
            diffs.append({"line": i + 1, "issue": "Extra line in Invoice not in PO", "invoice": inv})
        else:
            line_diff = {}
            if abs(inv["tax_rate"] - po["tax_rate"]) > 0.0001:
                line_diff["tax_rate"] = {"invoice": inv["tax_rate"], "po": po["tax_rate"]}
            if abs(inv["tax_amount"] - po["tax_amount"]) > 0.005:
                line_diff["tax_amount"] = {"invoice": inv["tax_amount"], "po": po["tax_amount"]}
            if abs(inv["unit_price"] - po["unit_price"]) > 0.005:
                line_diff["unit_price"] = {"invoice": inv["unit_price"], "po": po["unit_price"]}
            if line_diff:
                diffs.append({"line": i + 1, "description": inv.get("description", ""), "differences": line_diff})
    return diffs


def _compare_taxes(invoice_tax: dict, po_tax: dict) -> dict:
    differences, reasons = [], []

    inv_rate, po_rate = invoice_tax["tax_rate"], po_tax["tax_rate"]
    rate_diff = round(inv_rate - po_rate, 4)
    if abs(rate_diff) > 0.0001:
        differences.append({"field": "tax_rate", "invoice_value": f"{inv_rate}%",
                             "po_value": f"{po_rate}%", "difference": f"{rate_diff:+.4f}%"})
        reasons.append(_explain_rate_difference(inv_rate, po_rate))

    inv_amt, po_amt = invoice_tax["tax_amount"], po_tax["tax_amount"]
    amt_diff = round(inv_amt - po_amt, 4)
    if abs(amt_diff) > 0.005:
        differences.append({"field": "tax_amount", "invoice_value": inv_amt,
                             "po_value": po_amt, "difference": f"{amt_diff:+.4f}"})
        reasons.append(_explain_amount_difference(inv_amt, po_amt, inv_rate, po_rate, invoice_tax, po_tax))

    inv_base, po_base = invoice_tax["taxable_amount"], po_tax["taxable_amount"]
    base_diff = round(inv_base - po_base, 4)
    if abs(base_diff) > 0.005:
        differences.append({"field": "taxable_amount", "invoice_value": inv_base,
                             "po_value": po_base, "difference": f"{base_diff:+.4f}"})
        reasons.append(
            f"Taxable base differs by {base_diff:+.4f}. Possible causes: price changes, "
            "additional charges, freight costs, or quantity amendments after PO was raised."
        )

    if invoice_tax["tax_type"] and po_tax["tax_type"]:
        if invoice_tax["tax_type"].lower() != po_tax["tax_type"].lower():
            differences.append({"field": "tax_type", "invoice_value": invoice_tax["tax_type"],
                                 "po_value": po_tax["tax_type"], "difference": "Type mismatch"})
            reasons.append(
                f"Tax type changed from '{po_tax['tax_type']}' (PO) to '{invoice_tax['tax_type']}' (Invoice). "
                "This could indicate a tax classification change or vendor error."
            )

    return {
        "has_differences": len(differences) > 0,
        "summary_differences": differences,
        "reasons": reasons,
        "line_item_differences": _compare_line_items(invoice_tax["line_items"], po_tax["line_items"]),
        "invoice_tax_summary": {
            "tax_rate": f"{invoice_tax['tax_rate']}%",
            "tax_amount": invoice_tax["tax_amount"],
            "taxable_amount": invoice_tax["taxable_amount"],
            "tax_type": invoice_tax["tax_type"],
            "total_amount": invoice_tax["total_amount"],
        },
        "po_tax_summary": {
            "tax_rate": f"{po_tax['tax_rate']}%",
            "tax_amount": po_tax["tax_amount"],
            "taxable_amount": po_tax["taxable_amount"],
            "tax_type": po_tax["tax_type"],
            "total_amount": po_tax["total_amount"],
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# MCP TOOLS  (registered via @mcp.tool decorator)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def load_invoice(file_path: str, document_id: str = "") -> str:
    """
    Load an Invoice from a JSON or XML file into the document store.

    Args:
        file_path:   Absolute or relative path to the invoice file (.json or .xml).
        document_id: Optional ID to reference this document later. Defaults to the filename stem.
    """
    doc_id = document_id or Path(file_path).stem
    doc = _load_document(file_path)
    _store["invoices"][doc_id] = doc
    return json.dumps({"status": "ok", "message": f"Invoice '{doc_id}' loaded successfully.", "doc_id": doc_id})


@mcp.tool()
def load_po(file_path: str, document_id: str = "") -> str:
    """
    Load a Purchase Order from a JSON or XML file into the document store.

    Args:
        file_path:   Absolute or relative path to the PO file (.json or .xml).
        document_id: Optional ID to reference this document later. Defaults to the filename stem.
    """
    doc_id = document_id or Path(file_path).stem
    doc = _load_document(file_path)
    _store["purchase_orders"][doc_id] = doc
    return json.dumps({"status": "ok", "message": f"PO '{doc_id}' loaded successfully.", "doc_id": doc_id})


@mcp.tool()
def compare_tax(invoice_id: str, po_id: str) -> str:
    """
    Compare tax details between a loaded Invoice and a loaded PO.
    Returns a structured JSON diff covering tax rate, tax amount, taxable base,
    tax type, and line-item level differences.

    Args:
        invoice_id: ID of the loaded invoice (from load_invoice).
        po_id:      ID of the loaded purchase order (from load_po).
    """
    invoice = _store["invoices"].get(invoice_id)
    po      = _store["purchase_orders"].get(po_id)
    if not invoice:
        raise ValueError(f"Invoice '{invoice_id}' not found. Call load_invoice first.")
    if not po:
        raise ValueError(f"PO '{po_id}' not found. Call load_po first.")
    result = _compare_taxes(_extract_tax_info(invoice), _extract_tax_info(po))
    return json.dumps(result, indent=2)


@mcp.tool()
def explain_differences(invoice_id: str, po_id: str) -> str:
    """
    Compare tax fields between an Invoice and a PO and return a human-readable
    report with business and compliance reasons for every discrepancy found.

    Args:
        invoice_id: ID of the loaded invoice (from load_invoice).
        po_id:      ID of the loaded purchase order (from load_po).
    """
    invoice = _store["invoices"].get(invoice_id)
    po      = _store["purchase_orders"].get(po_id)
    if not invoice:
        raise ValueError(f"Invoice '{invoice_id}' not found.")
    if not po:
        raise ValueError(f"PO '{po_id}' not found.")

    result = _compare_taxes(_extract_tax_info(invoice), _extract_tax_info(po))

    lines = [
        "TAX COMPARISON REPORT",
        "=" * 50,
        f"Invoice ID : {invoice_id}",
        f"PO ID      : {po_id}",
        "",
        "INVOICE TAX SUMMARY",
        f"  Tax Rate      : {result['invoice_tax_summary']['tax_rate']}",
        f"  Tax Amount    : {result['invoice_tax_summary']['tax_amount']}",
        f"  Taxable Base  : {result['invoice_tax_summary']['taxable_amount']}",
        f"  Tax Type      : {result['invoice_tax_summary']['tax_type'] or 'N/A'}",
        f"  Total Amount  : {result['invoice_tax_summary']['total_amount']}",
        "",
        "PO TAX SUMMARY",
        f"  Tax Rate      : {result['po_tax_summary']['tax_rate']}",
        f"  Tax Amount    : {result['po_tax_summary']['tax_amount']}",
        f"  Taxable Base  : {result['po_tax_summary']['taxable_amount']}",
        f"  Tax Type      : {result['po_tax_summary']['tax_type'] or 'N/A'}",
        f"  Total Amount  : {result['po_tax_summary']['total_amount']}",
        "",
    ]

    if not result["has_differences"]:
        lines.append("✅  NO TAX DIFFERENCES FOUND — Invoice and PO tax values match.")
    else:
        lines.append(f"⚠️   {len(result['summary_differences'])} DIFFERENCE(S) FOUND")
        lines.append("")
        for i, diff in enumerate(result["summary_differences"], 1):
            lines += [
                f"Difference #{i}: {diff['field'].upper().replace('_', ' ')}",
                f"  Invoice : {diff['invoice_value']}",
                f"  PO      : {diff['po_value']}",
                f"  Delta   : {diff['difference']}",
                "",
            ]
        lines += ["REASONS & ANALYSIS", "-" * 40]
        for i, reason in enumerate(result["reasons"], 1):
            lines += [f"{i}. {reason}", ""]

        if result["line_item_differences"]:
            lines += ["LINE ITEM DIFFERENCES", "-" * 40]
            for ld in result["line_item_differences"]:
                lines.append(f"  Line {ld['line']}: {ld.get('description', '')} {ld.get('issue', '')}")
                for field, vals in ld.get("differences", {}).items():
                    lines.append(f"    {field}: Invoice={vals['invoice']}  PO={vals['po']}")
            lines.append("")

    return "\n".join(lines)


@mcp.tool()
def list_documents() -> str:
    """List all currently loaded invoices and purchase orders in the document store."""
    return json.dumps({
        "invoices":        list(_store["invoices"].keys()),
        "purchase_orders": list(_store["purchase_orders"].keys()),
    }, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# MCP RESOURCES  (registered via @mcp.resource decorator)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.resource("invoice://{doc_id}")
def get_invoice_resource(doc_id: str) -> str:
    """Expose a loaded invoice as an MCP resource at invoice://{doc_id}"""
    doc = _store["invoices"].get(doc_id)
    if not doc:
        raise ValueError(f"Invoice '{doc_id}' not found. Load it first with load_invoice.")
    return json.dumps(doc, indent=2)


@mcp.resource("po://{doc_id}")
def get_po_resource(doc_id: str) -> str:
    """Expose a loaded purchase order as an MCP resource at po://{doc_id}"""
    doc = _store["purchase_orders"].get(doc_id)
    if not doc:
        raise ValueError(f"PO '{doc_id}' not found. Load it first with load_po.")
    return json.dumps(doc, indent=2)


  # exposes an HTTP endpoint instead of stdio