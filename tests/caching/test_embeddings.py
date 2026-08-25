from gatekeep.caching.embeddings import embed_text, is_too_long


def test_embed_text_returns_384_dim_vector():
    vector = embed_text("hello world")
    assert vector is not None
    assert len(vector) == 384


def test_embed_text_is_deterministic():
    v1 = embed_text("what is the capital of France?")
    v2 = embed_text("what is the capital of France?")
    assert v1 == v2


def _cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b)


def test_embed_text_similar_texts_have_high_similarity():
    v1 = embed_text("What is the capital of France?")
    v2 = embed_text("What's the capital city of France?")
    assert _cosine_similarity(v1, v2) > 0.9


def test_embed_text_dissimilar_texts_have_lower_similarity():
    v1 = embed_text("What is the capital of France?")
    v2 = embed_text("Please write a haiku about a walrus.")
    assert _cosine_similarity(v1, v2) < 0.7


def test_is_too_long_false_for_short_text():
    assert is_too_long("a short message") is False


def test_is_too_long_true_for_long_text():
    assert is_too_long("word " * 2000) is True


def test_embed_text_returns_none_when_too_long():
    assert embed_text("word " * 2000) is None
