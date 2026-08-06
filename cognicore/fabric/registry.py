from typing import Optional
from cognicore.fabric.engine import CognitiveFabric
from cognicore.memory.base import MemoryBackend

# Global fabric instance
_global_fabric: Optional[CognitiveFabric] = None

def get_fabric(backend: MemoryBackend) -> CognitiveFabric:
    """
    Returns a singleton instance of the CognitiveFabric attached to the given backend.
    """
    global _global_fabric
    if _global_fabric is None or _global_fabric.backend is not backend:
        _global_fabric = CognitiveFabric(backend)
    return _global_fabric

# Import and register builtin plugins here once they are defined
def register_all_plugins(fabric: CognitiveFabric):
    try:
        from cognicore.fabric.plugins.elevenlabs import ElevenLabsAdapter
        fabric.register_adapter("elevenlabs", ElevenLabsAdapter)
    except ImportError:
        pass
