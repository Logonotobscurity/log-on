"""Generate the fictional sample business PDF used by Phase 2 (deterministic; no network).

    python scripts/make_sample_pdf.py            # -> samples/lagoon_freight_portfolio_fy2026.pdf

Everything in it is invented: companies, people, emails (.example domains) and phone numbers.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent.parent / "samples" / "lagoon_freight_portfolio_fy2026.pdf"

CUSTOMERS = [
    ["Customer ID", "Customer", "Sector", "City", "Account Manager"],
    ["CUS-001", "Zuma Cement Plc", "Construction", "Abuja", "Ifeoma Nwosu"],
    ["CUS-002", "Okoya Breweries Ltd", "FMCG", "Lagos", "Kunle Adebayo"],
    ["CUS-003", "Delta Agro Exports Ltd", "Agriculture", "Warri", "Ifeoma Nwosu"],
    ["CUS-004", "Kano Textiles Ltd", "Manufacturing", "Kano", "Musa Bello"],
    ["CUS-005", "Eko Pharma Distribution Ltd", "Healthcare", "Lagos", "Kunle Adebayo"],
    ["CUS-006", "Sahel Telecom Towers Ltd", "Telecoms", "Kaduna", "Musa Bello"],
]
CONTRACTS = [
    ["Contract ID", "Customer", "Service", "Value (NGN m)", "Start", "Expiry"],
    ["LF-C-2401", "Zuma Cement Plc", "Bulk haulage", "420.0", "2024-02-01", "2027-01-31"],
    ["LF-C-2402", "Okoya Breweries Ltd", "Cold-chain distribution", "310.5", "2024-04-15", "2027-04-14"],
    ["LF-C-2403", "Delta Agro Exports Ltd", "Port clearing and export", "185.0", "2025-03-01", "2027-02-28"],
    ["LF-C-2404", "Kano Textiles Ltd", "Warehousing", "96.0", "2025-07-01", "2026-12-31"],
    ["LF-C-2405", "Eko Pharma Distribution Ltd", "Temperature-controlled last mile", "240.0", "2025-01-01", "2027-03-31"],
    ["LF-C-2406", "Sahel Telecom Towers Ltd", "Diesel and equipment logistics", "150.0", "2024-10-01", "2027-09-30"],
    ["LF-C-2407", "Okoya Breweries Ltd", "Empties return", "58.0", "2026-01-01", "2027-03-15"],
]
SUPPLIERS = [
    ["Supplier ID", "Supplier", "Category", "City"],
    ["SUP-01", "Harmattan Haulage Ltd", "Trucking", "Kano"],
    ["SUP-02", "Atlantic Reefers Nigeria Ltd", "Refrigerated fleet", "Lagos"],
    ["SUP-03", "Onne Port Services Ltd", "Port clearing", "Port Harcourt"],
    ["SUP-04", "Arewa Warehousing Ltd", "Warehousing", "Kano"],
    ["SUP-05", "Coldline Couriers Ltd", "Last-mile cold chain", "Lagos"],
    ["SUP-06", "PetroMove Energy Ltd", "Diesel supply", "Kaduna"],
]
ALLOCATIONS = [
    ["Supplier", "Contract", "Role"],
    ["Harmattan Haulage Ltd", "LF-C-2401", "Primary haulier"],
    ["Harmattan Haulage Ltd", "LF-C-2406", "Equipment moves"],
    ["Atlantic Reefers Nigeria Ltd", "LF-C-2402", "Reefer trucks"],
    ["Atlantic Reefers Nigeria Ltd", "LF-C-2405", "Line-haul reefers"],
    ["Onne Port Services Ltd", "LF-C-2403", "Customs and port clearing"],
    ["Arewa Warehousing Ltd", "LF-C-2404", "Bonded storage"],
    ["Coldline Couriers Ltd", "LF-C-2405", "Last-mile riders"],
    ["PetroMove Energy Ltd", "LF-C-2406", "Diesel supply"],
]
CONTACTS = [  # PII on purpose: ingestion must not leak it into the graph or memory
    ["Name", "Organisation", "Email", "Phone"],
    ["Chidi Eze", "Zuma Cement Plc", "chidi.eze@zumacement.example", "0803 555 0142"],
    ["Halima Sani", "Harmattan Haulage Ltd", "halima@harmattanhaulage.example", "+234 809 555 0177"],
    ["Bisi Lawal", "Eko Pharma Distribution Ltd", "b.lawal@ekopharma.example", "0705 555 0123"],
]


def _table(rows, widths):
    t = Table(rows, colWidths=[w * mm for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def build(out: Path = OUT) -> Path:
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=16)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, spaceBefore=8)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
    P = lambda t: Paragraph(t, body)  # noqa: E731
    s = []
    s += [Paragraph("Lagoon Freight &amp; Logistics Ltd: Commercial Portfolio Review FY2026", h1),
          P("Prepared by the Commercial Office, Lagos. Position as at 31 October 2026. Fictional sample document "
            "for the LOG_ON project; all companies, people and contact details are invented."),
          Paragraph("1. Executive Summary", h2),
          P("Lagoon Freight &amp; Logistics Ltd is headquartered in Lagos. The company manages seven active customer "
            "contracts worth NGN 1,459.5m in total. Four contracts, worth NGN 903.0m, expire in Q1 2027 and are the "
            "priority for renewal. Supplier concentration is moderate: Atlantic Reefers Nigeria Ltd and Harmattan "
            "Haulage Ltd each support more than one contract."),
          Paragraph("2. Customer Portfolio", h2),
          _table(CUSTOMERS, [24, 52, 28, 22, 34]), Spacer(1, 4 * mm),
          P("Sahel Telecom Towers Ltd is a subsidiary of Zuma Cement Plc. Eko Pharma Distribution Ltd is "
            "headquartered in Lagos and is the fastest-growing account."),
          PageBreak(),
          Paragraph("3. Contract Register", h2),
          _table(CONTRACTS, [21, 44, 47, 25, 19, 19]), Spacer(1, 4 * mm),
          P("All values are annual contract values in NGN millions, excluding VAT."),
          Paragraph("4. Supplier Register", h2),
          _table(SUPPLIERS, [24, 56, 40, 30]),
          PageBreak(),
          Paragraph("5. Supplier Allocations", h2),
          _table(ALLOCATIONS, [56, 30, 60]), Spacer(1, 4 * mm),
          P("Harmattan Haulage Ltd also provides inland trucking to Delta Agro Exports Ltd under contract LF-C-2403. "
            "Atlantic Reefers Nigeria Ltd handles empties return for Okoya Breweries Ltd under contract LF-C-2407. "
            "Atlantic Reefers Nigeria Ltd subcontracts overflow capacity to Coldline Couriers Ltd."),
          Paragraph("6. Key Contacts", h2),
          _table(CONTACTS, [30, 50, 56, 32]),
          Paragraph("7. Risks and Renewal Notes", h2),
          P("Contract LF-C-2404 with Kano Textiles Ltd expires on 31 December 2026 and will not be renewed on current "
            "terms. Renewal talks for LF-C-2401 are led by Ifeoma Nwosu. Diesel price volatility affects PetroMove "
            "Energy Ltd and therefore contract LF-C-2406.")]
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=A4, title="Lagoon Freight Commercial Portfolio Review FY2026",
                            author="LOG_ON sample generator", leftMargin=18 * mm, rightMargin=18 * mm)
    doc.build(s)
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
