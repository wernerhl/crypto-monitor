# Indicators

One row per computed quantity: formula (equation in the notes), source table, function and
the unit test whose comments carry the hand-computed expected value.

## Universe (notes Section 2)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| exclusion flag | §2 | CoinGecko category membership lists (`category_members`) and per-coin category regexes in `config/universe.yaml` (stablecoin; wrapped / LST; exchange-internal; manual); missing tags → `meta_status = pending`, not excluded | `category_members`, `coin_meta`, `markets` | `compute.universe.classify_exclusions` | `test_universe.py::test_exclusions`, `test_category_membership_lists_exclude_without_per_coin_meta` |
| venue symbol map | §2 | upper-case symbol matched to live listings with an allowed quote; higher rank wins ties; `symbol_map` overrides | `venue_listings` | `compute.universe.resolve_symbols` | `test_universe.py::test_resolve_symbols_*` |
| `oi_median_usd` | §2 Tier 1 | median over trailing 30 days of Σ_venues OI_USD (one snapshot per venue-day, the last) | `perp_snapshot` | `compute.universe.oi_metrics` | `test_universe.py::test_oi_median_and_window` (150) |
| `adv_30d_usd` | §2 Tier 2, §7 | mean over trailing 30 days of Σ_spot venues quote volume (reported; `adv_basis = reported` until wash filters) | `prices_daily` | `compute.universe.adv_metrics` | `test_universe.py::test_adv_uses_quote_volume_or_base_times_close` (30) |
| `tier` | §2 | rules in `config/universe.yaml`; depth condition `pending_phase3` | above | `compute.universe.tier` | `test_universe.py::test_tiering_rules`, `test_no_stablecoin_or_wrapped_asset_in_any_tier` |

## Derivative units (adapters)
| quantity | venue | conversion | test |
|---|---|---|---|
| `oi_usd` | Binance USDT-M | `openInterest` (base units) × `markPrice` | `test_parsers.py::test_binance_perps_parse_units` |
| `oi_usd` | Bybit | `openInterestValue` as reported | `test_parsers.py::test_bybit_perps_parse_uses_oi_value` |
| `oi_usd` | OKX | `oiUsd` as reported; `oiCcy` = base units | `test_parsers.py::test_okx_perps_parse` |
| `multiplier` | Binance/Bybit | `1000PEPE → (PEPE, 1000)`, `SHIB1000 → (SHIB, 1000)` | `test_parsers.py::test_split_multiplier` |

_(Sections 3–9 of the notes are added in phases 3–5.)_
