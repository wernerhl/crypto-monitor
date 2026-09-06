"""Symbol normalisation shared by the venue adapters.

Venues prefix or suffix contract multipliers ("1000PEPEUSDT", "1MBABYDOGEUSDT",
"SHIB1000USDT"); Kraken uses legacy codes (XBT, XDG). Every adapter reports a normalised
`base` and a numeric `multiplier` so that OI in contracts converts to base units as
`oi_contracts * multiplier` (Binance/Bybit) or via `ctVal` (OKX).
"""

from __future__ import annotations

import re

KRAKEN_ALIASES = {
    "XBT": "BTC",
    "XDG": "DOGE",
    "XXBT": "BTC",
    "XETH": "ETH",
    "XXDG": "DOGE",
    "XLTC": "LTC",
    "XXRP": "XRP",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XZEC": "ZEC",
    "XETC": "ETC",
    "XREP": "REP",
    "XMLN": "MLN",
}
_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M|1MM)(?=[A-Z])")
_SUFFIX = re.compile(r"(1000000|100000|10000|1000)$")
_MULT = {"1M": 1_000_000.0, "1MM": 1_000_000.0}


def split_multiplier(base: str) -> tuple[str, float]:
    """'1000PEPE' -> ('PEPE', 1000.0); 'SHIB1000' -> ('SHIB', 1000.0); 'BTC' -> ('BTC', 1.0)."""
    m = _PREFIX.match(base)
    if m:
        tok = m.group(1)
        return base[m.end() :], _MULT.get(tok, float(tok) if tok.isdigit() else 1.0)
    m = _SUFFIX.search(base)
    if m and len(base) > len(m.group(1)):
        return base[: m.start()], float(m.group(1))
    return base, 1.0


def kraken_base(code: str) -> str:
    return KRAKEN_ALIASES.get(
        code,
        code[1:] if len(code) == 4 and code[0] in "XZ" and code not in KRAKEN_ALIASES else code,
    )
