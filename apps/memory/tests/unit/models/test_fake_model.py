import pytest

from tree.models.fake_model import FakeEmbeddingModel, FakeLLM, MockEmbeddingModel


class TestFakeLLM:
    async def test_returns_canned_responses(self):
        responses = [
            {"nodes": [{"name": "alice"}], "edges": []},
            {"nodes": [], "edges": [{"type": "related_to"}]},
        ]
        llm = FakeLLM(responses=responses)

        first = await llm.generate_json("prompt 1")
        second = await llm.generate_json("prompt 2")

        assert first == responses[0]
        assert second == responses[1]

    async def test_returns_empty_when_exhausted(self):
        llm = FakeLLM(responses=[{"nodes": [{"name": "bob"}], "edges": []}])

        await llm.generate_json("first call")
        result = await llm.generate_json("second call")

        assert result == {"nodes": [], "edges": []}

    async def test_tracks_call_count(self):
        llm = FakeLLM()

        assert llm.call_count == 0
        await llm.generate_json("call")
        assert llm.call_count == 1

    async def test_default_empty_responses(self):
        llm = FakeLLM()
        result = await llm.generate_json("anything")

        assert result == {"nodes": [], "edges": []}


class TestFakeEmbeddingModel:
    async def test_returns_zero_vectors(self):
        model = FakeEmbeddingModel(dimensions=4)
        vectors = await model.embed(["hello", "world"])

        assert len(vectors) == 2
        assert vectors[0] == [0.0, 0.0, 0.0, 0.0]
        assert vectors[1] == [0.0, 0.0, 0.0, 0.0]

    async def test_empty_input(self):
        model = FakeEmbeddingModel()
        vectors = await model.embed([])

        assert vectors == []


class TestInputTypeIgnored:
    """The test doubles accept the **Embedding role** and ignore it — a role
    is a retrieval hint, never a correctness input (ADR-009 §5)."""

    @pytest.mark.parametrize("role", [None, "query", "document"])
    async def test_fake_returns_identical_vectors_for_every_role(self, role):
        model = FakeEmbeddingModel(dimensions=4)

        vectors = await model.embed(["hello"], input_type=role)

        assert vectors == [[0.0, 0.0, 0.0, 0.0]]

    @pytest.mark.parametrize("role", [None, "query", "document"])
    async def test_mock_returns_identically_shaped_vectors_for_every_role(self, role):
        # Random values by design — compare on shape only.
        model = MockEmbeddingModel(dimensions=4)

        vectors = await model.embed(["hello", "world"], input_type=role)

        assert [len(v) for v in vectors] == [4, 4]
