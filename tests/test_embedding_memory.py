"""Regression tests for embedding memory and its optional Gym wrapper."""

from cognicore.memory import embedding


def test_cognitive_gym_wrapper_tracks_experience():
    base = embedding.gym.Env if embedding.gym is not None else object

    class FakeEnv(base):
        def reset(self, **kwargs):
            return "start", {}

        def step(self, action):
            return "next", 1.0, False, False, {"event": "goal"}

    wrapper = embedding.CognitiveGymWrapper(FakeEnv(), memory_size=10, top_k=1)
    wrapper.memory._model = "random"

    observation, info = wrapper.reset()
    assert observation == "start"
    assert info["cognicore_memory"] == []

    observation, reward, terminated, truncated, info = wrapper.step(1)
    assert (observation, reward, terminated, truncated) == ("next", 1.0, False, False)
    assert wrapper.memory.size == 1
    assert info["cognicore_memory_stats"]["stored"] == 1


def test_wrapper_inherits_gym_wrapper_when_available():
    if embedding.gym is not None:
        assert issubclass(embedding.CognitiveGymWrapper, embedding.gym.Wrapper)
