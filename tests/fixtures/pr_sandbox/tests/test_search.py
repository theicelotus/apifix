from unittest import mock

from supportbot import search


def vectors(*rows):
    return {"data": [{"embedding": list(row)} for row in rows]}


@mock.patch("openai.Embedding.create")
def test_embed_returns_one_vector_per_text(create):
    create.return_value = vectors([0.1, 0.2], [0.3, 0.4])
    assert search.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert create.call_args.kwargs["input"] == ["a", "b"]


@mock.patch("openai.Embedding.create")
def test_search_picks_the_closest_document(create):
    create.return_value = vectors([0, 1, 0], [1, 0, 0], [0.1, 0.9, 0], [0, 0, 1])
    assert search.search("invoice question") == "Billing and invoices"
