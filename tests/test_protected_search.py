from memory_plane.services.protected_search import protected_tokens


def test_protected_tokens_are_normalized_unique_and_not_plaintext() -> None:
    tokens = protected_tokens("Alpha alpha Бета", "search-key")

    assert len(tokens) == 2
    assert all(len(token) == 32 for token in tokens)
    assert b"alpha" not in tokens
    assert tokens == protected_tokens("ALPHA бета", "search-key")
