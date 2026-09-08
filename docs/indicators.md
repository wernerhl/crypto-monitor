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
| `phi` (live and history) | Def. 3.1 | one builder for both: `compute.fragility.build_series` writes `fragility_series`; the live row is its last row, so live == history on every common date. Every component carries `z_<c>_n` (finite input days in the 250-day window). A component that is null while its source is fresh fails the job (`check_components`); a stale source is listed as a gap in the reading. VRP comes from the daily DVOL history plus today's live DVOL (`live_vrp_history`), so the component never lags the chain. | `fragility`, `fragility_series` | `compute.fragility.build_series` | `test_all_five_fresh_sources_yield_five_components_with_sizes`, `test_null_component_with_fresh_source_is_a_build_failure` |
| `phi` | Def. 3.1 | ⅕ Σ of five 250-day robust z components; mean of the available ones with `n_components` | `fragility` | `state.fragility_index` | `test_fragility_index_mean_of_available_components` |
| `z_dd` (range position) | work order 2, item 2 (replaces Def. 3.1 fourth component) | pos₉₀ = (P_t − min₉₀ P)/(max₉₀ P − min₉₀ P); z_dd = 4·(pos₉₀ − ½) ∈ [−2, 2], no standardisation. Top of the 90-day range is fragile (liquidation mass just below), bottom is calm. `dd_90` = P/max₉₀ − 1 is kept for the reading. Old form (robust z of P/max₉₀ − 1, bounded at zero) made any day near the high a tail event of its own downtrend. | `prices_daily` → `fragility_series` | `compute.fragility._drawdown`, `range_position_score` | `test_range_position_component_is_bounded_and_near_zero_in_a_narrow_range` |
| `net_liquidity` | §3.3 | WALCL/1e3 − WTREGEN/1e3 − RRPONTSYD (USD bn) | `macro` | `state.net_liquidity` | `test_net_liquidity_units` (5768.594) |
| `p_high` | eq. 3.3 | two-state Markov switching on log RV, filtered probabilities, 1/(1 − p_jj) | `vol_state` | `state.vol_state_model` | `test_vol_state_model_on_synthetic_two_regime_series` |

| `basis_history` | §5 (basis) | (F − I)/I · 365/days for Binance COIN-M CURRENT/NEXT_QUARTER continuous klines against the index price, daily since 2020-06 | `basis_history` | `backfill.parse_basis_history` | `test_quarterly_delivery_dates` |
| `basis_term` | §5 (basis) | current annualised basis by expiry and venue for BTC/ETH | hourly.json | `jobs_hourly._basis_term` | — |
| state reading | C1 | deterministic paragraph from the live rows (Φ, two largest components, VRP signs, front basis sign, OI percentile, 90-day and cycle drawdown, stablecoin growth, rules, venue breaches) | hourly.json `reading` | `compute.reading.state_reading` | `test_state_reading_is_deterministic_and_links_numbers` |

| `liq_coverage` | B1 / work order 2 item 5 | per websocket venue and hour: connected seconds / 3600 and message count from the collector; Rule 4.2's 30-day liquidation percentile uses only venue-hours with coverage ≥ 0.9; `liq_source` lists the venues with rows in the window (a connected but silent stream is not listed); `liq_coverage_share` = share of the last 30 days' hours fully covered | `liq_coverage`, `positioning` | `fetch.liq_ws.HourBuffer`, `jobs_hourly._covered_liquidations` | `test_liq_coverage_parser_and_covered_hours` |

## Rules
| rule | function | test |
|---|---|---|
| 4.1 crowded long, 4.2 capitulation, 4.3 vol underpricing, 7.1 gate | `monitor.rules.*` (thresholds only from `config/thresholds.yaml`) | `test_state_rules.py::test_rules_fire_and_report_unavailable_inputs` |
| 5.1 cliff — status `calendar` since review decision 1 (2026-09-08) | `rules.cliff` → `cliff_calendar` table (daily); feeds the event strip, ESP, dilution, gate and the rolling calendar issue; no hit-rate row, not a trigger | `test_rule_51_is_a_calendar_not_a_trigger` |
| 4.3 driver (analysis, review item) | `compute.rule43`: complacency = IV₁ₘ < trailing 250-day median; post-shock = RV₃₀ > trailing 250-day 90th percentile; both / neither; hit = RV₃₀,next > IV_flag; episodes = flags ≥ 30 days apart | `test_rule43_driver_classification` |
| Φ after shocks (analysis) | `compute.fragility_validation`: shocks = BTC log return < rolling 250-day 5th percentile; outcomes MDD 5/20 d, RV 20 d, days to recover ≤ 60; Newey–West slopes; Φ-matched placebo; tercile tables | `test_fragility_validation_shocks_and_outcomes` |

