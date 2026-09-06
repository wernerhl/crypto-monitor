"""Sector seeding from CoinGecko categories (notes §9.2 sector list). Ordered rules: the
first match wins; unmatched -> 'other'. The seed is written into config/universe.yaml and then
reviewed by hand (governance: the review date is recorded)."""

from __future__ import annotations

import re

RULES: list[tuple[str, str]] = [
    # exact category names where possible (CoinGecko tags are many and overlapping)
    ("stablecoin", r"^(Stablecoins|.* Stablecoin)$"),
    ("memecoin", r"^Meme$"),
    ("derivatives-protocol", r"^(Derivatives|Perpetuals|Options)$"),
    ("DEX", r"^Decentralized Exchange \(DEX\)$|^Automated Market Maker \(AMM\)$|^DEX Aggregator$"),
    ("DeFi-lending", r"^Lending|^CDP|Lending/Borrowing"),
    ("L2", r"^Layer 2 \(L2\)$|^Rollup|^Ethereum Layer 2"),
    ("L1", r"^Layer 1 \(L1\)$"),
    (
        "oracle-infra",
        r"^Oracle$|^Infrastructure$|^Cross-chain Communication$|^Data Availability$|^Interoperability$|^Bridge|^Storage$|^Indexing$",
    ),
    ("exchange-token", r"^Centralized Exchange \(CEX\) Token$"),
    ("RWA", r"^Real World Assets \(RWA\)$|^Tokenized"),
    (
        "AI-compute",
        r"^Artificial Intelligence \(AI\)$|^AI Agents|^DePIN$|^GPU|^Compute$|^Decentralized Compute",
    ),
    ("gaming", r"^Gaming|^GameFi|^Metaverse|^Play To Earn|^NFT$"),
    ("payments", r"^Payments$|^Remittance"),
]


def assign(categories: list[str]) -> str:
    for sector, pat in RULES:
        if any(re.search(pat, c.strip()) for c in (categories or [])):
            return sector
    return "other"
