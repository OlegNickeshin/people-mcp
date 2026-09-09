import re
import unicodedata
from threading import Lock

import numpy as np
from fastembed import TextEmbedding
from tokenizers import Tokenizer

from server.config import DIMENSIONS, MODEL_NAME


def normalize_content(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_content(content: str, tokenizer: Tokenizer, size: int = 192, overlap: int = 32) -> list[str]:
    """Exact source excerpts; token windows keep every passage below model limits."""
    if not 0 <= overlap < size:
        raise ValueError("overlap must be smaller than chunk size")
    # Keep short profiles intact: separating skills from intentions can create
    # generic fragments whose meaning no longer represents the person.
    chunks = []
    offsets = tokenizer.encode(content, add_special_tokens=False).offsets
    for start in range(0, len(offsets), size - overlap):
        end = min(start + size, len(offsets))
        text = content[offsets[start][0]:offsets[end - 1][1]].strip()
        if text:
            chunks.append(text)
        if end == len(offsets):
            break
    return chunks


class LocalEmbedder:
    def __init__(self, cache_dir: str):
        self.model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir, threads=1)
        # A separate tokenizer prevents chunking from changing inference truncation.
        self.tokenizer = Tokenizer.from_str(self.model.model.tokenizer.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.lock = Lock()

    @staticmethod
    def validate(vectors: list[np.ndarray], expected: int) -> list[np.ndarray]:
        if len(vectors) != expected or any(
            v.shape != (DIMENSIONS,) or not np.isfinite(v).all() or np.linalg.norm(v) == 0
            for v in vectors
        ):
            raise RuntimeError("Embedding model returned invalid vectors")
        return vectors

    def documents(self, chunks: list[str]) -> list[np.ndarray]:
        with self.lock:
            vectors = list(self.model.passage_embed(chunks, batch_size=16))
        return self.validate(vectors, len(chunks))

    def query(self, text: str) -> np.ndarray:
        if len(self.tokenizer.encode(text, add_special_tokens=False).ids) > 480:
            raise ValueError("Query exceeds 480 model tokens; shorten it")
        with self.lock:
            vectors = list(self.model.query_embed(text))
        return self.validate(vectors, 1)[0]

    def index(self, content: str) -> list[tuple[str, np.ndarray]]:
        chunks = chunk_content(content, self.tokenizer)
        if not chunks:
            raise ValueError("Content has no indexable text")
        return list(zip(chunks, self.documents(chunks), strict=True))
