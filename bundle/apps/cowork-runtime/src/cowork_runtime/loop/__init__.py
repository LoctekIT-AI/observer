"""Agent loop primitives with lazy public exports."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "AgentLoop": ("agent_loop", "AgentLoop"),
    "CancellationRequested": ("cancellation", "CancellationRequested"),
    "CancellationToken": ("cancellation", "CancellationToken"),
    "AdaptiveDecision": ("adaptive_reasoning", "AdaptiveDecision"),
    "AdaptiveReasoningInput": ("adaptive_reasoning", "AdaptiveReasoningInput"),
    "AdaptiveReasoningPolicy": ("adaptive_reasoning", "AdaptiveReasoningPolicy"),
    "CONTEXT_WINDOW_METADATA_KEY": ("context_window", "CONTEXT_WINDOW_METADATA_KEY"),
    "TurnLifecycle": ("lifecycle", "TurnLifecycle"),
    "TurnPhase": ("lifecycle", "TurnPhase"),
    "TraceProjection": ("execution_trace", "TraceProjection"),
    "reconstruct_projection": ("execution_trace", "reconstruct_projection"),
    "RecipeCandidate": ("recipe_candidate", "RecipeCandidate"),
    "build_recipe_candidate": ("recipe_candidate", "build_recipe_candidate"),
    "ModularDeliveryVerdict": ("modular_delivery", "ModularDeliveryVerdict"),
    "evaluate_modular_delivery": ("modular_delivery", "evaluate_modular_delivery"),
    "LoopBudget": ("budget", "LoopBudget"),
    "LoopBudgetExceeded": ("budget", "LoopBudgetExceeded"),
    "BUDGET_PROFILES": ("budget", "BUDGET_PROFILES"),
    "budget_from_snapshot": ("budget", "budget_from_snapshot"),
    "budget_snapshot": ("budget", "budget_snapshot"),
    "resolve_loop_budget": ("budget", "resolve_loop_budget"),
    "LoopInput": ("types", "LoopInput"),
    "CrawlerInteractionResume": ("types", "CrawlerInteractionResume"),
    "LoopResult": ("types", "LoopResult"),
    "LoopResume": ("types", "LoopResume"),
    "ModelTurnOutput": ("types", "ModelTurnOutput"),
    "PolicyDecision": ("types", "PolicyDecision"),
    "ToolCallIntent": ("types", "ToolCallIntent"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
