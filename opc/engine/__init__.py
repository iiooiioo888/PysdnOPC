"""OPC Engine package — backwards-compatible re-exports.

This package was split from the monolithic opc/engine.py into mixin-based
modules for maintainability. External code continues to use:

    from opc.engine import OPCEngine
"""

from opc.engine._core import OPCEngine, ExternalRecruiterLLMAdapter

__all__ = ["OPCEngine", "ExternalRecruiterLLMAdapter"]
