from typing import List
from .base import EmbeddingProvider

class TFIDFEmbeddingProvider(EmbeddingProvider):
    """
    Zero-dependency TF-IDF 'embeddings' provider.
    TF-IDF is a lexical retrieval method, not a semantic dense embedder.
    This class is provided only for typing/interface compliance. Calling embed()
    will raise a NotImplementedError to prevent silent failures with fake zero-vectors.
    """

    def embed(self, text: str) -> List[float]:
        raise NotImplementedError(
            "TF-IDF is a lexical retrieval method and does not produce dense semantic embeddings. "
            "Use a DenseEmbeddingProvider (e.g., sentence-transformers) for functioning semantic retrieval."
        )

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError(
            "TF-IDF does not support dense batch embeddings."
        )

    @property
    def dimension(self) -> int:
        return 0
