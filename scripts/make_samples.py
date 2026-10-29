"""Generate demo documents (PDF, DOCX, MD) into ./samples without extra dependencies."""

from pathlib import Path

import docx

OUT = Path(__file__).resolve().parent.parent / "samples"

REPORT_PAGES = [
    [
        "Northwind Analytics - Q3 2026 Financial Report",
        "Revenue for the third quarter was 18.4 million USD, up 12 percent year over year.",
        "Gross margin improved to 64 percent thanks to lower cloud infrastructure costs.",
        "Operating expenses were 9.1 million USD, driven mainly by hiring in engineering.",
        "The company ended the quarter with 42 million USD in cash and no debt.",
    ],
    [
        "Outlook and Risks",
        "Management expects Q4 revenue between 19 and 20 million USD.",
        "The main risk is customer concentration: the top three customers represent 38 percent of revenue.",
        "A currency hedge program will start in January 2027 to reduce exposure to the euro.",
        "The board approved a share buyback of up to 5 million USD over the next twelve months.",
    ],
]

CONTRACT_MD = """# Master Services Agreement

This Master Services Agreement is entered into on 1 March 2026 between Acme Corp ("Client")
and Globex Consulting Ltd ("Provider").

## 1. Term
The agreement has an initial term of 24 months and renews automatically for successive
12 month periods unless either party gives notice of non-renewal.

## 2. Fees and Payment
The Client pays a monthly retainer of 15,000 USD. Invoices are due within 45 days of receipt.
Late payments accrue interest at 1.5 percent per month.

## 3. Termination
Either party may terminate this agreement for convenience with 30 days written notice.
Either party may terminate immediately for material breach that is not cured within 10 days.

## 4. Confidentiality
Both parties must keep confidential information secret for 5 years after termination.

## 5. Liability
The Provider's total liability is capped at the fees paid in the previous 12 months.
"""

HANDBOOK = [
    ("Employee Handbook - Leave Policy", "Title"),
    ("Annual Leave", "Heading 1"),
    ("Full-time employees receive 25 days of paid annual leave per calendar year.", None),
    ("Up to 5 unused days can be carried over to the next year and must be used by 31 March.", None),
    ("Sick Leave", "Heading 1"),
    ("Employees are entitled to 10 days of paid sick leave. A medical certificate is required after 3 consecutive days.", None),
    ("Parental Leave", "Heading 1"),
    ("Primary caregivers receive 16 weeks of fully paid parental leave; secondary caregivers receive 6 weeks.", None),
    ("Remote Work", "Heading 1"),
    ("Employees may work remotely up to 3 days per week with manager approval.", None),
]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], path: Path) -> None:
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, lines in enumerate(pages):
        content = "BT /F1 11 Tf 50 780 Td 15 TL " + " ".join(f"({_escape(l)}) Tj T*" for l in lines) + " ET"
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        data = content.encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def make_docx(path: Path) -> None:
    d = docx.Document()
    for text, style in HANDBOOK:
        d.add_paragraph(text, style=style) if style else d.add_paragraph(text)
    d.save(str(path))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    make_pdf(REPORT_PAGES, OUT / "q3_financial_report.pdf")
    make_docx(OUT / "employee_handbook.docx")
    (OUT / "services_agreement.md").write_text(CONTRACT_MD)
    print(f"samples written to {OUT}")


if __name__ == "__main__":
    main()
