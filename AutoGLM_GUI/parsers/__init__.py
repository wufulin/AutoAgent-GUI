"""Action parsers for different agent types.

This module provides parser implementations for converting model outputs
into standardized action dictionaries that can be executed by ActionHandler.

Each agent type has its own parser implementation:
- GLMParser: For GLM-based agents (enhanced AST parsing)
"""

from AutoGLM_GUI.agents.glm.parser import GLMParser

__all__ = [
    "GLMParser",
]
