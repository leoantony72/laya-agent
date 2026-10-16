"""
winbrow.brain — System 2 planning/execution package.

  model_gateway — provider-agnostic LLM access (OpenRouter/DeepSeek default)
  prompts       — planner/chat/normalizer prompt templates
  agent_loop    — plan → execute → verify → recover loop
"""

from .model_gateway import (
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    ToolCall,
    get_gateway,
    reset_gateway,
)
from .agent_loop import (
    AgentBrain,
    BrainResult,
    BrainStepResult,
)

__all__ = [
    "ModelGateway",
    "GatewayError",
    "GatewayUnavailable",
    "GatewayResponse",
    "ToolCall",
    "get_gateway",
    "reset_gateway",
    "AgentBrain",
    "BrainResult",
    "BrainStepResult",
]
