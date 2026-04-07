"""Generated trading doctrine corpus for large-scale brain seeding."""
from __future__ import annotations

TRADING_DOMAIN = "trading"
TRADING_META_DOMAIN = "trading_meta"

SCENARIO_REGIMES = {
    "trend_up": {"label": "Uptrend Acceptance", "families": {"trend_pullback": "primary", "breakout_continuation": "primary", "range_reversion": "invalid", "funding_carry": "conditional", "basis_dislocation": "conditional", "momentum_scalp": "conditional", "liquidity_reclaim": "primary", "exhaustion_fade": "conditional"}},
    "trend_down": {"label": "Downtrend Acceptance", "families": {"trend_pullback": "primary", "breakout_continuation": "primary", "range_reversion": "invalid", "funding_carry": "conditional", "basis_dislocation": "conditional", "momentum_scalp": "conditional", "liquidity_reclaim": "primary", "exhaustion_fade": "conditional"}},
    "range_balanced": {"label": "Balanced Range", "families": {"trend_pullback": "invalid", "breakout_continuation": "conditional", "range_reversion": "primary", "funding_carry": "primary", "basis_dislocation": "primary", "momentum_scalp": "conditional", "liquidity_reclaim": "conditional", "exhaustion_fade": "primary"}},
    "compression_chop": {"label": "Compression Chop", "families": {"trend_pullback": "invalid", "breakout_continuation": "invalid", "range_reversion": "conditional", "funding_carry": "conditional", "basis_dislocation": "conditional", "momentum_scalp": "invalid", "liquidity_reclaim": "conditional", "exhaustion_fade": "conditional"}},
    "volatility_expansion": {"label": "Volatility Expansion", "families": {"trend_pullback": "conditional", "breakout_continuation": "primary", "range_reversion": "invalid", "funding_carry": "invalid", "basis_dislocation": "conditional", "momentum_scalp": "primary", "liquidity_reclaim": "primary", "exhaustion_fade": "conditional"}},
    "carry_stability": {"label": "Carry Stability", "families": {"trend_pullback": "conditional", "breakout_continuation": "invalid", "range_reversion": "primary", "funding_carry": "primary", "basis_dislocation": "primary", "momentum_scalp": "invalid", "liquidity_reclaim": "conditional", "exhaustion_fade": "conditional"}},
    "panic_deleveraging": {"label": "Panic Deleveraging", "families": {"trend_pullback": "invalid", "breakout_continuation": "conditional", "range_reversion": "invalid", "funding_carry": "invalid", "basis_dislocation": "conditional", "momentum_scalp": "primary", "liquidity_reclaim": "primary", "exhaustion_fade": "conditional"}},
    "squeeze_regime": {"label": "Squeeze Regime", "families": {"trend_pullback": "conditional", "breakout_continuation": "primary", "range_reversion": "invalid", "funding_carry": "invalid", "basis_dislocation": "conditional", "momentum_scalp": "primary", "liquidity_reclaim": "primary", "exhaustion_fade": "primary"}},
}

SETUP_FAMILIES = {
    "trend_pullback": {"label": "Trend Pullback", "edge": "join the dominant move after a corrective retracement", "avoid": "mistaking reversal for pullback", "philosophy": "trend_following"},
    "breakout_continuation": {"label": "Breakout Continuation", "edge": "press acceptance once value leaves the prior range", "avoid": "chasing fake breaks", "philosophy": "trend_following"},
    "range_reversion": {"label": "Range Reversion", "edge": "fade visible extremes while value stays balanced", "avoid": "fading a real repricing", "philosophy": "mean_reversion"},
    "funding_carry": {"label": "Funding Carry", "edge": "harvest persistent carry when hedge mechanics remain intact", "avoid": "confusing one print with durable carry", "philosophy": "carry_relative_value"},
    "basis_dislocation": {"label": "Basis Dislocation", "edge": "trade temporary futures-spot dislocations with hedge discipline", "avoid": "calling every rich basis a gift", "philosophy": "carry_relative_value"},
    "momentum_scalp": {"label": "Momentum Scalp", "edge": "capture short-horizon bursts when speed matters more than swing anchors", "avoid": "holding a scalp like a swing", "philosophy": "microstructure_execution"},
    "liquidity_reclaim": {"label": "Liquidity Reclaim", "edge": "act after a sweep is reclaimed and trapped flow supports the reversal", "avoid": "catching the knife inside the vacuum", "philosophy": "microstructure_execution"},
    "exhaustion_fade": {"label": "Exhaustion Fade", "edge": "fade extension only after flow and continuation quality deteriorate", "avoid": "fading strong moves only because they look large", "philosophy": "mean_reversion"},
}

