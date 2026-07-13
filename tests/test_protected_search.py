from memory_plane.services.protected_search import (
    protected_document_marker,
    protected_index_digests,
    protected_tokens,
)


def test_protected_tokens_are_normalized_unique_and_not_plaintext() -> None:
    tokens = protected_tokens("Alpha alpha Бета", "search-key")

    assert len(tokens) == 2
    assert all(len(token) == 32 for token in tokens)
    assert b"alpha" not in tokens
    assert tokens == protected_tokens("ALPHA бета", "search-key")


def test_protected_document_marker_covers_tokenless_text_without_being_query_term() -> None:
    marker = protected_document_marker("search-key")

    assert len(marker) == 32
    assert protected_tokens("🤖", "search-key") == ()
    assert protected_index_digests("🤖", "search-key") == (marker,)
