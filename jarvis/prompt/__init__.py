"""Prompt management: versioned templates and rendering.

Responsibility (delivered from phase 5 onwards):
    * Store, version and render prompt templates for every agent; no
      prompt text is hardcoded inside agent/business code.

Why this package exists rather than a constant per module: the wording *is* the
behaviour for a prompt-driven assistant, and it used to be spread across five
packages with no way to read it together, diff it, or change it without a code
edit. :mod:`jarvis.prompt.templates` is now the single answer to "what does this
assistant actually tell the model".

Usage:

* Consumers that might be retuned by an operator call
  :func:`render_prompt` **at the point of use** — that is what lets
  ``prompt.overrides`` from the config file take effect.
* Tests and anything that needs the shipped text regardless of overrides import
  the template constant from :mod:`jarvis.prompt.templates`.

Allowed dependencies: ``core``, ``config``.
"""

from jarvis.prompt.registry import PromptRegistry, default_registry, render_prompt
from jarvis.prompt.service import PromptService
from jarvis.prompt.templates import (
    AGENT_CONVERSATIONAL,
    BUILTIN_TEMPLATES,
    DEMO_ASSISTANT,
    HUD_ASSISTANT,
    KNOWLEDGE_ANSWER,
    MEMORY_EXTRACTION,
    MEMORY_SUMMARY,
    PLANNER_DECOMPOSE,
    PLANNER_REPLAN,
    SUPERVISOR_ROUTING,
    VISION_DESCRIBE,
)
from jarvis.prompt.types import PromptOverride, PromptTemplate

__all__ = [
    "AGENT_CONVERSATIONAL",
    "BUILTIN_TEMPLATES",
    "DEMO_ASSISTANT",
    "HUD_ASSISTANT",
    "KNOWLEDGE_ANSWER",
    "MEMORY_EXTRACTION",
    "MEMORY_SUMMARY",
    "PLANNER_DECOMPOSE",
    "PLANNER_REPLAN",
    "SUPERVISOR_ROUTING",
    "VISION_DESCRIBE",
    "PromptOverride",
    "PromptRegistry",
    "PromptService",
    "PromptTemplate",
    "default_registry",
    "render_prompt",
]
