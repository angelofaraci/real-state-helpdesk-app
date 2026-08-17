"""Document chunker for RAG ingestion.

Splits a knowledge base article's raw text into token-budgeted `Chunk`s so
each chunk fits within an embedding model's context window, while trying to
preserve semantic boundaries (paragraphs, then sentences) as much as
possible before falling back to a hard token-boundary split.

Algorithm:
1. Split the document on blank lines (`\\n\\s*\\n`) into paragraphs.
2. Greedily pack paragraphs into chunks while the running token count stays
   within `max_tokens`.
3. A paragraph that alone exceeds `max_tokens` is split into sentences
   (`re.split(r"(?<=[.!?])\\s+", ...)`) and those sentences are packed the
   same way.
4. A single sentence that still exceeds `max_tokens` is hard-split on raw
   token boundaries via the tokenizer.
5. Every chunk after the first is prefixed with the trailing sentences of
   the previous (pre-overlap) packed chunk whose cumulative token count is
   `<= overlap_tokens`.

`tiktoken` is used ONLY for counting tokens and for the token-boundary hard
split — it is never used as (nor should it be confused with) an embedding
tokenizer. See `app.services.rag_embeddings` for the embedding providers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    token_count: int


def _count_tokens(encoding: "tiktoken.Encoding", text: str) -> int:
    return len(encoding.encode(text))


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]


def _hard_split_by_tokens(
    encoding: "tiktoken.Encoding", text: str, max_tokens: int
) -> list[str]:
    tokens = encoding.encode(text)
    return [
        encoding.decode(tokens[i : i + max_tokens])
        for i in range(0, len(tokens), max_tokens)
    ]


def _units_for_paragraph(
    encoding: "tiktoken.Encoding", paragraph: str, max_tokens: int
) -> list[str]:
    """Return the packable units for one paragraph: itself if it fits, or
    its sentences (hard-split further if a sentence alone is oversized)."""
    if _count_tokens(encoding, paragraph) <= max_tokens:
        return [paragraph]

    units: list[str] = []
    for sentence in _split_sentences(paragraph):
        if _count_tokens(encoding, sentence) <= max_tokens:
            units.append(sentence)
        else:
            units.extend(_hard_split_by_tokens(encoding, sentence, max_tokens))
    return units


def _pack_units(
    encoding: "tiktoken.Encoding", units: list[str], max_tokens: int
) -> list[str]:
    """Greedily pack units (paragraphs or sentences) into chunk contents,
    each staying within `max_tokens` when possible."""
    packed: list[str] = []
    current_units: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = _count_tokens(encoding, unit)
        if current_units and current_tokens + unit_tokens > max_tokens:
            packed.append("\n\n".join(current_units))
            current_units = [unit]
            current_tokens = unit_tokens
        else:
            current_units.append(unit)
            current_tokens += unit_tokens

    if current_units:
        packed.append("\n\n".join(current_units))

    return packed


def _overlap_prefix(
    encoding: "tiktoken.Encoding", previous_content: str, overlap_tokens: int
) -> str:
    """Return the trailing sentences of `previous_content` whose cumulative
    token count is `<= overlap_tokens`, joined back into text."""
    if overlap_tokens <= 0:
        return ""

    sentences = _split_sentences(previous_content)
    overlap_sentences: list[str] = []
    running_tokens = 0
    for sentence in reversed(sentences):
        sentence_tokens = _count_tokens(encoding, sentence)
        if running_tokens + sentence_tokens > overlap_tokens:
            break
        overlap_sentences.insert(0, sentence)
        running_tokens += sentence_tokens

    return " ".join(overlap_sentences)


def chunk_document(
    text: str,
    *,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    encoding_name: str = "cl100k_base",
) -> list[Chunk]:
    """Split `text` into token-budgeted `Chunk`s.

    Returns `[]` for empty/whitespace-only input. `Chunk.index` is
    sequential starting at 0.
    """
    if not text or not text.strip():
        return []

    encoding = tiktoken.get_encoding(encoding_name)

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text.strip()) if p.strip()]

    units: list[str] = []
    for paragraph in paragraphs:
        units.extend(_units_for_paragraph(encoding, paragraph, max_tokens))

    packed = _pack_units(encoding, units, max_tokens)

    chunks: list[Chunk] = []
    for i, content in enumerate(packed):
        if i == 0:
            final_content = content
        else:
            prefix = _overlap_prefix(encoding, packed[i - 1], overlap_tokens)
            final_content = f"{prefix}\n\n{content}" if prefix else content

        chunks.append(
            Chunk(
                index=i,
                content=final_content,
                token_count=_count_tokens(encoding, final_content),
            )
        )

    return chunks
