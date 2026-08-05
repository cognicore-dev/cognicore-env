"""Tests for the CogniCore ElevenLabs Integration."""
import json
import pytest
import tempfile
import os

from cognicore.memory import SQLiteMemoryBackend
from cognicore.integrations.elevenlabs import (
    ElevenLabsIntegration,
    CATEGORY_VOICE,
    CATEGORY_USAGE,
    CATEGORY_ADVANCED,
)


@pytest.fixture
def backend():
    """Create a fresh SQLite backend for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield SQLiteMemoryBackend(path)
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def el(backend):
    """Create a fresh ElevenLabsIntegration for each test."""
    return ElevenLabsIntegration(backend)


# ------------------------------------------------------------------
# Tier 1: Voice Preferences
# ------------------------------------------------------------------

class TestElevenLabsSync:
    """Test storing and recalling voice preferences."""

    def test_sync_stores_voice_preference(self, el, backend):
        result = el.sync(
            voice_id="pNInz6obpgDQGcFmaJgB",
            voice_name="Adam",
            stability=0.8,
            similarity_boost=0.9,
        )
        assert result["status"] == "success"
        assert result["voice_id"] == "pNInz6obpgDQGcFmaJgB"
        assert result["voice_name"] == "Adam"

        # Verify it's actually in the backend
        entries = backend.get_by_category(CATEGORY_VOICE, top_k=5)
        assert len(entries) == 1
        assert entries[0].metadata["voice_id"] == "pNInz6obpgDQGcFmaJgB"

    def test_sync_overwrites_previous(self, el, backend):
        el.sync(voice_id="voice_1", voice_name="Old Voice")
        el.sync(voice_id="voice_2", voice_name="New Voice")

        entries = backend.get_by_category(CATEGORY_VOICE, top_k=5)
        assert len(entries) == 1
        assert entries[0].metadata["voice_name"] == "New Voice"

    def test_sync_stores_all_parameters(self, el):
        el.sync(
            voice_id="test_id",
            voice_name="Test Voice",
            stability=0.6,
            similarity_boost=0.7,
            style_exaggeration=0.3,
            speed=0.9,
            use_speaker_boost=False,
            content_type="meditation",
            audience="25-40 professionals",
            tone="warm and calm",
            language="en",
            model_id="eleven_turbo_v2",
        )
        recalled = el.recall()
        assert recalled["voice_id"] == "test_id"
        assert recalled["voice_settings"]["stability"] == 0.6
        assert recalled["voice_settings"]["similarity_boost"] == 0.7
        assert recalled["voice_settings"]["style"] == 0.3
        assert recalled["voice_settings"]["speed"] == 0.9
        assert recalled["model_id"] == "eleven_turbo_v2"
        assert recalled["content_context"]["content_type"] == "meditation"
        assert recalled["content_context"]["audience"] == "25-40 professionals"
        assert recalled["content_context"]["tone"] == "warm and calm"


class TestElevenLabsRecall:
    """Test recalling voice preferences."""

    def test_recall_returns_defaults_when_empty(self, el):
        recalled = el.recall()
        assert recalled["has_preferences"] is False
        assert recalled["voice_id"] == ""
        assert recalled["voice_settings"]["stability"] == 0.75
        assert recalled["model_id"] == "eleven_multilingual_v2"

    def test_recall_returns_stored_preferences(self, el):
        el.sync(
            voice_id="pNInz6obpgDQGcFmaJgB",
            voice_name="Adam",
            stability=0.8,
            similarity_boost=0.9,
            speed=0.85,
            content_type="podcast",
            tone="energetic",
        )
        recalled = el.recall()
        assert recalled["has_preferences"] is True
        assert recalled["voice_id"] == "pNInz6obpgDQGcFmaJgB"
        assert recalled["voice_settings"]["stability"] == 0.8
        assert recalled["voice_settings"]["similarity_boost"] == 0.9
        assert recalled["voice_settings"]["speed"] == 0.85
        assert recalled["content_context"]["voice_name"] == "Adam"
        assert recalled["content_context"]["content_type"] == "podcast"
        assert recalled["content_context"]["tone"] == "energetic"

    def test_recall_api_format_is_correct(self, el):
        """Verify the output dict has the exact keys ElevenLabs API expects."""
        el.sync(voice_id="test_id", stability=0.75, similarity_boost=0.85)
        recalled = el.recall()

        # Must have these top-level keys
        assert "voice_id" in recalled
        assert "voice_settings" in recalled
        assert "model_id" in recalled

        # voice_settings must have these exact keys
        settings = recalled["voice_settings"]
        assert "stability" in settings
        assert "similarity_boost" in settings
        assert "style" in settings
        assert "speed" in settings
        assert "use_speaker_boost" in settings


# ------------------------------------------------------------------
# Tier 2: Usage Patterns
# ------------------------------------------------------------------

class TestElevenLabsUsagePatterns:
    """Test logging and recalling usage patterns."""

    def test_log_successful_usage(self, el):
        result = el.log_usage(
            voice_used="Adam",
            content_type="meditation",
            audio_length_sec=180.0,
            success=True,
        )
        assert result["status"] == "success"

    def test_log_rejected_usage(self, el):
        result = el.log_usage(
            voice_used="Elli",
            success=False,
            rejection_reason="too corporate",
        )
        assert result["status"] == "success"

    def test_recall_usage_empty(self, el):
        usage = el.recall_usage()
        assert usage["total_generations"] == 0
        assert usage["best_performing_voices"] == []

    def test_recall_usage_aggregates_correctly(self, el):
        # Log several uses
        el.log_usage(voice_used="Adam", content_type="meditation", audio_length_sec=120, success=True)
        el.log_usage(voice_used="Adam", content_type="podcast", audio_length_sec=300, success=True)
        el.log_usage(voice_used="Rachel", content_type="meditation", audio_length_sec=60, success=True)
        el.log_usage(voice_used="Elli", success=False, rejection_reason="too robotic")

        usage = el.recall_usage()
        assert usage["total_generations"] == 4

        # Adam should be best (2 uses)
        assert usage["best_performing_voices"][0]["voice"] == "Adam"
        assert usage["best_performing_voices"][0]["uses"] == 2

        # Should have one rejection
        assert len(usage["rejected_voices"]) == 1
        assert usage["rejected_voices"][0]["voice"] == "Elli"
        assert usage["rejected_voices"][0]["reason"] == "too robotic"

        # Categories
        assert "meditation" in usage["content_categories"]
        assert "podcast" in usage["content_categories"]

        # Average length: (120 + 300 + 60) / 3 = 160.0
        assert usage["avg_audio_length_sec"] == 160.0

    def test_custom_pronunciations_accumulate(self, el):
        el.log_usage(
            voice_used="Adam", success=True,
            custom_pronunciations={"CogniCore": "Cogni-Core"},
        )
        el.log_usage(
            voice_used="Adam", success=True,
            custom_pronunciations={"NEXUS": "NEX-us"},
        )
        usage = el.recall_usage()
        assert usage["custom_pronunciations"]["CogniCore"] == "Cogni-Core"
        assert usage["custom_pronunciations"]["NEXUS"] == "NEX-us"


# ------------------------------------------------------------------
# Tier 3: Advanced Features
# ------------------------------------------------------------------

class TestElevenLabsAdvanced:
    """Test advanced settings storage."""

    def test_set_and_recall_advanced(self, el):
        el.set_advanced(
            remix_preferences={"gender": "male", "accent": "British"},
            pronunciation_dict_id="dict_123",
            use_streaming=True,
            optimize_latency=False,
            has_cloned_voice=True,
            clone_type="instant",
        )
        adv = el.recall_advanced()
        assert adv["has_advanced_config"] is True
        assert adv["remix_preferences"]["gender"] == "male"
        assert adv["pronunciation_dict_id"] == "dict_123"
        assert adv["has_cloned_voice"] is True
        assert adv["clone_type"] == "instant"

    def test_recall_advanced_defaults(self, el):
        adv = el.recall_advanced()
        assert adv["has_advanced_config"] is False
        assert adv["remix_preferences"] == {}
        assert adv["use_streaming"] is True

    def test_set_advanced_overwrites(self, el):
        el.set_advanced(pronunciation_dict_id="old_dict")
        el.set_advanced(pronunciation_dict_id="new_dict")
        adv = el.recall_advanced()
        assert adv["pronunciation_dict_id"] == "new_dict"


# ------------------------------------------------------------------
# Combined Recall
# ------------------------------------------------------------------

class TestElevenLabsRecallAll:
    """Test the combined recall_all method."""

    def test_recall_all_returns_all_tiers(self, el):
        # Set up all three tiers
        el.sync(voice_id="test_id", voice_name="Test")
        el.log_usage(voice_used="Test", content_type="podcast", success=True)
        el.set_advanced(use_streaming=True)

        all_prefs = el.recall_all()

        assert "voice_preferences" in all_prefs
        assert "usage_patterns" in all_prefs
        assert "advanced" in all_prefs
        assert "intelligence" in all_prefs

        assert all_prefs["voice_preferences"]["has_preferences"] is True
        assert all_prefs["usage_patterns"]["total_generations"] == 1
        assert all_prefs["advanced"]["has_advanced_config"] is True

    def test_recall_all_empty(self, el):
        all_prefs = el.recall_all()
        assert all_prefs["voice_preferences"]["has_preferences"] is False
        assert all_prefs["usage_patterns"]["total_generations"] == 0
        assert all_prefs["advanced"]["has_advanced_config"] is False


# ------------------------------------------------------------------
# Layer 2: Intelligence Tests
# ------------------------------------------------------------------

class TestLearnFromGeneration:
    """Test recording generation events."""

    def test_learn_returns_generation_id(self, el):
        gen_id = el.learn_from_generation(
            voice_id="pNInz6obpgDQGcFmaJgB",
            voice_name="Adam",
            stability=0.8,
            speed=0.85,
            content_type="podcast",
            content_text="Welcome to episode 12",
            audio_length_sec=180.0,
        )
        assert gen_id.startswith("gen_")
        assert len(gen_id) > 4

    def test_learn_stores_all_metadata(self, el, backend):
        gen_id = el.learn_from_generation(
            voice_id="test_voice",
            voice_name="TestVoice",
            stability=0.6,
            similarity_boost=0.7,
            speed=0.9,
            content_type="meditation",
            content_text="Breathe deeply and relax",
            audio_length_sec=300.0,
            model_id="eleven_turbo_v2",
        )
        entries = backend.get_by_category("elevenlabs_generation", top_k=5)
        assert len(entries) == 1
        meta = entries[0].metadata
        assert meta["generation_id"] == gen_id
        assert meta["voice_id"] == "test_voice"
        assert meta["stability"] == 0.6
        assert meta["speed"] == 0.9
        assert meta["content_type"] == "meditation"
        assert meta["has_feedback"] is False

    def test_learn_multiple_generations(self, el, backend):
        el.learn_from_generation(voice_id="v1", voice_name="Voice1")
        el.learn_from_generation(voice_id="v2", voice_name="Voice2")
        el.learn_from_generation(voice_id="v1", voice_name="Voice1")

        entries = backend.get_by_category("elevenlabs_generation", top_k=10)
        assert len(entries) == 3


class TestRecordFeedback:
    """Test attaching feedback to generations."""

    def test_record_feedback_success(self, el):
        gen_id = el.learn_from_generation(voice_id="v1", voice_name="Adam")
        result = el.record_feedback(gen_id, rating=4.5, engagement_percent=72.0,
                                     audience_feedback="Perfect pace")
        assert result["status"] == "success"
        assert result["rating"] == 4.5

    def test_record_feedback_not_found(self, el):
        result = el.record_feedback("gen_nonexistent", rating=3.0)
        assert result["status"] == "error"

    def test_feedback_is_retrievable(self, el):
        gen_id = el.learn_from_generation(
            voice_id="v1", voice_name="Adam",
            content_type="podcast", stability=0.8, speed=0.85,
        )
        el.record_feedback(gen_id, rating=4.5, engagement_percent=72.0)

        # Feedback should be visible in recommendations
        rec = el.recommend_voice()
        assert rec["total_generations_analyzed"] == 1


class TestRecommendVoice:
    """Test voice recommendation engine."""

    def test_recommend_with_no_data(self, el):
        rec = el.recommend_voice()
        assert rec["data_source"] == "none"
        assert len(rec["recommendations"]) == 0

    def test_recommend_falls_back_to_preferences(self, el):
        el.sync(voice_id="pref_voice", voice_name="Preferred")
        rec = el.recommend_voice()
        assert rec["data_source"] == "preference"
        assert rec["recommendations"][0]["voice_id"] == "pref_voice"
        assert rec["recommendations"][0]["confidence"] == 0.3

    def test_recommend_from_learned_data(self, el):
        # Generate with two voices, rate one higher
        for _ in range(3):
            gid = el.learn_from_generation(voice_id="v1", voice_name="Adam",
                                            content_type="podcast", stability=0.8, speed=0.85)
            el.record_feedback(gid, rating=4.5, engagement_percent=80)

        for _ in range(3):
            gid = el.learn_from_generation(voice_id="v2", voice_name="Rachel",
                                            content_type="podcast", stability=0.7, speed=1.0)
            el.record_feedback(gid, rating=2.5, engagement_percent=30)

        rec = el.recommend_voice()
        assert rec["data_source"] == "learned"
        assert rec["recommendations"][0]["voice_name"] == "Adam"
        assert rec["recommendations"][0]["avg_rating"] == 4.5
        assert rec["total_generations_analyzed"] == 6

    def test_recommend_filters_by_content_type(self, el):
        # Adam is good for podcasts
        gid = el.learn_from_generation(voice_id="v1", voice_name="Adam", content_type="podcast")
        el.record_feedback(gid, rating=5.0)

        # Rachel is good for meditation
        gid = el.learn_from_generation(voice_id="v2", voice_name="Rachel", content_type="meditation")
        el.record_feedback(gid, rating=5.0)

        # Ask for podcast recommendation
        rec = el.recommend_voice(content_type="podcast")
        assert rec["content_type_filter"] == "podcast"
        assert rec["recommendations"][0]["voice_name"] == "Adam"

        # Ask for meditation recommendation
        rec = el.recommend_voice(content_type="meditation")
        assert rec["recommendations"][0]["voice_name"] == "Rachel"


class TestRecommendSettings:
    """Test settings recommendation engine."""

    def test_recommend_settings_defaults(self, el):
        rec = el.recommend_settings()
        assert rec["data_source"] == "default"
        assert rec["stability"] == 0.75
        assert rec["speed"] == 1.0

    def test_recommend_settings_from_high_rated(self, el):
        # Two high-rated generations with specific settings
        gid = el.learn_from_generation(voice_id="v1", stability=0.8, speed=0.85, similarity_boost=0.9)
        el.record_feedback(gid, rating=4.8)

        gid = el.learn_from_generation(voice_id="v1", stability=0.7, speed=0.9, similarity_boost=0.8)
        el.record_feedback(gid, rating=4.2)

        rec = el.recommend_settings()
        assert rec["data_source"] == "learned"
        assert rec["stability"] == 0.75  # avg of 0.8 and 0.7
        assert rec["speed"] == 0.88  # avg of 0.85 and 0.9 → 0.875 → rounds to 0.88
        assert rec["high_rated_generations"] == 2
        assert rec["confidence"] == 0.2  # 2/10

    def test_recommend_settings_detects_bad_speed(self, el):
        # Good generation: slow
        gid = el.learn_from_generation(voice_id="v1", stability=0.8, speed=0.8)
        el.record_feedback(gid, rating=4.5)

        # Bad generation: fast
        gid = el.learn_from_generation(voice_id="v1", stability=0.8, speed=1.3)
        el.record_feedback(gid, rating=2.0)

        rec = el.recommend_settings()
        assert "avoid" in rec["reason"].lower() or "faster" in rec["reason"].lower()


class TestImproveProfile:
    """Test the intelligence profile generation."""

    def test_profile_empty(self, el):
        profile = el.improve_profile()
        assert profile["total_generations"] == 0
        assert profile["profile_ready"] is False

    def test_profile_with_data(self, el):
        # Create some history
        for i in range(5):
            gid = el.learn_from_generation(
                voice_id="v1", voice_name="Adam",
                content_type="podcast", audio_length_sec=180.0,
            )
            el.record_feedback(gid, rating=4.0 + i * 0.2)

        profile = el.improve_profile()
        assert profile["total_generations"] == 5
        assert profile["total_rated"] == 5
        assert profile["total_audio_minutes"] == 15.0  # 5 * 180 / 60
        assert profile["most_used_voice"] == "Adam"
        assert profile["best_rated_voice"] == "Adam"
        assert profile["profile_ready"] is True
        assert profile["avg_rating"] > 0

    def test_profile_detects_improving_trend(self, el):
        # First batch: low ratings
        for _ in range(3):
            gid = el.learn_from_generation(voice_id="v1", voice_name="Adam")
            el.record_feedback(gid, rating=2.0)

        # Second batch: high ratings
        for _ in range(3):
            gid = el.learn_from_generation(voice_id="v1", voice_name="Adam")
            el.record_feedback(gid, rating=5.0)

        profile = el.improve_profile()
        assert profile["rating_trend"] == "improving"

    def test_profile_suggests_feedback(self, el):
        # 5 generations but no feedback
        for _ in range(5):
            el.learn_from_generation(voice_id="v1", voice_name="Adam")

        profile = el.improve_profile()
        assert any("feedback" in i.lower() for i in profile["insights"])

    def test_profile_content_distribution(self, el):
        el.learn_from_generation(voice_id="v1", content_type="podcast")
        el.learn_from_generation(voice_id="v1", content_type="podcast")
        el.learn_from_generation(voice_id="v1", content_type="meditation")

        profile = el.improve_profile()
        assert profile["content_types"]["podcast"] == 2
        assert profile["content_types"]["meditation"] == 1

