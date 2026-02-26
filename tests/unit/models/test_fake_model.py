from twin.models.fake_model import FakeEmbeddingModel, FakeLLM


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