EXECUTION_CONDITIONS = {
    "normal_liquidity": {"label": "Normal Liquidity", "instruction": "use the standard execution ladder and passive improvement where possible", "failure": "assuming fills stay cheap without checking spread and depth"},
    "thin_liquidity": {"label": "Thin Liquidity", "instruction": "size down, widen patience, and avoid forcing market orders into empty books", "failure": "letting slippage erase the trade before the thesis is tested"},
    "event_window": {"label": "Event Window", "instruction": "wait for post-event value stabilization before escalating size", "failure": "confusing headline chaos with durable edge"},
    "cross_venue_dislocation": {"label": "Cross-Venue Dislocation", "instruction": "verify the signal is broad enough to trust and route to the venue with cleaner liquidity", "failure": "assuming one venue anomaly represents the whole market"},
    "liquidation_pressure": {"label": "Liquidation Pressure", "instruction": "reduce size, manage to mark price, and prioritize exit agility over ideal targets", "failure": "treating forced flow like normal order flow"},
}

SOURCE_VENUES = [
    {"key": "binance_usdm", "label": "Binance USD-M Futures Docs", "base_url": "https://developers.binance.com/docs/derivatives/usds-margined-futures", "authority": 0.98},
    {"key": "binance_coinm", "label": "Binance COIN-M Futures Docs", "base_url": "https://developers.binance.com/docs/derivatives/coin-margined-futures", "authority": 0.98},
    {"key": "binance_options", "label": "Binance Options Docs", "base_url": "https://developers.binance.com/docs/derivatives/option", "authority": 0.97},
    {"key": "binance_spot", "label": "Binance Spot Docs", "base_url": "https://developers.binance.com/docs/binance-spot-api-docs", "authority": 0.97},
    {"key": "bybit_v5", "label": "Bybit V5 API Docs", "base_url": "https://bybit-exchange.github.io/docs/v5", "authority": 0.97},
    {"key": "deribit_api", "label": "Deribit API Docs", "base_url": "https://docs.deribit.com", "authority": 0.98},
    {"key": "kraken_futures", "label": "Kraken Futures Docs", "base_url": "https://docs.kraken.com/api/docs/futures-api", "authority": 0.96},
    {"key": "coinbase_advanced", "label": "Coinbase Advanced Trade Docs", "base_url": "https://docs.cdp.coinbase.com/advanced-trade/docs/welcome", "authority": 0.95},
    {"key": "okx_v5", "label": "OKX V5 Docs", "base_url": "https://www.okx.com/docs-v5/en", "authority": 0.96},
    {"key": "cme_crypto", "label": "CME Crypto Education", "base_url": "https://www.cmegroup.com/markets/cryptocurrencies.html", "authority": 0.94},
    {"key": "ibit_exchange_rules", "label": "Independent Exchange Risk Rules", "base_url": "https://www.binance.com/en/support/faq", "authority": 0.92},
    {"key": "exchange_margin_methods", "label": "Exchange Margin Methodology", "base_url": "https://www.bybit.com/en/help-center", "authority": 0.92},
]

SOURCE_TOPICS = [
    ("market_data", "core market data and pricing feeds", "microstructure_execution"),
    ("order_book", "order-book depth and queue mechanics", "microstructure_execution"),
    ("trades", "tick-level trade flow and aggressor behavior", "microstructure_execution"),
    ("candles", "candles and historical price structure", "systematic_validation"),
    ("funding", "funding mechanics and carry behavior", "carry_relative_value"),
    ("basis", "basis, premium, and convergence behavior", "carry_relative_value"),
    ("mark_price", "mark price and fair-value methodology", "risk_governance"),
    ("liquidations", "liquidation mechanics and forced-flow behavior", "risk_governance"),
    ("margin", "margin, collateral, and maintenance rules", "risk_governance"),
    ("leverage", "leverage brackets and notional constraints", "risk_governance"),
    ("order_types", "order type mechanics and trigger nuances", "microstructure_execution"),
    ("execution", "routing, fill quality, and implementation details", "microstructure_execution"),
    ("open_interest", "position crowding and participation signals", "trend_following"),
    ("long_short_ratio", "crowding ratios and sentiment overlays", "mean_reversion"),
    ("options_surface", "volatility surface and options context", "volatility_event"),
    ("settlement", "expiry, settlement, and contract transitions", "systematic_validation"),
    ("portfolio_margin", "cross-margin and portfolio-level capital logic", "risk_governance"),
    ("risk_engine", "risk engine, ADL, and system protection mechanics", "risk_governance"),
]

