"""Capture dashboard screenshots from a running app (dev tool; needs `pip install playwright` and a local Chrome).

    streamlit run app/streamlit_app.py --server.port 8765
    python scripts/screenshots.py http://localhost:8765
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def wait_idle(page, ms=1500):
    page.wait_for_selector("[data-testid='stStatusWidget']", state="detached", timeout=120_000)
    page.wait_for_timeout(ms)


def main(url: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 2100}, device_scale_factor=1)
        page.goto(url)
        page.wait_for_selector(".js-plotly-plot", timeout=120_000)
        page.wait_for_function("document.querySelectorAll('.js-plotly-plot').length >= 4", timeout=120_000)
        wait_idle(page, 2500)
        page.screenshot(path=OUT / "overview.png", full_page=True)

        page.get_by_role("tab", name="Scorecards").click()
        wait_idle(page)
        page.screenshot(path=OUT / "scorecard.png", full_page=True)

        page.get_by_role("tab", name="Ask a question").click()
        wait_idle(page)
        box = page.get_by_label("Question", exact=True).and_(page.locator("input"))
        for name, q in [("ask_answer.png", "Compare hourly vs salaried turnover at Parks in 2025"),
                        ("ask_refusal.png", "What is John Smith's salary?")]:
            box.fill(q)
            box.press("Enter")
            wait_idle(page, 2000)
            page.screenshot(path=OUT / name, full_page=True)

        page.get_by_role("tab", name="Data quality").click()
        wait_idle(page)
        page.screenshot(path=OUT / "data_quality.png", full_page=True)
        browser.close()
    print("wrote", sorted(x.name for x in OUT.glob("*.png")))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765")
