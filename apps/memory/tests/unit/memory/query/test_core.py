import pytest

from twin.memory.query.core import _rrf_fuse


class TestRRFFuse:
    def test_single_list(self):
        vector = [{"_id": "a", "score": 0.9}, {"_id": "b", "score": 0.8}]
        fused = _rrf_fuse(vector, [], k=60)

        assert "a" in fused
        assert "b" in fused
        assert fused["a"]["score"] > fused["b"]["score"]

    def test_both_lists_boost_shared(self):
        vector = [{"_id": "a"}, {"_id": "b"}]
        text = [{"_id": "b"}, {"_id": "c"}]
        fused = _rrf_fuse(vector, text, k=60)

        # "b" appears in both lists → highest score.
        assert fused["b"]["score"] > fused["a"]["score"]
        assert fused["b"]["score"] > fused["c"]["score"]

    def test_empty_lists(self):
        fused = _rrf_fuse([], [])
        assert fused == {}

    def test_scores_are_positive(self):
        results = [{"_id": f"node-{i}"} for i in range(5)]
        fused = _rrf_fuse(results, [], k=60)

        for item in fused.values():
            assert item["score"] > 0

    def test_rrf_formula(self):
        """Verify the exact RRF score for a simple case."""
        vector = [{"_id": "x"}]
        text = [{"_id": "x"}]
        fused = _rrf_fuse(vector, text, k=10)

        # rank=0 → score = 1/(10+1) + 1/(10+1) = 2/11
        expected = 2.0 / 11.0
        assert abs(fused["x"]["score"] - expected) < 1e-9

    @pytest.mark.parametrize("k", [1, 10, 60, 100])
    def test_different_k_values(self, k):
        vector = [{"_id": "a"}, {"_id": "b"}]
        text = [{"_id": "b"}, {"_id": "a"}]
        fused = _rrf_fuse(vector, text, k=k)

        # Both appear in both lists at different ranks.
        # "a": 1/(k+1) + 1/(k+2), "b": 1/(k+2) + 1/(k+1) → equal.
        assert abs(fused["a"]["score"] - fused["b"]["score"]) < 1e-9