REGIME_PHILOSOPHY_WEIGHTS = {
    "trend_up": {"trend_following": 1.35, "mean_reversion": 0.8, "carry_relative_value": 1.0, "microstructure_execution": 1.1, "volatility_event": 0.95, "risk_governance": 1.15, "systematic_validation": 1.05},
    "trend_down": {"trend_following": 1.35, "mean_reversion": 0.8, "carry_relative_value": 1.0, "microstructure_execution": 1.1, "volatility_event": 0.95, "risk_governance": 1.15, "systematic_validation": 1.05},
    "range_balanced": {"trend_following": 0.85, "mean_reversion": 1.3, "carry_relative_value": 1.15, "microstructure_execution": 1.05, "volatility_event": 0.9, "risk_governance": 1.1, "systematic_validation": 1.0},
    "compression_chop": {"trend_following": 0.75, "mean_reversion": 1.15, "carry_relative_value": 1.1, "microstructure_execution": 0.9, "volatility_event": 0.85, "risk_governance": 1.25, "systematic_validation": 1.0},
    "volatility_expansion": {"trend_following": 1.1, "mean_reversion": 0.8, "carry_relative_value": 0.8, "microstructure_execution": 1.3, "volatility_event": 1.25, "risk_governance": 1.2, "systematic_validation": 0.95},
    "carry_stability": {"trend_following": 0.9, "mean_reversion": 1.0, "carry_relative_value": 1.35, "microstructure_execution": 1.0, "volatility_event": 0.8, "risk_governance": 1.15, "systematic_validation": 1.05},
    "panic_deleveraging": {"trend_following": 0.85, "mean_reversion": 0.8, "carry_relative_value": 0.7, "microstructure_execution": 1.3, "volatility_event": 1.2, "risk_governance": 1.4, "systematic_validation": 0.95},
    "squeeze_regime": {"trend_following": 1.15, "mean_reversion": 0.95, "carry_relative_value": 0.85, "microstructure_execution": 1.25, "volatility_event": 1.15, "risk_governance": 1.15, "systematic_validation": 0.95},
}

SETUP_PHILOSOPHY_WEIGHTS = {key: {meta["philosophy"]: 1.2} for key, meta in SETUP_FAMILIES.items()}

GOVERNANCE_BLUEPRINTS = [
    ("before_entry", "checklist", "Complete the full checklist before approving risk.", "Never approve a trade with missing checklist items."),
    ("before_entry", "event_calendar", "Review event windows before committing fresh risk.", "Do not assume normal liquidity through a catalyst window."),
    ("before_entry", "portfolio_heat", "Check portfolio heat before each add.", "Never evaluate a new trade without book context."),
    ("before_entry", "data_quality", "Confirm data consistency across key feeds.", "Do not size aggressively through state disagreement."),
    ("execution_planning", "slippage_budget", "Declare the slippage budget before sending size.", "Do not force size after the slippage budget breaks."),
    ("execution_planning", "order_type", "Match order type to setup urgency and liquidity.", "Do not use market urgency for convenience."),
    ("execution_planning", "venue_selection", "Route to the venue with cleaner liquidity and lower friction.", "Do not assume one venue's microstructure represents the market."),
    ("managing_open_trade", "stop_integrity", "Keep stop logic tied to thesis invalidation.", "Never widen the stop only to reduce emotional pain."),
    ("managing_open_trade", "thesis_downgrade", "Reduce or exit when thesis quality degrades.", "Do not keep full size after the reason for the trade weakens."),
    ("managing_open_trade", "hold_period", "Manage each setup according to its intended holding period.", "Do not manage a scalp like a swing."),
    ("portfolio_construction", "theme_concentration", "Cluster risk by theme, not ticker count.", "Do not confuse ticker count with diversification."),
    ("portfolio_construction", "liquidity_cluster", "Respect liquidity concentration across correlated books.", "Do not let multiple exits depend on the same thin window."),
]

