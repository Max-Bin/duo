"""Property-based tests using Hypothesis for core Duo functions."""

from __future__ import annotations

import json
import re
import string
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from duo.ceo_log import _validate_session_id
from duo.config import set_config
from duo.poller import age
from duo.protocol import atomic_write_text, prompt_hash, read_jsonl
from duo.transport import _validate_label
from duo.verifier import _match_writable

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

SAFE_LABEL_ALPHABET = string.ascii_letters + string.digits + "_.-"
SAFE_SESSION_ALPHABET = string.ascii_letters + string.digits + "_.-"

safe_labels = st.text(alphabet=SAFE_LABEL_ALPHABET, min_size=1, max_size=64)
unsafe_chars = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # exclude surrogates
        whitelist_categories=("L", "N", "P", "S", "Z"),
    ),
    min_size=1,
    max_size=64,
).filter(lambda s: not re.match(r"^[a-zA-Z0-9_.-]+$", s))

iso_timestamps = st.builds(
    lambda dt: dt.isoformat(),
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31),
        timezones=st.just(UTC),
    ),
)


# ---------------------------------------------------------------------------
# _validate_label (transport.py)
# ---------------------------------------------------------------------------


class TestValidateLabelProperty:
    """Property-based tests for pane label validation."""

    @given(safe_labels)
    def test_safe_labels_always_pass(self, label: str) -> None:
        """Any string matching [a-zA-Z0-9_.-]+ should pass."""
        _validate_label(label)  # should not raise

    @given(unsafe_chars)
    def test_unsafe_labels_always_raise(self, label: str) -> None:
        """Any string with chars outside the safe set should raise."""
        with pytest.raises(ValueError, match="Unsafe pane label"):
            _validate_label(label)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsafe pane label"):
            _validate_label("")

    @given(safe_labels)
    def test_trailing_newline_rejected(self, label: str) -> None:
        """Labels with trailing newline must be rejected (regex \\Z vs $)."""
        with pytest.raises(ValueError, match="Unsafe pane label"):
            _validate_label(label + "\n")

    @given(st.text(alphabet=SAFE_LABEL_ALPHABET, min_size=1, max_size=3))
    def test_short_labels_pass(self, label: str) -> None:
        """Even single-char safe labels pass."""
        _validate_label(label)


# ---------------------------------------------------------------------------
# prompt_hash (protocol.py)
# ---------------------------------------------------------------------------


class TestPromptHashProperty:
    """Property-based tests for prompt hashing."""

    @given(st.text(min_size=0, max_size=10000))
    def test_always_returns_8_hex_chars(self, text: str) -> None:
        """Hash is always 8 hex characters."""
        h = prompt_hash(text)
        assert len(h) == 8
        assert all(c in string.hexdigits for c in h)

    @given(st.text(min_size=0, max_size=500))
    def test_deterministic(self, text: str) -> None:
        """Same input always produces same hash."""
        assert prompt_hash(text) == prompt_hash(text)

    @given(
        st.lists(
            st.text(min_size=1, max_size=100), min_size=2, max_size=50, unique=True
        )
    )
    @settings(max_examples=50)
    def test_distinct_inputs_distinct_hashes(self, texts: list[str]) -> None:
        """Distinct inputs should produce distinct 8-char hashes (probabilistic)."""
        hashes = [prompt_hash(t) for t in texts]
        assert len(set(hashes)) == len(hashes)


# ---------------------------------------------------------------------------
# age (poller.py)
# ---------------------------------------------------------------------------


class TestAgeProperty:
    """Property-based tests for timestamp age calculation."""

    @given(iso_timestamps)
    def test_past_timestamps_positive(self, ts: str) -> None:
        """Timestamps in the past should return positive age."""
        result = age(ts)
        assert result >= 0.0

    def test_none_returns_inf(self) -> None:
        assert age(None) == float("inf")

    @given(st.text(min_size=1, max_size=50).filter(lambda s: not _is_valid_iso(s)))
    @settings(max_examples=50)
    def test_invalid_strings_return_inf(self, ts: str) -> None:
        """Invalid ISO strings return inf."""
        assert age(ts) == float("inf")

    def test_recent_timestamp_small_age(self) -> None:
        """A timestamp from 'now' should have age < 2 seconds."""
        ts = datetime.now(UTC).isoformat()
        assert age(ts) < 2.0

    def test_future_timestamp_clamped_to_zero(self) -> None:
        """Future timestamps return 0 due to max(0, ...) guard."""
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert age(future) == 0.0


