"""Model turn execution wrapper."""

from __future__ import annotations

from .types import LoopContext, ModelClient, ModelTurnOutput


async def run_model_turn(model_client: ModelClient, context: LoopContext) -> ModelTurnOutput:
    return await context.cancellation_token.run(model_client.complete(context))
