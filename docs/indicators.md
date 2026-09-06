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

## Positioning (notes Section 4)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| `funding_ann` | eq. 4.1 | OI-weighted Σ_v FR_v × (24/interval_v) × 365 (3 × 365 × FR for 8-hour funding) | `perp_snapshot` | `positioning.aggregate_funding` | `test_positioning.py::test_aggregate_funding_*` (0.136875) |
| `z_fr` | eq. 4.2 | (FR − median₉₀)/(1.4826·MAD₉₀), walk-forward on daily means | `funding_daily` | `positioning.robust_z` | `test_robust_z_hand_value` (30/14.826) |
| `oi_rel`, `oi_rel_pctile` | §4.2 | OI$/MarketCap; trailing-90-day percentile rank | `perp_snapshot`, `markets` | `positioning.oi_relative`, `percentile_rank` | `test_oi_relative_and_percentile_rank` |
| `quadrant`, `oi_price_slope_20d` | §4.2 table | signs of Δlog P, Δlog OI over 5 d; slope of Δlog OI on Δlog P over 20 d | `funding_daily`, `prices_daily` | `positioning.oi_price_quadrant` | `test_quadrant_and_slope` (slope 2.0) |
| `lambda_minus_2`, `lambda_plus_2` | eq. 4.3–4.4 | atoms from positive daily OI increments at the day's close, split by long share and the assumed leverage distribution (`thresholds.yaml → positioning`), P_liq = P0(1 ∓ 1/ℓ ± m); Λ^± integrates within ±κσ√h | `funding_daily`, `prices_daily`, `long_short` | `positioning.liquidation_density`, `liquidation_mass` | `test_liquidation_prices_and_mass` (300 / 200) |
| `iv_1w/1m/3m`, `rr25_1m`, `term_slope` | §4.4 | per-expiry ATM IV and Black-76 25Δ call/put IV interpolated in delta; term points interpolated in time | `options` | `positioning.options_metrics` | `test_realised_var_and_options_metrics` |
| `vrp` | §4.4 | IV²₁ₘ − (365/30) Σ₃₀ f² | `options`, `prices_daily` | `positioning.realised_var_30` | same (0.25 − 0.146) |
| `delta_est` | adapter | Black-76 delta from `mark_iv` (Deribit summary has no greeks) | `options` | `fetch.deribit.black76_delta` | `test_black76_delta_matches_deribit_greeks_fixture` (0.976 vs 0.97619) |

## Liquidity (notes Section 7)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| `depth_usd` per venue | Def. 7.1 | Σ p·q within ±2 % of mid; coverage and truncation flags | raw books | `liquidity.depth_from_levels` | `test_depth_from_levels_*` (709) |
| `depth_2pct_usd` | Def. 7.1 | intraday median of Σ_v D_v(0.02) | `orderbook_depth` | `liquidity.aggregate_depth` | `test_aggregate_depth_*` (150) |
| `benford_chi2`, `benford_dev` | §7 | χ²₈ of first digits of trade sizes (base units) vs Benford; `dev = χ²/n` is compared with the reference venues (Coinbase, Kraken): fail when > 3× their median | `trade_stats` | `liquidity.benford_stats` | `test_benford_stats_*` |
| `vol_absret_corr` | §7 | corr(hourly volume, |Δlog close|) over 7 days | `prices_hourly` | `liquidity.volume_absret_corr` | (integration) |
| wash filter `pass` | §7 | three tests: volume/depth ≤ 5× reference median (skipped on truncated books); Benford deviation ≤ 3× reference; corr(volume, |r|) > 0. A venue fails when ≥ 2 tests fail | `wash_filters` | `liquidity.wash_filters` | `test_wash_filters_flags` |
| `adv_real_usd` | §5, §7 | 30-day mean of Σ volume over passing venues | `prices_daily`, `wash_filters` | `liquidity.real_adv` | (integration) |
| `amihud` | §7 | mean(|r_d| / volume_d) over 30 d | `prices_daily` | `liquidity.amihud` | `test_amihud_hand_value` |
| `impact_cost`, `dtl_days`, `gate_pass` | eq. 7.2–7.3, Rule 7.1 | s/2 + Yσ√(Q/ADV); Q/(ρ ADV); DTL ≤ 3 d and 2·cost ≤ ¼ net return | `liquidity` | `liquidity.gate` | `test_impact_cost_and_dtl_formulas` (0.0065, 0.4 d) |

## Market state (notes Section 3)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| `phi` | Def. 3.1 | ⅕ Σ of five 250-day robust z components; mean of the available ones with `n_components` | `fragility` | `state.fragility_index` | `test_fragility_index_mean_of_available_components` |
| `z_dd` input | Def. 3.1 | −(P_t / max₉₀ P − 1) | `prices_daily` | `state.drawdown_from_high` | `test_drawdown_from_high` |
| `net_liquidity` | §3.3 | WALCL/1e3 − WTREGEN/1e3 − RRPONTSYD (USD bn) | `macro` | `state.net_liquidity` | `test_net_liquidity_units` (5768.594) |
| `p_high` | eq. 3.3 | two-state Markov switching on log RV, filtered probabilities, 1/(1 − p_jj) | `vol_state` | `state.vol_state_model` | `test_vol_state_model_on_synthetic_two_regime_series` |

## Rules
| rule | function | test |
|---|---|---|
| 4.1 crowded long, 4.2 capitulation, 4.3 vol underpricing, 5.1 cliff, 7.1 gate | `monitor.rules.*` (thresholds only from `config/thresholds.yaml`) | `test_state_rules.py::test_rules_fire_and_report_unavailable_inputs` |

_(Sections 5, 6, 8 and 9 of the notes are added in phases 4–5.)_
