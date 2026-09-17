"""Rendered-page check (work order 6, H1): load the deployed page in headless Chromium, assert
that the seven panels each carry more than 500 characters of text, that no console error or
page error fired, and save a screenshot. Exit 1 on any failure.

    python scripts/check_rendered_page.py https://wernerhl.github.io/crypto-monitor/ shot.png
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    url = argv[0] if argv else "https://wernerhl.github.io/crypto-monitor/"
    shot = argv[1] if len(argv) > 1 else "rendered-page.png"
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.on(
            "console",
            lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(url, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(4000)
        lengths = page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('section.panel')].map(s => [s.id, (s.innerText||'').length]))"
        )
        page.screenshot(path=shot, full_page=True)
        browser.close()
    short = {k: v for k, v in lengths.items() if v <= 500}
    print(f"panels: {lengths}")
    ok = len(lengths) >= 8 and not short and not errors
    if len(lengths) < 8:
        print(f"::error::only {len(lengths)} panels found (expected 8)")
    if short:
        print(f"::error::panels with ≤ 500 characters: {short}")
    for e in errors:
        print(f"::error::{e}")
    print("rendered page ok" if ok else "rendered page FAILED", f"(screenshot {shot})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