PHILOSOPHY_TENSIONS = [
    ("trend_following", "mean_reversion", "trend_up", "Prefer trend-following when value is accepting in one direction; mean reversion is secondary until acceptance fails."),
    ("trend_following", "mean_reversion", "range_balanced", "Prefer mean reversion while value stays balanced; trend following needs clear acceptance break."),
    ("carry_relative_value", "trend_following", "carry_stability", "Prefer carry and relative-value evidence when funding is persistent and realized volatility is contained."),
    ("carry_relative_value", "trend_following", "volatility_expansion", "Demote carry logic when volatility expansion threatens hedge integrity and directional repricing dominates."),
    ("microstructure_execution", "systematic_validation", "volatility_expansion", "Execution evidence can outrank slow systematic summaries during fast repricing, but not risk rules."),
    ("microstructure_execution", "systematic_validation", "compression_chop", "In compression, execution noise should not bully the system into false confidence; systematic patience wins."),
    ("risk_governance", "trend_following", "panic_deleveraging", "Risk governance outranks trend conviction during liquidation cascades and infrastructure stress."),
    ("risk_governance", "mean_reversion", "panic_deleveraging", "Risk governance outranks mean-reversion temptation during forced deleveraging."),
    ("volatility_event", "trend_following", "event_window", "Event-driven evidence outranks normal trend assumptions until post-event value stabilizes."),
    ("volatility_event", "carry_relative_value", "event_window", "Event-driven evidence outranks carry logic during catalyst windows."),
    ("systematic_validation", "microstructure_execution", "carry_stability", "Long-horizon validation should dominate opportunistic microstructure noise in stable carry regimes."),
    ("systematic_validation", "trend_following", "compression_chop", "Validation and patience outrank directional urgency in low-quality compression states."),
]


def contradiction_weight(regime_key: str, philosophy: str, setup_key: str | None = None) -> float:
    score = REGIME_PHILOSOPHY_WEIGHTS.get(regime_key, {}).get(philosophy, 1.0)
    if setup_key:
        score *= SETUP_PHILOSOPHY_WEIGHTS.get(setup_key, {}).get(philosophy, 1.0)
    return round(score, 3)


def _build_source_catalog() -> list[dict]:
    catalog: list[dict] = []
    for venue in SOURCE_VENUES:
        for topic_key, description, philosophy in SOURCE_TOPICS:
            catalog.append(
                {
                    "key": f"{venue['key']}_{topic_key}",
                    "label": f"{venue['label']} {topic_key.replace('_', ' ').title()}",
                    "base_url": venue["base_url"],
                    "authority": venue["authority"],
                    "topic_key": topic_key,
                    "description": description,
                    "philosophy": philosophy,
                }
            )
    return catalog


TRADING_SOURCE_CATALOG = _build_source_catalog()


def rank_source_catalog(regime_key: str, setup_key: str | None = None) -> list[dict]:
    ranked = []
    for source in TRADING_SOURCE_CATALOG:
        score = source["authority"] * contradiction_weight(regime_key, source["philosophy"], setup_key)
        ranked.append({**source, "score": round(score, 3)})
    return sorted(ranked, key=lambda row: row["score"], reverse=True)


def _build_source_catalog_kb_entries() -> list[dict]:
    entries: list[dict] = []
    for source in TRADING_SOURCE_CATALOG:
        entries.append(
            {
                "domain": TRADING_META_DOMAIN,
                "topic": f"source_catalog_{source['key']}",
                "content": (
                    f"Canonical primary-source target: {source['label']}. Base URL: {source['base_url']}. "
                    f"Use it to ground {source['description']}. "
                    f"Primary philosophy family: {source['philosophy']}. "
                    f"Authority score: {source['authority']:.2f}."
                ),
                "confidence": "high",
                "source": "core_trading_corpus",
            }
        )
    return entries