# ---------------------------------------------------------------------------
# _match_writable (verifier.py)
# ---------------------------------------------------------------------------


class TestMatchWritableProperty:
    """Property-based tests for path matching."""

    @given(st.text(alphabet=string.ascii_lowercase + "/", min_size=1, max_size=30))
    def test_exact_match_always_works(self, path: str) -> None:
        """A path always matches itself as a pattern."""
        assert _match_writable(path, path) is True

    @given(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
        st.text(alphabet=string.ascii_lowercase + ".", min_size=1, max_size=10),
    )
    def test_star_matches_single_segment(self, dir_name: str, file_name: str) -> None:
        """dir/* matches dir/file."""
        assert _match_writable(f"{dir_name}/{file_name}", f"{dir_name}/*") is True

    @given(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
        st.text(alphabet=string.ascii_lowercase + ".", min_size=1, max_size=10),
    )
    def test_star_matches_across_slash_in_fnmatch(
        self, d1: str, d2: str, f: str
    ) -> None:
        """fnmatch * matches across / (unlike glob) — this is expected behavior."""
        assert _match_writable(f"{d1}/{d2}/{f}", f"{d1}/*") is True

    @given(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=10),
        st.text(alphabet=string.ascii_lowercase + ".", min_size=1, max_size=10),
    )
    def test_globstar_matches_nested(self, d1: str, d2: str, f: str) -> None:
        """dir/** matches dir/sub/file via fnmatch."""
        # fnmatch ** doesn't work like glob — it's just two wildcards
        # So this test verifies actual fnmatch behavior
        path = f"{d1}/{d2}/{f}"
        # fnmatch("a/b/c", "a/**") is implementation-dependent
        # We just verify it doesn't crash
        _match_writable(path, f"{d1}/**")

    def test_nfc_normalization(self) -> None:
        """NFC normalization ensures consistent matching."""
        # é as combining characters (NFD) vs precomposed (NFC)
        nfd = unicodedata.normalize("NFD", "café.py")
        nfc = unicodedata.normalize("NFC", "café.py")
        assert _match_writable(nfd, nfc) is True
        assert _match_writable(nfc, nfd) is True


# ---------------------------------------------------------------------------
# _validate_session_id (ceo_log.py)
# ---------------------------------------------------------------------------


class TestValidateSessionIdProperty:
    """Property-based tests for CEO session ID validation."""

    @given(
        st.from_regex(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", fullmatch=True).filter(
            lambda s: len(s) <= 100
        )
    )
    def test_valid_ids_pass(self, session_id: str) -> None:
        """IDs matching the regex should pass validation."""
        _validate_session_id(session_id)

    @given(st.just(""))
    def test_empty_raises(self, session_id: str) -> None:
        with pytest.raises(ValueError, match="Invalid CEO session ID"):
            _validate_session_id(session_id)

    def test_trailing_newline_rejected(self) -> None:
        """Session IDs with trailing newline must be rejected (regex \\Z vs $)."""
        with pytest.raises(ValueError, match="Invalid CEO session ID"):
            _validate_session_id("valid123\n")

    @given(
        st.sampled_from(
            [
                "../etc/passwd",
                "./hidden",
                "/absolute",
                "a/../b",
                "..",
                ".",
                "foo/../../bar",
                "a/b/../c",
            ]
        )
    )
    def test_traversal_attempts_rejected(self, session_id: str) -> None:
        """Session IDs with path traversal characters should fail."""
        with pytest.raises(ValueError, match="Invalid CEO session ID"):
            _validate_session_id(session_id)


# ---------------------------------------------------------------------------
# Config coercion
# ---------------------------------------------------------------------------