| 5.1 backfill | `compute.cliff_study` (hit = negative 14-day pre-cliff return; float backed out of the schedule; wash-filtered ADV where available, flagged otherwise; s.e. clustered by cliff week; β-adjusted return with the factor model's rolling β_MKT; placebo of 20 pseudo-cliffs per token and year; base rate on all tokens and on the Rule 5.1 tokens; by-year rows first) | `test_cliff_study_counts_hits_and_flags_bases`, `test_clustered_se_and_placebo` |
| alerts | `monitor.alerts` (one issue per Rule 4.x firing, venue breach or dataset unavailable two runs; ONE rolling "Cliff calendar, next 30 days" issue for Rule 5.1, refreshed when the list changes; `site/alerts.xml`) | `test_alerts_open_and_close_conditions_dry_run`, `test_rule_51_firings_collapse_into_one_calendar_condition` |

## Scheduled supply (notes Section 5)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| `esp_float` | eq. 5.1 | Σ_{τ∈(t,t+h]} Σ_c π_c U_{τ,c} / Float; π_c from `thresholds.yaml → supply.pi_c` (documented defaults; `pi_default_used` flags the `unknown` class) | `unlock_events`, `markets` | `supply.esp` | `test_supply_context.py::test_esp_float_and_days_of_volume` (0.009) |
| `esp_days_of_volume` | eq. 5.2 | P × Σ π_c U / ADV_real (falls back to reported ADV, flagged `adv_is_reported`) | + `liquidity` | `supply.esp` | same (6.0) |
| `dilution` | §5 | (Float_{t+365} − Float_t)/Float_t from scheduled unlocks + emissions | `unlock_events`, `markets` | `supply.dilution` | `test_dilution` (0.1005) |
| cliff inputs | Rule 5.1 | per cliff date: share of float, days of real volume | `unlock_events` | `supply.cliffs` | `test_cliffs_rule_inputs` |
| linear tranches | adapter | DefiLlama index gives (start, total); spread evenly over 400 d until the per-protocol detail is fetched | `unlock_events` | `supply.expand_linear` | `test_expand_linear_spreads_tranche_evenly` |

## Context (notes §3.3, §12)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| `net_liquidity` and 4-week changes | §3.3 | WALCL/1e3 − WTREGEN/1e3 − RRPONTSYD; change vs the observation ≥ 28 d earlier | `macro` (FRED CSV) | `context.macro_table` | `test_macro_table_net_liquidity_and_changes` (5768.594, −26.406) |
| `growth_30d` | Def. 3.1 (z^{SC−} input) | total USD stablecoin supply / 30 d earlier − 1 | `stablecoin_total` | `context.stablecoin_growth` | `test_stablecoin_growth` |
| event strip | §12 item 6 | cliffs, governance ends, manual events, options expiries within 28 d | `cliffs`, `proposals`, `events.yaml`, `options` | `context.event_strip` | `test_event_strip_orders_and_filters` |
| MVRV, exchange supply/flows, staking ratio | §5 | as reported (CoinMetrics community; ultrasound.money) | `onchain`, `eth_staking` | adapters | (fixture) |

## Trade structures, venue, sizing, cross-section (notes Sections 5, 6, 8, 9)
| indicator | notes ref | formula | source table | function | test |
|---|---|---|---|---|---|
| basis `gross_ann` | eq. 5.1 | (F − P)/P × 365/(T − t); cost = stablecoin borrow + 4 fees × 365/days | `futures_marks`, `prices_daily` | `trades.basis_table` | `test_risk.py::test_annualised_basis_and_table` (0.125 / 0.066) |
| funding carry | §5.2 | shrink × mean₃₀(funding_ann); cost = borrow + fees per monthly round trip | `funding_daily` | `trades.funding_carry` | `test_funding_carry_shrinks_trailing_mean` (0.05) |
| vol selling | §5.3 | IV₁ₘ − √RV²₃₀; FORBIDDEN when Rule 4.3 fires | `options_metrics`, `rule_fires` | `trades.vol_premium` | `test_vol_premium_forbidden_when_rule_43_fires` |
| unlock short | §5.4 | flagged cliffs 14–28 d ahead on Tier 1/2; cost = −funding if negative + fees; crowded-short warning z^FR < −1 | `cliffs`, `rule_fires`, `funding_daily` | `trades.unlock_short` | (integration) |
| venue score / limits | §6.1–6.2 | weighted points → grade A–D → x̄_v; X_v vs limits; low-score aggregate | `config/venues.yaml`, `wash_filters` | `venue.score_venue`, `exposure_table` | `test_venue_score_and_limits` (18 → A, breach) |
| stress covariance | eq. 8.2 | ξ Σ_high + (1−ξ) Σ_low + η Φ⁺ (Σ_high − Σ_low), Ledoit–Wolf state-weighted | `prices_daily`, `vol_state`, `fragility` | `covariance.stress_covariance` | `test_stress_covariance_tilt_and_fallbacks` (4.0) |
| `n_eff` | Prop. 2.1 | (Σ|w_i|σ_i)² / (w′Σw) | book | `covariance.effective_bets` | `test_effective_bets_matches_proposition_2_1` (1.6) |
| `es_1d` | §8.4 | 5 % ES by simulation, multivariate Student-t (df 4) with the stress covariance | book | `es.simulate_es` | `test_es_simulation_scales_with_vol` |
| cascade / halt / systemic P&L | §8.4 | √-law move on Λ⁻/D(0.02) capped at κσ; (1 − recovery) × venue exposure; 50 % × (direct + collateral) | `positioning`, `book.yaml` | `scenarios.*` | `test_cascade_and_halt_scenarios` (−0.04, −0.24, −0.10) |
| sector-relative z | eq. 9.1 | (x − median_sector)/(1.4826·MAD), winsorised ±3; small sectors fall back to the cross-section | `screens` | `crosssection.sector_standardise` | `test_sector_standardise_and_ic` |
| factors, betas | eq. 9.2 | MKT (cap-weighted), tercile long-shorts SMB/MOM/LIQ, sector minus market; WLS betas over 26 weeks, half-life 8 | `factor_returns`, `factor_betas` | `crosssection.factor_returns`, `rolling_betas` | `test_ew_weights_half_life` |
| screen IC | §9.4 | mean over 52 weeks of Spearman(screen, next-week return), s.e. = sd/√n | `screen_ic` | `crosssection.spearman_ic` | `test_sector_standardise_and_ic` (IC 1.0) |
| rule hit rates | §4.5, §10 | share of fired evaluations whose forward outcome matched the rule's action; n reported | `hit_rates` | `hitrates.hit_rates` | (integration; backfill) |
