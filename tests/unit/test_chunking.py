"""Unit tests for `app.services.chunking.chunk_document`.

Token counts are computed directly via `tiktoken` (the same encoding the
production code uses) so test expectations are derived from the real
tokenizer rather than guessed magic numbers.
"""

import tiktoken

from app.services.chunking import Chunk, chunk_document

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def test_chunk_document_returns_empty_list_for_empty_input() -> None:
    assert chunk_document("") == []


def test_chunk_document_returns_empty_list_for_whitespace_only_input() -> None:
    assert chunk_document("   \n\n   \t  ") == []


def test_chunk_document_packs_short_paragraphs_into_a_single_chunk_when_budget_allows() -> None:
    para1 = "A cat sat on the mat."
    para2 = "A dog sat on the log."
    text = f"{para1}\n\n{para2}"
    combined_tokens = _tokens(f"{para1}\n\n{para2}")

    chunks = chunk_document(text, max_tokens=combined_tokens, overlap_tokens=0)

    assert len(chunks) == 1
    assert chunks[0] == Chunk(index=0, content=text, token_count=combined_tokens)


def test_chunk_document_splits_paragraphs_into_separate_chunks_when_budget_is_tight() -> None:
    para1 = "A cat sat on the mat."
    para2 = "A dog sat on the log."
    text = f"{para1}\n\n{para2}"
    max_tokens = _tokens(para1)

    chunks = chunk_document(text, max_tokens=max_tokens, overlap_tokens=0)

    assert len(chunks) == 2
    assert [c.index for c in chunks] == [0, 1]
    assert chunks[0].content == para1
    assert chunks[1].content == para2
    assert chunks[0].token_count == _tokens(para1)
    assert chunks[1].token_count == _tokens(para2)


def test_chunk_document_splits_oversized_paragraph_on_sentence_boundaries() -> None:
    sentences = [
        "Sentence number one is here.",
        "Sentence number two is here.",
        "Sentence number three is here.",
        "Sentence number four is here.",
    ]
    paragraph = " ".join(sentences)
    max_tokens = _tokens(sentences[0])

    chunks = chunk_document(paragraph, max_tokens=max_tokens, overlap_tokens=0)

    assert len(chunks) == 4
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert [c.content for c in chunks] == sentences
    for chunk in chunks:
        assert chunk.token_count <= max_tokens


def test_chunk_document_prefixes_each_chunk_after_the_first_with_overlap_from_previous() -> None:
    sentences = [
        "Sentence number one is here.",
        "Sentence number two is here.",
        "Sentence number three is here.",
        "Sentence number four is here.",
    ]
    paragraph = " ".join(sentences)
    max_tokens = _tokens(sentences[0])
    overlap_tokens = _tokens(sentences[0])

    chunks = chunk_document(paragraph, max_tokens=max_tokens, overlap_tokens=overlap_tokens)

    assert len(chunks) == 4
    # First chunk has no overlap prefix.
    assert chunks[0].content == sentences[0]
    # Every subsequent chunk is prefixed with the previous chunk's (whole)
    # sentence, since overlap_tokens equals exactly one sentence's budget.
    for i in range(1, len(chunks)):
        assert chunks[i].content.startswith(sentences[i - 1])
        assert sentences[i] in chunks[i].content
        assert chunks[i].token_count > _tokens(sentences[i])


def test_chunk_document_hard_splits_a_single_oversized_sentence_on_token_boundaries() -> None:
    # No sentence-ending punctuation: this is one giant "sentence" that must
    # be hard-split on raw token boundaries.
    giant_sentence = " ".join(f"word{i}" for i in range(300))
    max_tokens = 20

    chunks = chunk_document(giant_sentence, max_tokens=max_tokens, overlap_tokens=0)

    assert len(chunks) > 1
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.token_count <= max_tokens
        assert chunk.token_count > 0
    # Reassembling the hard-split pieces recovers all the original words.
    reassembled = "".join(c.content for c in chunks)
    for i in range(300):
        assert f"word{i}" in reassembled
