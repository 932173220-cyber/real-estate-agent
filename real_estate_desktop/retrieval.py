"""Lexical retrieval and rank fusion.

The lexical half of the hybrid retriever is Okapi BM25 over jieba-segmented
Chinese text (with character bigrams for recall). Dense and lexical rankings
are merged with Reciprocal Rank Fusion, which needs no score calibration and
keeps a document that both retrievers like at the top.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fff]")
_tokenizer = None


def _get_tokenizer():
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    try:
        import jieba

        jieba.setLogLevel(logging.WARNING)
        _tokenizer = jieba
    except Exception as exc:  # pragma: no cover - only without jieba
        logger.warning("jieba unavailable (%s); BM25 falls back to character n-grams", exc)
        _tokenizer = False
    return _tokenizer


def tokenize(text: str) -> list[str]:
    """Segment Chinese with jieba and keep ASCII words; add bigrams for recall."""
    text = (text or "").strip().lower()
    if not text:
        return []
    jieba = _get_tokenizer()
    if jieba:
        words = [word.strip() for word in jieba.cut_for_search(text)]
    else:
        words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text)
    tokens = [word for word in words if word and not word.isspace()]
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


class BM25Index:
    """Okapi BM25 (k1=1.5, b=0.75) over an in-memory chunk corpus."""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._ids: list[str] = []
        self._rows: dict[str, int] = {}
        self._tf: list[Counter] = []
        self._length: list[int] = []
        self._df: Counter = Counter()
        self._avg = 0.0
        self._ready = False

    def build(self, rows: list[tuple[str, str]]) -> None:
        self._ids = [row[0] for row in rows]
        self._rows = {row[0]: index for index, row in enumerate(rows)}
        self._tf = []
        self._length = []
        self._df = Counter()
        for _chunk_id, text in rows:
            tokens = tokenize(text)
            counts = Counter(tokens)
            self._tf.append(counts)
            self._length.append(len(tokens))
            self._df.update(counts.keys())
        self._avg = (sum(self._length) / len(self._length)) if self._length else 0.0
        self._ready = bool(self._ids)

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def document_ids(self) -> list[str]:
        return list(self._ids)

    def search(self, query: str, *, limit: int = 10, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        if not self._ready:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        total = len(self._ids)
        scores: list[tuple[str, float]] = []
        for index, chunk_id in enumerate(self._ids):
            if allowed is not None and chunk_id not in allowed:
                continue
            counts = self._tf[index]
            length = self._length[index] or 0
            score = 0.0
            for token in tokens:
                frequency = counts.get(token)
                if not frequency:
                    continue
                df = self._df.get(token, 0)
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                denominator = frequency + self.k1 * (1 - self.b + self.b * (length / (self._avg or 1.0)))
                score += idf * (frequency * (self.k1 + 1)) / denominator
            if score > 0:
                scores.append((chunk_id, round(score, 6)))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return scores[:limit]


def rrf_fuse(rankings, *, k: int = 60, weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score(d) = sum(weight / (k + rank))."""
    if k <= 0:
        raise ValueError("RRF k must be positive")
    fused: dict[str, float] = {}
    for position, ranking in enumerate(rankings):
        weight = 1.0 if not weights else float(weights[position])
        for rank, (doc_id, _score) in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))