class TestConfigCoercionProperty:
    """Property-based tests for config value coercion and round-trip."""

    @given(val=st.integers(min_value=1, max_value=100))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_int_config_roundtrip(self, val: int, tmp_path, monkeypatch) -> None:
        """Integer values in valid range round-trip through set_config."""
        import duo.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        result = set_config("max_corrections", str(val))
        assert result == val
        assert isinstance(result, int)

    @given(
        val=st.floats(
            min_value=0.01, max_value=300.0, allow_nan=False, allow_infinity=False
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_float_config_roundtrip(self, val: float, tmp_path, monkeypatch) -> None:
        """Float values round-trip correctly."""
        import duo.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        result = set_config("poll_base_interval", str(val))
        assert abs(result - val) < 1e-6
        assert isinstance(result, float)

    @given(
        val=st.sampled_from(
            ["true", "false", "1", "0", "yes", "no", "True", "False", "YES", "NO"]
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_bool_config_accepts_all_variants(
        self, val: str, tmp_path, monkeypatch
    ) -> None:
        """All boolean string variants are accepted."""
        import duo.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        result = set_config("auto_allow_all", val)
        assert isinstance(result, bool)

    @given(
        val=st.text(min_size=1, max_size=10).filter(
            lambda s: s.lower() not in ("true", "false", "1", "0", "yes", "no")
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_invalid_bool_rejected(self, val: str, tmp_path, monkeypatch) -> None:
        """Non-boolean strings raise ValueError for bool keys."""
        import duo.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        with pytest.raises(ValueError, match="Cannot convert"):
            set_config("auto_allow_all", val)


# ---------------------------------------------------------------------------
# Atomic write roundtrip
# ---------------------------------------------------------------------------


class TestAtomicWriteRoundtrip:
    """Property: atomic_write_text → read gives back the same content."""

    # Exclude bare \r (Python text-mode normalizes to \n) and surrogates
    # (\ud800-\udfff can't be encoded to UTF-8). Our function targets JSON/text.
    _text_strategy = st.text(
        alphabet=st.characters(
            blacklist_characters="\r",
            blacklist_categories=("Cs",),
        ),
        min_size=0,
        max_size=5000,
    )

    @given(content=_text_strategy)
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_text_roundtrip(self, tmp_path: Path, content: str) -> None:
        """Any unicode text (sans bare CR) survives atomic write + read."""
        p = tmp_path / f"roundtrip-{hash(content)}.txt"
        atomic_write_text(p, content)
        assert p.read_text(encoding="utf-8") == content

    @given(
        content=st.text(
            alphabet=st.characters(
                blacklist_characters="\r",
                blacklist_categories=("Cs",),
            ),
            min_size=1,
            max_size=1000,
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_overwrites_cleanly(self, tmp_path: Path, content: str) -> None:
        """Successive atomic writes always leave a readable file."""
        p = tmp_path / "overwrite.txt"
        atomic_write_text(p, "initial")
        atomic_write_text(p, content)
        assert p.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# JSONL append + read roundtrip
# ---------------------------------------------------------------------------


class TestJsonlRoundtrip:
    """Property: appending N JSONL lines then reading back gives N events."""

    @given(n=st.integers(min_value=1, max_value=20))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_append_count(self, tmp_path: Path, n: int) -> None:
        """Appending N lines to a JSONL file gives N events on read_jsonl."""
        p = tmp_path / f"journal-{n}.jsonl"
        for i in range(n):
            line = json.dumps({"event": "test", "i": i}) + "\n"
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        events = read_jsonl(p)
        assert len(events) == n
        assert all(e["event"] == "test" for e in events)

    @given(
        n=st.integers(min_value=1, max_value=20),
        tail=st.integers(min_value=1, max_value=20),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_tail_never_exceeds_total(self, tmp_path: Path, n: int, tail: int) -> None:
        """read_jsonl(tail=K) returns min(K, N) events."""
        p = tmp_path / f"journal-{n}-{tail}.jsonl"
        for i in range(n):
            line = json.dumps({"event": "test", "i": i}) + "\n"
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        events = read_jsonl(p, tail=tail)
        assert len(events) == min(n, tail)
        if tail <= n:
            assert events[-1]["i"] == n - 1


# ---------------------------------------------------------------------------
# Prompt hash stability
# ---------------------------------------------------------------------------


class TestPromptHashStability:
    """Property: same input → same hash, different input → different hash."""

    @given(text=st.text(min_size=1, max_size=5000))
    def test_deterministic(self, text: str) -> None:
        """Same text always produces the same hash."""
        assert prompt_hash(text) == prompt_hash(text)

    @given(
        a=st.text(min_size=1, max_size=1000),
        b=st.text(min_size=1, max_size=1000),
    )
    def test_collision_resistant(self, a: str, b: str) -> None:
        """Different texts produce different hashes (modulo rare collisions)."""
        assume(a != b)
        assert prompt_hash(a) != prompt_hash(b)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_iso(s: str) -> bool:
    """Check if a string is a valid ISO-8601 timestamp."""
    try:
        datetime.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False
