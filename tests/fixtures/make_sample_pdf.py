"""Generate a small test PDF: headings mid-page, running header/footer, optional outline (TOC).

    python tests/fixtures/make_sample_pdf.py            -> tests/fixtures/sample_toc.pdf + sample_notoc.pdf
"""

from pathlib import Path

import pymupdf

LOREM = (
    "Collected water should be inspected regularly for colour and smell. Households that plan the system "
    "carefully usually recover the installation cost within a few seasons. Local climate, roof material and "
    "family size all change the right design, so each choice below explains the trade-offs involved."
)

# Each page: list of (kind, text) where kind is h1 | h2 | p.
PAGES = [
    [("h1", "1 Introduction"),
     ("p", "Rainwater harvesting means collecting rain from a roof and storing it for later use. " + LOREM),
     ("h2", "1.1 Why harvest rainwater"),
     ("p", "It reduces dependence on municipal supply and lowers groundwater extraction. " + LOREM)],
    [("p", "In dry months a full tank can supply toilets and gardens for several weeks. " + LOREM),
     ("h2", "1.2 Legal status"),
     ("p", "Under the Water Act 2019, households may use harvested rainwater for up to 10% of their "
           "drinking supply after certified filtration. Non-potable use has no limit. " + LOREM)],
    [("h1", "2 System Components"),
     ("p", "Every system has four parts: catchment, conveyance, first-flush diversion and storage. " + LOREM),
     ("h2", "2.1 Catchment"),
     ("p", "Metal roofs give the cleanest runoff, with a runoff coefficient near 0.9. Clay tiles reach about "
           "0.8 and thatch only 0.3. " + LOREM)],
    [("h2", "2.2 First-flush diverter"),
     ("p", "The first-flush diverter discards the first 2 litres per square metre of roof, which carries "
           "dust, leaves and bird droppings. " + LOREM),
     ("h2", "2.3 Filters"),
     ("p", "A 200 micron mesh filter protects the tank inlet; a sand filter is needed for drinking use. " + LOREM)],
    [("h1", "3 Design"),
     ("p", "Design starts from how much water falls on the roof and how much the household needs. " + LOREM),
     ("h2", "3.1 Storage tank sizing"),
     ("p", "Tank capacity in litres equals roof area in square metres times annual rainfall in millimetres "
           "times the runoff coefficient. For a 50 square metre metal roof and 800 mm of rain this gives "
           "36,000 litres per year. " + LOREM)],
    [("h2", "3.2 Maintenance schedule"),
     ("p", "Clean gutters monthly in the rainy season, flush the diverter after every storm and disinfect "
           "the tank once a year with chlorine at 5 mg per litre. " + LOREM),
     ("h1", "4 Conclusion"),
     ("p", "A well designed system is cheap, simple and pays for itself. " + LOREM)],
]

CSS = "h1 {font-size: 20px; font-weight: bold;} h2 {font-size: 15px; font-weight: bold;} p {font-size: 11px;}"


def build(path: Path, with_toc: bool) -> None:
    doc = pymupdf.open()
    toc = []
    for number, blocks in enumerate(PAGES, start=1):
        page = doc.new_page()
        page.insert_text((72, 40), "Rainwater Harvesting Guide - Draft 2", fontsize=8)
        page.insert_text((280, 810), f"Page {number}", fontsize=8)
        html = "".join(f"<{kind}>{text}</{kind}>" for kind, text in blocks)
        page.insert_htmlbox(pymupdf.Rect(72, 60, 523, 780), html, css=CSS)
        toc += [[1 if kind == "h1" else 2, text, number] for kind, text in blocks if kind != "p"]
    if with_toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": "Rainwater Harvesting Guide"})
    doc.save(path)


if __name__ == "__main__":
    here = Path(__file__).parent
    build(here / "sample_toc.pdf", with_toc=True)
    build(here / "sample_notoc.pdf", with_toc=False)
    print("wrote", here / "sample_toc.pdf", "and", here / "sample_notoc.pdf")