def _build_scenario_entries() -> list[dict]:
    entries: list[dict] = []
    for regime_key, regime in SCENARIO_REGIMES.items():
        for setup_key, setup in SETUP_FAMILIES.items():
            compatibility = regime["families"][setup_key]
            for exec_key, exec_meta in EXECUTION_CONDITIONS.items():
                base_topic = f"{regime_key}_{setup_key}_{exec_key}"
                if compatibility == "primary":
                    scenario_text = (
                        f"{regime['label']} with {setup['label']} under {exec_meta['label']}. "
                        f"Use this scenario to {setup['edge']}. "
                        f"Execution instruction: {exec_meta['instruction']}."
                    )
                elif compatibility == "conditional":
                    scenario_text = (
                        f"Conditional deployment of {setup['label']} inside {regime['label']} under {exec_meta['label']}. "
                        f"This setup can work only when evidence is unusually strong and risk remains cheap. "
                        f"Execution instruction: {exec_meta['instruction']}."
                    )
                else:
                    scenario_text = (
                        f"Anti-playbook for {setup['label']} inside {regime['label']} under {exec_meta['label']}. "
                        f"Default stance is stand down because this pairing usually fails by {setup['avoid']}. "
                        f"If attempted at all, it must be treated as exceptional and size must stay minimal."
                    )
                entries.append(
                    {
                        "domain": TRADING_DOMAIN,
                        "topic": f"scenario_{base_topic}",
                        "content": scenario_text,
                        "confidence": "high",
                        "source": "core_trading_corpus",
                    }
                )
                entries.append(
                    {
                        "domain": TRADING_DOMAIN,
                        "topic": f"failure_{base_topic}",
                        "content": (
                            f"Failure card for {regime['label']} plus {setup['label']} under {exec_meta['label']}. "
                            f"Common failure: {setup['avoid']}. "
                            f"Execution trap: {exec_meta['failure']}. "
                            f"If these conditions appear, flatten or demote the trade instead of defending the narrative."
                        ),
                        "confidence": "high",
                        "source": "core_trading_corpus",
                    }
                )
    return entries


def _build_regime_setup_rules() -> list[dict]:
    rules: list[dict] = []
    for regime_key, regime in SCENARIO_REGIMES.items():
        for setup_key, setup in SETUP_FAMILIES.items():
            compatibility = regime["families"][setup_key]
            if compatibility == "primary":
                full_rule = f"In {regime['label']}, {setup['label']} is a first-class setup family when evidence is clean and costs are acceptable."
                pointer = f"allow_{regime_key}_{setup_key}"
            elif compatibility == "conditional":
                full_rule = f"In {regime['label']}, {setup['label']} is conditional and should use reduced size, tighter invalidation, and stronger confirmation than normal."
                pointer = f"conditional_{regime_key}_{setup_key}"
            else:
                full_rule = f"Anti-rule: do not deploy {setup['label']} in {regime['label']} as a normal playbook. Default action is pass, not creativity."
                pointer = f"block_{regime_key}_{setup_key}"
            rules.append(
                {
                    "trigger": "strategy_selection",
                    "pointer": pointer,
                    "full_rule": full_rule,
                    "domain": TRADING_DOMAIN,
                    "priority": 1,
                    "confidence": 0.98 if compatibility != "conditional" else 0.95,
                }
            )
    return rules


def _build_execution_rules() -> list[dict]:
    rules: list[dict] = []
    for setup_key, setup in SETUP_FAMILIES.items():
        for exec_key, exec_meta in EXECUTION_CONDITIONS.items():
            rules.append(
                {
                    "trigger": "execution_planning",
                    "pointer": f"{setup_key}_{exec_key}_execution_policy",
                    "full_rule": f"For {setup['label']} under {exec_meta['label']}, {exec_meta['instruction']}. Anti-rule: {exec_meta['failure']}.",
                    "domain": TRADING_DOMAIN,
                    "priority": 2,
                    "confidence": 0.96,
                }
            )
    return rules


def _build_governance_rules() -> list[dict]:
    rules: list[dict] = []
    for trigger, pointer_root, must_text, anti_text in GOVERNANCE_BLUEPRINTS:
        rules.append(
            {
                "trigger": trigger,
                "pointer": f"{pointer_root}_must",
                "full_rule": must_text,
                "domain": TRADING_DOMAIN,
                "priority": 1,
                "confidence": 0.98,
            }
        )
        rules.append(
            {
                "trigger": trigger,
                "pointer": f"{pointer_root}_anti",
                "full_rule": f"Anti-rule: {anti_text}",
                "domain": TRADING_DOMAIN,
                "priority": 1,
                "confidence": 0.98,
            }
        )
    return rules


