"""Trading-only specialization helpers for phase-1 CORE AGI rollout."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from core_config import (
    CORE_ENABLE_GENERIC_PUBLIC_INGEST,
    CORE_ENABLE_GENERIC_RESEARCH_AUTONOMY,
    CORE_SPECIALIZATION,
    CORE_TRAINING_MODE,
    sb_get,
)
from core_trading_corpus import (
    TRADING_CONTRADICTION_META_ENTRIES,
    TRADING_CORPUS_STATS,
    TRADING_GENERATED_MISTAKES,
    TRADING_GENERATED_RULES,
    TRADING_GENERATED_SCENARIO_KB_ENTRIES,
    TRADING_SOURCE_CATALOG_KB_ENTRIES,
)

TRADING_DOMAIN = "trading"
TRADING_META_DOMAIN = "trading_meta"
UNIVERSAL_DOMAIN = "universal"

_TRUE_SET = {"1", "true", "yes", "on"}
_TRADING_TASK_KEYWORDS = {
    "trading",
    "trade",
    "paper",
    "portfolio",
    "position",
    "signal",
    "strategy",
    "regime",
    "risk",
    "funding",
    "carry",
    "correlation",
    "execution",
    "slippage",
    "drawdown",
    "market",
    "symbol",
    "backtest",
    "matrix",
    "btc",
    "eth",
    "sol",
    "bnb",
}

TRADING_RULES = [
    {
        "trigger": "before_entry",
        "pointer": "check_regime_before_entry",
        "full_rule": (
            "Never enter a directional trade when market_classifier returns CHOP. "
            "CHOP means ATR is compressed and breakout expectancy is weak. "
            "Wait for TREND, RANGE, or EXPANSION-specific confirmation before taking risk."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "before_entry",
        "pointer": "check_bias_alignment",
        "full_rule": (
            "Never open a LONG when higher-timeframe bias is BEAR with confidence >= 0.80, "
            "and never open a SHORT when higher-timeframe bias is BULL with confidence >= 0.80. "
            "Bias blocks override narrative confidence."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.95,
    },
    {
        "trigger": "sizing_trade",
        "pointer": "risk_first_sizing_from_atr",
        "full_rule": (
            "Position size must come from ATR-based stop distance, never conviction. "
            "Capital at risk per trade stays within 0.25-0.75 percent, "
            "and single-direction allocation must remain bounded even when confidence is high."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.96,
    },
    {
        "trigger": "funding_harvest_decision",
        "pointer": "funding_rate_threshold",
        "full_rule": (
            "Only open funding-harvest positions when funding is clearly positive and persistent. "
            "Exit immediately when funding weakens materially or turns negative."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 2,
        "confidence": 0.98,
    },
    {
        "trigger": "after_3_consecutive_losses",
        "pointer": "circuit_breaker_pause",
        "full_rule": (
            "After three consecutive losses or a severe daily drawdown, pause trading and require human review. "
            "No automatic revenge trading or confidence escalation is allowed."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.99,
    },
    {
        "trigger": "selecting_strategy",
        "pointer": "candidate_menu_only",
        "full_rule": (
            "Only select from the deterministic candidate menu produced by the strategy engine. "
            "If no candidate clears the minimum setup threshold, return strategy=nothing."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "directional_crowding",
        "pointer": "max_same_symbol_strategy_pair",
        "full_rule": (
            "Never open the exact same symbol plus strategy pair twice. "
            "Manage correlated crowding across BTC, ETH, SOL, and BNB before adding exposure."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
]

TRADING_KB_ENTRIES = [
    {
        "domain": TRADING_DOMAIN,
        "topic": "regime_strategy_matrix",
        "content": (
            "Regime strategy matrix. CHOP: no directional entries. RANGE: funding harvest first, "
            "then range reversion at extremes. TREND: trend breakout and pullback dominate. "
            "EXPANSION: prefer short-horizon scalp logic with tight risk. "
            "Higher-timeframe bias blocks the opposite direction when confidence is high."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "funding_harvest_mechanics",
        "content": (
            "Funding harvest is a carry trade that requires positive funding, low directional drift, "
            "and clean hedge management. It fails quickly when funding compresses, flips negative, "
            "or volatility expands faster than the hedge can stay neutral."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "chop_regime_loss_pattern",
        "content": (
            "CHOP is the most common source of repeated momentum losses. "
            "Compressed ATR and flat structure produce fake breaks and low reward-to-risk. "
            "The correct default in CHOP is patience, not creativity."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "paper_trading_graduation_path",
        "content": (
            "Promotion from paper to live requires statistical evidence, regime coverage, "
            "controlled drawdown, and operational discipline. "
            "A small winning streak is not enough to justify live exposure."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
]

TRADING_MISTAKE_ENTRIES = [
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Directional entry attempted in CHOP instead of standing down.",
        "root_cause": "Regime compression was ignored and breakout expectancy was overstated.",
        "correct_approach": "Treat CHOP as a hard no-trade state for momentum entries until structure changes.",
        "how_to_avoid": "Gate all directional trades through regime validation before sizing or narrative review.",
        "severity": "high",
        "context": "seed: chop directional loss",
        "tags": ["seed", "regime", "chop", "directional"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Trade direction conflicted with higher-timeframe bias.",
        "root_cause": "Lower-timeframe setup was treated as sufficient despite strong top-down opposition.",
        "correct_approach": "Respect high-confidence higher-timeframe bias as a hard directional constraint.",
        "how_to_avoid": "Block opposing entries when higher-timeframe bias confidence is elevated.",
        "severity": "high",
        "context": "seed: htf bias conflict",
        "tags": ["seed", "bias", "htf", "direction"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Position size expanded because conviction was high rather than ATR risk being acceptable.",
        "root_cause": "Sizing discipline was replaced by narrative confidence.",
        "correct_approach": "Use ATR stop distance and risk budget to set size before evaluating conviction.",
        "how_to_avoid": "Reject any position whose size cannot be justified by risk-first sizing math.",
        "severity": "critical",
        "context": "seed: oversizing by conviction",
        "tags": ["seed", "risk", "sizing", "atr"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Funding harvest was opened with weak or deteriorating funding support.",
        "root_cause": "Carry yield quality was not verified before entry.",
        "correct_approach": "Require strong positive funding and exit as soon as funding weakens materially.",
        "how_to_avoid": "Treat negative or fading funding as an immediate invalidation of the carry thesis.",
        "severity": "high",
        "context": "seed: weak funding harvest",
        "tags": ["seed", "funding", "carry", "yield"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Duplicate symbol plus strategy exposure was allowed.",
        "root_cause": "Exposure accounting focused on entries individually instead of the portfolio state.",
        "correct_approach": "Prevent duplicate symbol-strategy pairs and review crowding before every add.",
        "how_to_avoid": "Check open exposure inventory before approving any new trade.",
        "severity": "medium",
        "context": "seed: duplicate exposure",
        "tags": ["seed", "portfolio", "exposure", "crowding"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Portfolio became crowded with correlated directional risk across majors.",
        "root_cause": "Correlation guard was weaker than single-trade conviction.",
        "correct_approach": "Size and gate entries at the portfolio level, not just trade level.",
        "how_to_avoid": "Review BTC, ETH, SOL, and BNB correlation before adding another directional trade.",
        "severity": "high",
        "context": "seed: correlated crowding",
        "tags": ["seed", "correlation", "portfolio", "risk"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Carry or reversion logic was used during an expansion regime.",
        "root_cause": "Regime-specific playbook was ignored when volatility expanded.",
        "correct_approach": "Use expansion-specific logic with smaller size, tighter stops, and shorter hold time.",
        "how_to_avoid": "Map each strategy family to valid regimes before entry.",
        "severity": "high",
        "context": "seed: regime strategy mismatch",
        "tags": ["seed", "regime", "expansion", "strategy"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Paper-trading results were promoted to live expectations too early.",
        "root_cause": "Short sample performance was mistaken for robust edge.",
        "correct_approach": "Require sample size, drawdown control, and multi-regime evidence before graduation.",
        "how_to_avoid": "Treat paper-to-live promotion as a gated risk decision, not a morale milestone.",
        "severity": "critical",
        "context": "seed: paper graduation overreach",
        "tags": ["seed", "paper", "live", "graduation"],
    },
]

TRADING_RARL_GOALS = [
    ("Improve regime classification robustness so trading logic stands down faster in ambiguous structure.", "regime_classification"),
    ("Improve strategy-family gating quality so each strategy fires only in the regimes it was built for.", "strategy_gating"),
    ("Improve risk sizing and stop logic so capital at risk stays stable across volatility changes.", "risk_sizing"),
    ("Improve funding and carry entry-exit logic so yield is harvested only when the carry thesis is durable.", "funding_logic"),
    ("Improve correlation and crowding protection across the portfolio before new exposure is added.", "correlation_guard"),
    ("Improve execution and slippage handling so realized outcomes stay close to planned outcomes.", "execution"),
    ("Improve paper-to-live graduation policy so promotion requires durable evidence instead of optimism.", "paper_graduation"),
    ("Improve decision calibration so confidence and action intensity match the actual edge quality.", "decision_calibration"),
]

TRADING_RULES.extend([
    {
        "trigger": "before_entry",
        "pointer": "require_positive_expected_value_after_costs",
        "full_rule": (
            "Reject any setup whose expected edge does not survive fees, spread, slippage, and funding. "
            "Gross edge is not actionable edge."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
    {
        "trigger": "before_entry",
        "pointer": "invalidation_must_be_defined_before_entry",
        "full_rule": (
            "Every trade thesis must define invalidation before entry. "
            "If invalidation cannot be stated in price, structure, or flow terms, there is no trade."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
    {
        "trigger": "before_entry",
        "pointer": "liquidity_caps_position_size",
        "full_rule": (
            "Liquidity and order-book depth cap position size before conviction does. "
            "If expected impact or slippage is too high, size down or skip."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "before_entry",
        "pointer": "portfolio_heat_limit",
        "full_rule": (
            "Total open portfolio heat must remain bounded. "
            "Do not add a new trade if combined open risk, correlation, and event exposure breach the portfolio heat budget."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
    {
        "trigger": "before_entry",
        "pointer": "event_risk_blackout",
        "full_rule": (
            "Do not initiate fresh risk into major scheduled event windows when realized edge depends on normal liquidity. "
            "Wait until post-event spread, volatility, and direction stabilize."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.96,
    },
    {
        "trigger": "execution_planning",
        "pointer": "slippage_budget_gate",
        "full_rule": (
            "Execution plan must specify acceptable slippage and order type before sending size. "
            "If the slippage budget is breached, reduce size or stand down."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "execution_planning",
        "pointer": "prefer_pass_over_forced_fill",
        "full_rule": (
            "Passing on a mediocre setup is superior to forcing a poor fill. "
            "Never chase a move solely because price is leaving the intended entry zone."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 2,
        "confidence": 0.96,
    },
    {
        "trigger": "managing_open_trade",
        "pointer": "never_average_loser_without_new_edge",
        "full_rule": (
            "Do not add to a losing trade unless a new higher-quality edge appears and portfolio heat still permits it. "
            "A worse price alone is not a new edge."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
    {
        "trigger": "managing_open_trade",
        "pointer": "respect_mark_price_and_liquidation_distance",
        "full_rule": (
            "Derivatives risk must be managed to mark price and liquidation distance, not last-trade comfort. "
            "Leverage must be reduced before liquidation proximity becomes urgent."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.97,
    },
    {
        "trigger": "strategy_selection",
        "pointer": "match_strategy_to_regime_family",
        "full_rule": (
            "Trend strategies belong to trend structure, reversion belongs to range structure, "
            "and carry belongs to stable funding plus controlled volatility. Do not cross-deploy strategy families casually."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.98,
    },
    {
        "trigger": "paper_to_live_review",
        "pointer": "promotion_requires_cost_adjusted_regime_coverage",
        "full_rule": (
            "Paper-to-live promotion requires cost-adjusted profitability, multi-regime coverage, "
            "stable drawdown, and operational discipline. A single hot streak never qualifies."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 1,
        "confidence": 0.99,
    },
    {
        "trigger": "post_trade_review",
        "pointer": "separate_process_quality_from_outcome_noise",
        "full_rule": (
            "Review process quality separately from PnL. "
            "A good trade can lose and a bad trade can win; policy updates must be evidence-based, not outcome-biased."
        ),
        "domain": TRADING_DOMAIN,
        "priority": 2,
        "confidence": 0.97,
    },
])

TRADING_RULES.extend(TRADING_GENERATED_RULES)

TRADING_KB_ENTRIES.extend([
    {
        "domain": TRADING_DOMAIN,
        "topic": "higher_timeframe_bias_framework",
        "content": (
            "Higher-timeframe bias is the first directional filter. "
            "Use structure, momentum, and macro positioning to define the dominant path, then let lower timeframes refine timing only."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "atr_risk_sizing_formula",
        "content": (
            "ATR-based sizing converts volatility into bounded dollar risk. "
            "Risk per trade should be fixed first, then size becomes risk budget divided by stop distance and adjusted for liquidity."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "portfolio_heat_budgeting",
        "content": (
            "Portfolio heat is the combined open risk after correlation and event overlap. "
            "Manage the book so one macro move cannot invalidate multiple positions at once."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "expected_value_after_execution_costs",
        "content": (
            "Trade expectancy must be net of spread, fees, slippage, delay, and funding. "
            "Many research edges disappear after realistic execution assumptions are applied."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "trend_pullback_playbook",
        "content": (
            "Trend pullback entries work best when higher-timeframe bias and local structure agree, "
            "volatility is orderly, and invalidation is nearby enough to keep reward-to-risk attractive."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "range_reversion_playbook",
        "content": (
            "Range reversion belongs in balanced structure with visible extremes, fading momentum, and contained volatility. "
            "If range edges are not obvious, the setup is not mean reversion."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "expansion_regime_playbook",
        "content": (
            "Expansion regimes demand smaller size, tighter execution control, and shorter holding periods. "
            "Volatility expansion punishes stale levels and slow decision loops."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "liquidity_and_slippage_principles",
        "content": (
            "Liquidity is a hard constraint, not a post-trade excuse. "
            "The book should only size positions that can enter and exit without destroying the edge."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "order_book_signal_limitations",
        "content": (
            "Order-book imbalance is useful only as a short-horizon execution overlay. "
            "It is fragile, easy to spoof, and insufficient without structure, volatility, and cost context."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "funding_basis_alignment",
        "content": (
            "Funding, basis, open interest, and crowding should align before carry trades are trusted. "
            "One positive carry metric alone is not durable edge."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "open_interest_interpretation",
        "content": (
            "Open interest matters only when paired with price and context. "
            "Rising OI with price continuation can confirm fresh participation; rising OI into exhaustion can signal crowded risk."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "long_short_ratio_interpretation",
        "content": (
            "Long-short ratios are crowding indicators, not stand-alone triggers. "
            "They are most useful as a fade or caution overlay when price, funding, and open interest corroborate the crowding story."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "mark_price_and_liquidation_awareness",
        "content": (
            "Derivatives risk should be monitored to mark price because liquidations and funding settle there. "
            "Ignoring mark-price distance creates false safety during stress."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "event_risk_blackout_logic",
        "content": (
            "Event risk compresses decision quality by widening spreads, changing liquidity, and accelerating correlation. "
            "Fresh risk should wait until the post-event regime is observable."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "stop_placement_principles",
        "content": (
            "Stops belong where the thesis is invalidated, not where loss feels emotionally convenient. "
            "If the proper stop is too wide for the risk budget, the trade is too large or invalid."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "target_and_trailing_logic",
        "content": (
            "Profit-taking should reflect structure, volatility, and diminishing edge. "
            "Trail only when the market continues to pay for patience; otherwise bank edge before noise reclaims it."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "correlation_crowding_protocol",
        "content": (
            "BTC, ETH, SOL, and BNB often compress into one macro trade during stress. "
            "Correlation management should reduce the number of hidden copies of the same bet."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "leverage_cap_principles",
        "content": (
            "Leverage is a delivery mechanism for edge, not a substitute for edge. "
            "Leverage should fall when volatility, crowding, or gap risk rises."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "backtest_validation_requirements",
        "content": (
            "Backtests need walk-forward splits, cost adjustment, regime coverage, and rejection of fragile parameter tuning. "
            "If the edge vanishes under slight perturbation, it was curve fit."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_DOMAIN,
        "topic": "nothing_is_a_position",
        "content": (
            "No-trade is an active position in uncertainty. "
            "Standing down during weak structure preserves capital, attention, and confidence for higher-quality conditions."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
])

TRADING_KB_ENTRIES.extend(TRADING_GENERATED_SCENARIO_KB_ENTRIES)

TRADING_MISTAKE_ENTRIES.extend([
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Trade was taken without positive expected value after costs.",
        "root_cause": "Gross setup quality was confused with net executable edge.",
        "correct_approach": "Model spread, fees, slippage, and funding before approving the trade.",
        "how_to_avoid": "Reject setups whose reward-to-risk only works before execution costs.",
        "severity": "critical",
        "context": "seed: negative net expectancy",
        "tags": ["seed", "execution", "costs", "expectancy"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Liquidity was too thin for the intended position size.",
        "root_cause": "Size was based on conviction rather than executable market depth.",
        "correct_approach": "Let liquidity and slippage cap size before the order is sent.",
        "how_to_avoid": "Estimate impact from spread and depth and cut size when the market cannot absorb it cleanly.",
        "severity": "high",
        "context": "seed: liquidity breach",
        "tags": ["seed", "liquidity", "slippage", "execution"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "No invalidation was defined before entry.",
        "root_cause": "The trade thesis was emotional or vague rather than structural.",
        "correct_approach": "Define the condition that proves the idea wrong before entry and price the stop from that logic.",
        "how_to_avoid": "Block every trade that cannot state invalidation in one sentence.",
        "severity": "critical",
        "context": "seed: no invalidation",
        "tags": ["seed", "invalidation", "risk", "process"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Fresh risk was added during an event-risk blackout window.",
        "root_cause": "Normal-liquidity assumptions were carried into abnormal conditions.",
        "correct_approach": "Wait for post-event spreads, volatility, and structure to normalize before re-engaging.",
        "how_to_avoid": "Maintain an event calendar and enforce a pre-event risk gate.",
        "severity": "high",
        "context": "seed: event blackout violation",
        "tags": ["seed", "event", "liquidity", "risk"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Portfolio heat exceeded safe limits before the new trade was approved.",
        "root_cause": "Single-trade quality was evaluated without full book context.",
        "correct_approach": "Check aggregate risk and correlation before each new position.",
        "how_to_avoid": "Require a portfolio heat snapshot in every pre-trade decision.",
        "severity": "high",
        "context": "seed: portfolio heat breach",
        "tags": ["seed", "portfolio", "heat", "risk"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "A losing position was averaged down without new evidence.",
        "root_cause": "Lower price was mistaken for better value instead of higher risk.",
        "correct_approach": "Only add if a fresh edge appears and the portfolio still has risk capacity.",
        "how_to_avoid": "Treat averaging a loser as a new trade that must pass the full entry checklist.",
        "severity": "critical",
        "context": "seed: averaged loser",
        "tags": ["seed", "averaging_down", "discipline", "risk"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Paper edge was evaluated without realistic execution costs.",
        "root_cause": "Research assumed ideal fills and ignored live market friction.",
        "correct_approach": "Stress the strategy with cost-adjusted execution before trusting paper PnL.",
        "how_to_avoid": "Paper-to-live reviews must include realistic spread, fees, and slippage assumptions.",
        "severity": "high",
        "context": "seed: paper costs omitted",
        "tags": ["seed", "paper", "slippage", "validation"],
    },
    {
        "domain": TRADING_DOMAIN,
        "what_failed": "Post-trade review updated policy from outcome noise instead of process evidence.",
        "root_cause": "One result was overweighted relative to sample quality and execution quality.",
        "correct_approach": "Separate process quality from PnL noise and update rules only from repeated evidence.",
        "how_to_avoid": "Review trades with a checklist that scores setup quality, execution quality, and regime fit separately.",
        "severity": "medium",
        "context": "seed: outcome bias review",
        "tags": ["seed", "review", "outcome_bias", "process"],
    },
])

TRADING_MISTAKE_ENTRIES.extend(TRADING_GENERATED_MISTAKES)

TRADING_META_KB_ENTRIES = [
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_operating_doctrine",
        "content": (
            "CORE trading doctrine: protect capital first, demand positive edge after costs, "
            "and prefer no-trade over low-quality exposure. Regime fit, invalidation clarity, and portfolio heat are mandatory."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_signal_quality_rubric",
        "content": (
            "Signal quality rubric: regime fit, higher-timeframe alignment, cost-adjusted expectancy, liquidity quality, "
            "invalidation clarity, and portfolio heat. Weakness in any mandatory column should demote or block the trade."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_execution_scorecard",
        "content": (
            "Execution scorecard tracks spread paid, slippage versus budget, fill discipline, and whether the order type matched the setup. "
            "Good research with bad fills is still bad live trading."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_post_trade_review_loop",
        "content": (
            "Every closed trade should be reviewed for thesis quality, regime fit, execution quality, and portfolio context. "
            "The review goal is to improve process, not to rationalize the result."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_research_backlog",
        "content": (
            "Trading research priorities: regime classification, strategy-family gating, risk sizing, funding logic, "
            "correlation control, execution quality, graduation policy, and confidence calibration."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
    {
        "domain": TRADING_META_DOMAIN,
        "topic": "trading_runtime_boundary",
        "content": (
            "Seed deterministic priors into rules, concepts, mistakes, and curated knowledge only. "
            "Do not fabricate live trades, live patterns, or realized PnL history; those tables must reflect runtime truth."
        ),
        "confidence": "high",
        "source": "core_seed_trading_brain",
    },
]

TRADING_META_KB_ENTRIES.extend(TRADING_CONTRADICTION_META_ENTRIES)
TRADING_META_KB_ENTRIES.extend(TRADING_SOURCE_CATALOG_KB_ENTRIES)

TRADING_SEED_HOT_REFLECTIONS = [
    {
        "task_summary": "Trading doctrine seed installed",
        "domain": TRADING_META_DOMAIN,
        "new_patterns": ["regime_gate", "risk_first_sizing", "cost_adjusted_expectancy"],
        "new_mistakes": [],
        "quality_score": 0.94,
        "gaps_identified": ["runtime trading decisions still required for live adaptation"],
        "reflection_text": (
            "Installed deterministic trading doctrine covering regime gating, higher-timeframe alignment, "
            "risk-first sizing, execution-cost discipline, and portfolio heat."
        ),
        "processed_by_cold": 0,
        "source": "trading_seed",
    },
    {
        "task_summary": "Trading curriculum seed installed",
        "domain": TRADING_META_DOMAIN,
        "new_patterns": ["execution_scorecard", "paper_to_live_gate", "process_over_outcome_review"],
        "new_mistakes": [],
        "quality_score": 0.92,
        "gaps_identified": ["live trading outcomes absent until bot starts closing positions"],
        "reflection_text": (
            "Installed seed curriculum for research, execution review, and paper-to-live graduation so CORE starts with "
            "a disciplined trading framework before any live outcomes exist."
        ),
        "processed_by_cold": 0,
        "source": "trading_seed",
    },
]

TRADING_SEED_HOT_REFLECTIONS.extend([
    {
        "task_summary": "Trading source corpus catalog installed",
        "domain": TRADING_META_DOMAIN,
        "new_patterns": ["primary_source_catalog", "source_family_index", "regime_weighted_sources"],
        "new_mistakes": [],
        "quality_score": 0.96,
        "gaps_identified": ["the 200+ source catalog still needs selective live fetching over time"],
        "reflection_text": (
            f"Installed a primary-source catalog with {TRADING_CORPUS_STATS['source_catalog_cards']} canonical source cards and "
            "regime-weighted ranking logic for conflicting trading philosophies."
        ),
        "processed_by_cold": 0,
        "source": "trading_seed",
    },
    {
        "task_summary": "Trading scenario lattice installed",
        "domain": TRADING_META_DOMAIN,
        "new_patterns": ["scenario_lattice", "regime_setup_execution_matrix", "anti_playbook_memory"],
        "new_mistakes": [],
        "quality_score": 0.97,
        "gaps_identified": ["runtime hit-rate tracking still needs live decisions"],
        "reflection_text": (
            f"Installed a generated scenario lattice with {TRADING_CORPUS_STATS['scenario_cards']} scenario and failure cards "
            "covering regime, setup family, and execution condition combinations."
        ),
        "processed_by_cold": 0,
        "source": "trading_seed",
    },
])

TRADING_READINESS_TARGETS = {
    "rules": 150,
    "knowledge_base": 500,
    "trading_meta_kb": 200,
    "seed_sources": 10,
    "seed_articles": 8,
    "seed_concepts": 18,
    "memory_mistakes": 40,
    "hot_reflections": 4,
}


def trading_specialization_enabled() -> bool:
    return CORE_SPECIALIZATION == "trading" or CORE_TRAINING_MODE == "trading_only"


def trading_training_only_enabled() -> bool:
    return CORE_TRAINING_MODE == "trading_only" or trading_specialization_enabled()


def allow_generic_public_ingest() -> bool:
    if trading_specialization_enabled():
        return CORE_ENABLE_GENERIC_PUBLIC_INGEST
    return True


def allow_generic_research_autonomy() -> bool:
    if trading_specialization_enabled():
        return CORE_ENABLE_GENERIC_RESEARCH_AUTONOMY
    return True


def training_meta_domain() -> str:
    return TRADING_META_DOMAIN if trading_specialization_enabled() else "meta"


def detect_trading_task_domain(task_json: str = "") -> str | None:
    text = str(task_json or "").lower()
    if any(keyword in text for keyword in _TRADING_TASK_KEYWORDS):
        return TRADING_DOMAIN
    return None


def _safe_rows(table: str, query: str) -> list[dict]:
    try:
        rows = sb_get(table, query, svc=True) or []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _default_since_ts(days: int = 30) -> str:
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_trading_source_packet(since_ts: str | None = None, limit: int = 40) -> dict:
    since_ts = (since_ts or _default_since_ts()).strip()
    count_limit = max(limit * 4, 1200)
    rules = _safe_rows(
        "behavioral_rules",
        "select=id,trigger,pointer,full_rule,domain,priority,confidence,source,created_at"
        f"&active=eq.true&domain=in.({UNIVERSAL_DOMAIN},{TRADING_DOMAIN})&order=priority.asc&limit={count_limit}",
    )
    kb = _safe_rows(
        "knowledge_base",
        "select=id,domain,topic,content,instruction,confidence,source,created_at,updated_at"
        f"&domain=eq.{TRADING_DOMAIN}&order=updated_at.desc&limit={count_limit}",
    )
    trading_meta_kb = _safe_rows(
        "knowledge_base",
        "select=id,domain,topic,content,instruction,confidence,source,created_at,updated_at"
        f"&domain=eq.{TRADING_META_DOMAIN}&order=updated_at.desc&limit={count_limit}",
    )
    seed_sources = _safe_rows(
        "kb_sources",
        "select=id,url,title,source_type,source_platform,published_at,ingested_at,last_refreshed,trust_level,topics,engagement_score,status"
        f"&topics=cs.{{trading}}&order=last_refreshed.desc&limit={count_limit}",
    )
    all_kb_articles = _safe_rows(
        "kb_articles",
        f"select=id,source_id,summary,consensus_level&order=id.desc&limit={count_limit}",
    )
    external_concepts = _safe_rows(
        "kb_concepts",
        "select=id,concept_name,category,definition,source_count,avg_engagement,trend,related_concepts,implementations"
        f"&category=like.trading*&order=source_count.desc&limit={count_limit}",
    )
    memory_mistakes = _safe_rows(
        "mistakes",
        "select=id,domain,what_failed,root_cause,how_to_avoid,correct_approach,severity,created_at,context"
        f"&domain=eq.{TRADING_DOMAIN}&order=created_at.desc&limit={count_limit}",
    )
    hot_reflections = _safe_rows(
        "hot_reflections",
        "select=id,domain,task_summary,reflection_text,quality_score,source,created_at,new_patterns,new_mistakes,gaps_identified"
        f"&domain=in.({TRADING_DOMAIN},{TRADING_META_DOMAIN})&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )
    trading_patterns = _safe_rows(
        "trading_patterns",
        "select=id,pattern_key,description,conditions,outcome,win_count,total_count,avg_pnl_usd,win_rate,last_seen,created_at"
        f"&order=created_at.desc&limit={count_limit}",
    )
    trading_mistakes = _safe_rows(
        "trading_mistakes",
        "select=id,position_id,decision_id,what_failed,market_context,root_cause,how_to_avoid,loss_usd,severity,created_at"
        f"&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )
    trading_decisions = _safe_rows(
        "trading_decisions",
        "select=id,market_regime,strategy,symbol,direction,confidence,risk_level,reasoning,expected_pnl,action_taken,position_id,created_at,context_snapshot"
        f"&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )
    trading_positions = _safe_rows(
        "trading_positions",
        "select=id,strategy,symbol,direction,capital_usd,status,opened_at,closed_at,total_funding_usd,realized_pnl_usd,close_reason,decision_id,notes"
        f"&order=id.desc&limit={count_limit}",
    )
    output_critiques = _safe_rows(
        "output_critiques",
        "select=id,session_id,source,output_text,score,verdict,failure_pattern,failure_category,reason,suggested_improvement,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )
    causal_chains = _safe_rows(
        "causal_chains",
        "select=id,session_id,source,output_text,why_reasoning,root_knowledge,knowledge_source,reasoning_type,confidence,potential_bias,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )
    output_reflections = _safe_rows(
        "output_reflections",
        "select=id,session_id,source,critique_score,verdict,gap,gap_domain,new_behavior,evo_worthy,prompt_patch,created_at"
        f"&source=eq.{TRADING_DOMAIN}&created_at=gte.{since_ts}&order=created_at.desc&limit={count_limit}",
    )

    source_ids = {str(row.get("id") or "") for row in seed_sources if row.get("id")}
    kb_articles = [
        row for row in all_kb_articles
        if str(row.get("source_id") or "") in source_ids
    ]
    closed_positions = [
        row for row in trading_positions
        if row.get("closed_at") or str(row.get("status") or "").lower() in {"closed", "complete", "completed"}
    ]
    reflection_count = len(output_critiques) + len(causal_chains) + len(output_reflections) + len(hot_reflections)
    targets = TRADING_READINESS_TARGETS
    source_seed_ready = (
        len(seed_sources) >= targets["seed_sources"]
        and len(kb_articles) >= targets["seed_articles"]
        and len(external_concepts) >= targets["seed_concepts"]
    )
    seed_ready = (
        len(rules) >= targets["rules"]
        and len(kb) >= targets["knowledge_base"]
        and len(trading_meta_kb) >= targets["trading_meta_kb"]
        and len(memory_mistakes) >= targets["memory_mistakes"]
        and len(hot_reflections) >= targets["hot_reflections"]
        and source_seed_ready
    )
    fresh_signal_count = (
        len(seed_sources)
        + len(kb_articles)
        + len(external_concepts)
        + len(trading_patterns)
        + len(trading_mistakes)
        + len(trading_decisions)
        + len(closed_positions)
        + reflection_count
    )

    return {
        "ok": True,
        "mode": "trading_only",
        "specialization": CORE_SPECIALIZATION,
        "training_mode": CORE_TRAINING_MODE,
        "since_ts": since_ts,
        "seed_ready": seed_ready,
        "fresh_signal_count": fresh_signal_count,
        "verified": seed_ready,
        "counts": {
            "rules": len(rules),
            "knowledge_base": len(kb),
            "trading_meta_kb": len(trading_meta_kb),
            "seed_sources": len(seed_sources),
            "seed_articles": len(kb_articles),
            "seed_concepts": len(external_concepts),
            "memory_mistakes": len(memory_mistakes),
            "hot_reflections": len(hot_reflections),
            "trading_patterns": len(trading_patterns),
            "trading_mistakes": len(trading_mistakes),
            "trading_decisions": len(trading_decisions),
            "trading_positions": len(trading_positions),
            "closed_positions": len(closed_positions),
            "output_critiques": len(output_critiques),
            "causal_chains": len(causal_chains),
            "output_reflections": len(output_reflections),
        },
        "tables": {
            "behavioral_rules": rules[:limit],
            "knowledge_base": kb[:limit],
            "trading_meta_kb": trading_meta_kb[:limit],
            "kb_sources": seed_sources[:limit],
            "kb_articles": kb_articles[:limit],
            "kb_concepts": external_concepts[:limit],
            "mistakes": memory_mistakes[:limit],
            "hot_reflections": hot_reflections[:limit],
            "trading_patterns": trading_patterns[:limit],
            "trading_mistakes": trading_mistakes[:limit],
            "trading_decisions": trading_decisions[:limit],
            "trading_positions": trading_positions[:limit],
            "closed_positions": closed_positions[:limit],
            "output_critiques": output_critiques[:limit],
            "causal_chains": causal_chains[:limit],
            "output_reflections": output_reflections[:limit],
        },
        "summary": (
            f"rules={len(rules)} kb={len(kb)} meta_kb={len(trading_meta_kb)} "
            f"seed_sources={len(seed_sources)} seed_articles={len(kb_articles)} seed_concepts={len(external_concepts)} "
            f"memory_mistakes={len(memory_mistakes)} "
            f"decisions={len(trading_decisions)} closed_positions={len(closed_positions)} "
            f"patterns={len(trading_patterns)} live_mistakes={len(trading_mistakes)} reflections={reflection_count}"
        ),
    }


def build_trading_readiness(limit: int = 12) -> dict:
    packet = build_trading_source_packet(limit=max(limit, 40))
    counts = packet.get("counts", {})
    blockers: list[str] = []
    if counts.get("seed_sources", 0) < TRADING_READINESS_TARGETS["seed_sources"]:
        blockers.append("external_trading_seed_sources_below_target")
    if counts.get("seed_articles", 0) < TRADING_READINESS_TARGETS["seed_articles"]:
        blockers.append("external_trading_seed_articles_below_target")
    if counts.get("seed_concepts", 0) < TRADING_READINESS_TARGETS["seed_concepts"]:
        blockers.append("external_trading_seed_concepts_below_target")
    if counts.get("rules", 0) < TRADING_READINESS_TARGETS["rules"]:
        blockers.append("behavioral_rules_missing")
    if counts.get("knowledge_base", 0) < TRADING_READINESS_TARGETS["knowledge_base"]:
        blockers.append("trading_knowledge_base_empty")
    if counts.get("trading_meta_kb", 0) < TRADING_READINESS_TARGETS["trading_meta_kb"]:
        blockers.append("trading_meta_knowledge_base_below_target")
    if counts.get("memory_mistakes", 0) < TRADING_READINESS_TARGETS["memory_mistakes"]:
        blockers.append("trading_mistake_memory_empty")
    if counts.get("hot_reflections", 0) < TRADING_READINESS_TARGETS["hot_reflections"]:
        blockers.append("trading_seed_reflections_below_target")
    return {
        "ok": len(blockers) == 0,
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "counts": counts,
        "summary": packet.get("summary", ""),
        "sample_sources": packet.get("tables", {}).get("kb_sources", [])[:min(limit, 6)],
        "sample_articles": packet.get("tables", {}).get("kb_articles", [])[:min(limit, 6)],
        "sample_concepts": packet.get("tables", {}).get("kb_concepts", [])[:min(limit, 8)],
        "sample_rules": packet.get("tables", {}).get("behavioral_rules", [])[:min(limit, 6)],
        "sample_kb": packet.get("tables", {}).get("knowledge_base", [])[:min(limit, 8)],
        "sample_meta_kb": packet.get("tables", {}).get("trading_meta_kb", [])[:min(limit, 6)],
        "targets": dict(TRADING_READINESS_TARGETS),
        "fresh_signal_count": packet.get("fresh_signal_count", 0),
    }


def build_trading_curriculum(limit: int = 16, packet: dict | None = None) -> dict:
    packet = packet or build_trading_source_packet(limit=max(limit, 40))
    tables = packet.get("tables", {})
    items: list[dict] = []

    for row in tables.get("trading_decisions", []):
        items.append({
            "work_track": "trading_decision",
            "title": f"{row.get('symbol', '?')} {row.get('strategy', '?')} {row.get('action_taken', '?')}",
            "description": (row.get("reasoning") or "")[:220],
            "status": row.get("action_taken") or "recorded",
            "result": f"regime={row.get('market_regime', '')} risk={row.get('risk_level', '')}",
            "source": "trading_decisions",
            "priority": row.get("confidence"),
        })
    for row in tables.get("closed_positions", []):
        items.append({
            "work_track": "trading_position",
            "title": f"{row.get('symbol', '?')} {row.get('strategy', '?')} closed",
            "description": (row.get("notes") or row.get("close_reason") or "")[:220],
            "status": row.get("status") or "closed",
            "result": f"pnl={row.get('realized_pnl_usd', 0)} funding={row.get('total_funding_usd', 0)}",
            "source": "trading_positions",
            "priority": row.get("realized_pnl_usd"),
        })
    for row in tables.get("trading_mistakes", []):
        items.append({
            "work_track": "trading_mistake",
            "title": (row.get("what_failed") or "trading mistake")[:160],
            "description": (row.get("root_cause") or row.get("market_context") or "")[:220],
            "status": row.get("severity") or "recorded",
            "result": (row.get("how_to_avoid") or "")[:220],
            "source": "trading_mistakes",
            "priority": row.get("loss_usd"),
        })
    for row in tables.get("trading_patterns", []):
        items.append({
            "work_track": "trading_pattern",
            "title": (row.get("pattern_key") or "trading_pattern")[:160],
            "description": (row.get("description") or "")[:220],
            "status": row.get("outcome") or "tracked",
            "result": f"win_rate={row.get('win_rate', 0)} avg_pnl={row.get('avg_pnl_usd', 0)}",
            "source": "trading_patterns",
            "priority": row.get("win_rate"),
        })
    for row in tables.get("output_reflections", []):
        items.append({
            "work_track": "trading_reflection",
            "title": (row.get("gap") or row.get("new_behavior") or "trading_reflection")[:160],
            "description": (row.get("prompt_patch") or "")[:220],
            "status": row.get("verdict") or "reflected",
            "result": (row.get("new_behavior") or "")[:220],
            "source": "output_reflections",
            "priority": row.get("critique_score"),
        })
    for row in tables.get("behavioral_rules", []):
        items.append({
            "work_track": "trading_rule",
            "title": (row.get("pointer") or row.get("trigger") or "trading_rule")[:160],
            "description": (row.get("full_rule") or "")[:220],
            "status": "active",
            "result": row.get("trigger") or "",
            "source": "behavioral_rules",
            "priority": row.get("priority"),
        })
    for row in tables.get("knowledge_base", []):
        items.append({
            "work_track": "trading_kb",
            "title": (row.get("topic") or "trading_kb")[:160],
            "description": (row.get("instruction") or row.get("content") or "")[:220],
            "status": row.get("confidence") or "seeded",
            "result": row.get("source") or "",
            "source": "knowledge_base",
            "priority": row.get("confidence"),
        })
    for row in tables.get("kb_concepts", []):
        items.append({
            "work_track": "trading_concept",
            "title": (row.get("concept_name") or "trading_concept")[:160],
            "description": (row.get("definition") or "")[:220],
            "status": row.get("trend") or "tracked",
            "result": f"sources={row.get('source_count', 0)} avg_eng={row.get('avg_engagement', 0)}",
            "source": "kb_concepts",
            "priority": row.get("source_count"),
        })
    for row in tables.get("kb_sources", []):
        items.append({
            "work_track": "trading_source",
            "title": (row.get("title") or row.get("url") or "trading_source")[:160],
            "description": (row.get("source_platform") or "")[:220],
            "status": row.get("source_type") or "seeded",
            "result": f"trust={row.get('trust_level', 0)} engagement={row.get('engagement_score', 0)}",
            "source": "kb_sources",
            "priority": row.get("engagement_score"),
        })
    for row in tables.get("mistakes", []):
        items.append({
            "work_track": "trading_memory",
            "title": (row.get("what_failed") or "trading_memory")[:160],
            "description": (row.get("root_cause") or "")[:220],
            "status": row.get("severity") or "seeded",
            "result": (row.get("how_to_avoid") or "")[:220],
            "source": "mistakes",
            "priority": row.get("severity"),
        })

    deduped: list[dict] = []
    seen = set()
    for item in items:
        key = (item.get("work_track"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    counts = Counter(item.get("work_track") or "general" for item in deduped)
    return {"counts": dict(counts), "items": deduped, "source_packet": packet}
