from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from cognicore.fabric.engine import CognitiveFabric

class CognitiveAdapter(ABC):
    """
    Base class for connecting a tool to the Cognitive Fabric.
    Adapters are intentionally thin: they observe facts and request recommendations.
    The Fabric handles pattern discovery, rule generation, and translation.
    """
    
    def __init__(self, fabric: 'CognitiveFabric'):
        self.fabric = fabric
        
    @abstractmethod
    def observe(self, action: str, context: Dict[str, Any]) -> str:
        """
        Layer 1: Raw observation.
        Record what the tool is doing so the Fabric can discover patterns.
        
        Args:
            action: The action performed (e.g., "styled_component", "generated_audio")
            context: Contextual details (e.g., {"color": "#FFF", "padding": "16px"})
            
        Returns:
            observation_id (str)
        """
        pass

    @abstractmethod
    def learn(self, **kwargs) -> str:
        """
        Process observations into structural learnings.
        Typically calls back to self.fabric.record_learning().
        """
        pass
        
    @abstractmethod
    def feedback(self, action_id: str, success_score: float, **kwargs) -> None:
        """
        Layer 4: Reflection.
        Attach human or system feedback to a previously recorded action so the Fabric 
        can validate or discard its generated rules.
        """
        pass

    @abstractmethod
    def recommend(self, **kwargs) -> Dict[str, Any]:
        """
        Layer 3: Translation.
        Request instructions for this specific tool based on the Fabric's universal rules.
        The Fabric translates its high-level concepts into tool-specific actionable data.
        """
        pass