def _build_tension_rules() -> tuple[list[dict], list[dict]]:
    rules: list[dict] = []
    meta_entries: list[dict] = []
    for left, right, regime_key, resolution in PHILOSOPHY_TENSIONS:
        rules.append(
            {
                "trigger": "source_conflict",
                "pointer": f"prefer_{left}_over_{right}_in_{regime_key}",
                "full_rule": resolution,
                "domain": TRADING_DOMAIN,
                "priority": 2,
                "confidence": 0.96,
            }
        )
        rules.append(
            {
                "trigger": "source_conflict",
                "pointer": f"anti_blind_merge_{left}_{right}_in_{regime_key}",
                "full_rule": f"Anti-rule: do not average {left} and {right} blindly in {regime_key}; weight them by regime and setup instead.",
                "domain": TRADING_DOMAIN,
                "priority": 2,
                "confidence": 0.96,
            }
        )
        meta_entries.append(
            {
                "domain": TRADING_META_DOMAIN,
                "topic": f"contradiction_matrix_{left}_{right}_in_{regime_key}",
                "content": resolution,
                "confidence": "high",
                "source": "core_trading_corpus",
            }
        )
    return rules, meta_entries


def _build_generated_mistakes() -> list[dict]:
    patterns = [
        ("Checklist was skipped when the setup looked obvious.", "process", "critical"),
        ("Event risk was treated as normal liquidity.", "event", "high"),
        ("Portfolio context was ignored because the single chart looked clean.", "portfolio", "high"),
        ("Data disagreement was ignored and size stayed aggressive.", "data_quality", "high"),
        ("Slippage budget was breached and the trade was still forced.", "execution", "high"),
        ("Wrong order type was used for the microstructure state.", "execution", "medium"),
        ("The cleanest venue was not chosen before routing.", "execution", "medium"),
        ("Stop logic was changed after entry without better evidence.", "risk", "critical"),
        ("A weak thesis kept full size instead of shrinking.", "risk", "high"),
        ("Setup holding period drifted away from the actual playbook.", "management", "medium"),
        ("Theme concentration hid multiple copies of the same risk.", "portfolio", "high"),
        ("Liquidity concentration created exit crowding across positions.", "portfolio", "high"),
    ]
    mistakes: list[dict] = []
    for pattern, tag_root, severity in patterns:
        for context in ("during calm conditions", "during stressed conditions"):
            mistakes.append(
                {
                    "domain": TRADING_DOMAIN,
                    "what_failed": f"{pattern} {context.capitalize()}.",
                    "root_cause": "The playbook was present but not enforced at the right layer.",
                    "correct_approach": "Map the trade to regime, setup family, and execution condition before risking capital.",
                    "how_to_avoid": "Use the generated doctrine and scenario corpus as a hard gate instead of optional reading.",
                    "severity": severity,
                    "context": f"seed: {tag_root} {context}",
                    "tags": ["seed", tag_root, "corpus"],
                }
            )
    return mistakes


TRADING_SOURCE_CATALOG_KB_ENTRIES = _build_source_catalog_kb_entries()
TRADING_GENERATED_SCENARIO_KB_ENTRIES = _build_scenario_entries()
_tension_rules, TRADING_CONTRADICTION_META_ENTRIES = _build_tension_rules()
TRADING_GENERATED_RULES = _build_regime_setup_rules() + _build_execution_rules() + _build_governance_rules() + _tension_rules
TRADING_GENERATED_MISTAKES = _build_generated_mistakes()

TRADING_CORPUS_STATS = {
    "source_catalog_cards": len(TRADING_SOURCE_CATALOG_KB_ENTRIES),
    "scenario_cards": len(TRADING_GENERATED_SCENARIO_KB_ENTRIES),
    "generated_rules": len(TRADING_GENERATED_RULES),
    "generated_anti_rules": sum(1 for row in TRADING_GENERATED_RULES if str(row["full_rule"]).startswith("Anti-rule:")),
    "generated_mistakes": len(TRADING_GENERATED_MISTAKES),
    "generated_meta_entries": len(TRADING_CONTRADICTION_META_ENTRIES),
}
