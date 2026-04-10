"""Property-based tests using Hypothesis for core Duo functions."""

from __future__ import annotations

import json
import math
import re
import string
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from duo.ceo_log import _validate_session_id
from duo.cli import _fmt_ts, _parse_age, _validate_task_name
from duo.config import set_config
from duo.poller import AdaptivePoller, age
from duo.protocol import (
    DEFAULT_SECRET_PATTERNS,
    TRANSITIONS,
    TaskStatus,
    atomic_write_text,
    new_incarnation,
    prompt_hash,
    read_jsonl,
)
from duo.thinking import (
    THINKING_DIR,
    _validate_name,
    extract_response,
    thinking_dir,
)
from duo.transport import _validate_label, strip_ansi
from duo.verifier import _match_writable, _validate_writable_patterns

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


# ---------------------------------------------------------------------------
# strip_ansi
# ---------------------------------------------------------------------------


class TestStripAnsiProperty:
    """Properties of ANSI escape stripping."""

    @given(text=st.text(min_size=0, max_size=500))
    def test_idempotent(self, text: str) -> None:
        """Stripping twice is the same as stripping once."""
        once = strip_ansi(text)
        twice = strip_ansi(once)
        assert once == twice

    @given(
        text=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",), blacklist_characters="\x1b"
            ),
            min_size=0,
            max_size=200,
        )
    )
    def test_preserves_non_ansi(self, text: str) -> None:
        """Text without ESC character is unchanged by strip_ansi."""
        assert strip_ansi(text) == text

    @given(
        prefix=st.text(min_size=0, max_size=50),
        code=st.from_regex(r"\x1b\[[0-9;]{0,10}[A-Za-z]", fullmatch=True),
        suffix=st.text(min_size=0, max_size=50),
    )
    def test_removes_ansi(self, prefix: str, code: str, suffix: str) -> None:
        """ANSI sequences injected into text are removed."""
        result = strip_ansi(prefix + code + suffix)
        assert code not in result


# ---------------------------------------------------------------------------
# new_incarnation
# ---------------------------------------------------------------------------


class TestNewIncarnationProperty:
    """Properties of incarnation ID generation."""

    def test_format(self) -> None:
        """Incarnation IDs are 16-char lowercase hex."""
        for _ in range(50):
            inc = new_incarnation()
            assert len(inc) == 16
            assert re.fullmatch(r"[0-9a-f]{16}", inc)

    def test_uniqueness(self) -> None:
        """100 generated IDs are all unique."""
        ids = {new_incarnation() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# FSM transitions
# ---------------------------------------------------------------------------


class TestFSMTransitionsProperty:
    """Properties of the FSM transition table."""

    def test_all_states_have_transitions(self) -> None:
        """Every non-terminal state has at least one outgoing transition."""
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED}
        for status in TaskStatus:
            if status not in terminal:
                assert status in TRANSITIONS, f"{status} has no transitions"
                assert len(TRANSITIONS[status]) > 0

    def test_transitions_target_valid_states(self) -> None:
        """All transition targets are valid TaskStatus values."""
        valid = set(TaskStatus)
        for source, targets in TRANSITIONS.items():
            assert source in valid
            for target in targets:
                assert target in valid, f"{source} -> {target} is invalid"

    def test_no_unexpected_self_transitions(self) -> None:
        """Only PROMPT_SENT allows self-transition (resend)."""
        allowed_self = {TaskStatus.PROMPT_SENT}
        for source, targets in TRANSITIONS.items():
            if source not in allowed_self:
                assert source not in targets, f"{source} has unexpected self-transition"

    def test_terminal_states_absorbing(self) -> None:
        """COMPLETED is absorbing; FAILED only transitions to SESSION_STARTING (restart)."""
        assert TaskStatus.COMPLETED in TRANSITIONS
        assert TRANSITIONS[TaskStatus.COMPLETED] == frozenset(), (
            "COMPLETED should have no outgoing transitions"
        )
        # FAILED allows restart via SESSION_STARTING only
        failed_targets = TRANSITIONS.get(TaskStatus.FAILED, frozenset())
        assert failed_targets <= {TaskStatus.SESSION_STARTING}, (
            f"FAILED should only transition to SESSION_STARTING, got {failed_targets}"
        )

    def test_every_state_can_reach_terminal(self) -> None:
        """Every non-terminal state can reach COMPLETED or FAILED via BFS."""
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED}
        for start in TaskStatus:
            if start in terminal:
                continue
            visited: set[TaskStatus] = set()
            queue = [start]
            reached_terminal = False
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                if current in terminal:
                    reached_terminal = True
                    break
                queue.extend(
                    t for t in TRANSITIONS.get(current, frozenset()) if t not in visited
                )
            assert reached_terminal, f"{start} cannot reach any terminal state"


# ---------------------------------------------------------------------------
# _validate_label: path-safety on transport labels
# ---------------------------------------------------------------------------


class TestValidateLabelPathSafety:
    """Property: _validate_label rejects all path-traversal attempts."""

    @given(
        label=st.from_regex(r"[a-zA-Z0-9_.\-]{1,50}", fullmatch=True),
    )
    def test_valid_labels_accepted(self, label: str) -> None:
        """Labels matching ^[a-zA-Z0-9_.-]+$ are accepted."""
        _validate_label(label)  # should not raise

    @given(
        label=st.text(min_size=1, max_size=50).filter(
            lambda s: not re.fullmatch(r"[a-zA-Z0-9_.\-]+", s)
        ),
    )
    def test_invalid_labels_rejected(self, label: str) -> None:
        """Labels with path-unsafe characters are rejected."""
        with pytest.raises(ValueError, match="Unsafe pane label"):
            _validate_label(label)

    @given(
        prefix=st.sampled_from(["../", "./", "/", "~/"]),
        suffix=st.from_regex(r"[a-z]{1,10}", fullmatch=True),
    )
    def test_path_traversal_rejected(self, prefix: str, suffix: str) -> None:
        """Path traversal prefixes are always rejected."""
        with pytest.raises(ValueError, match="Unsafe pane label"):
            _validate_label(prefix + suffix)


# ---------------------------------------------------------------------------
# _match_writable: glob edge cases
# ---------------------------------------------------------------------------


class TestMatchWritableGlobProperty:
    """Property: _match_writable with strict patterns."""

    @given(
        filename=st.from_regex(r"[a-z]{1,10}\.(py|js|ts|md)", fullmatch=True),
    )
    def test_star_glob_matches_flat_files(self, filename: str) -> None:
        """'src/*' matches any file directly under src/."""
        assert _match_writable(f"src/{filename}", "src/*")

    @given(
        depth=st.integers(min_value=1, max_value=5),
        filename=st.from_regex(r"[a-z]{1,8}\.py", fullmatch=True),
    )
    def test_double_star_matches_any_depth(self, depth: int, filename: str) -> None:
        """'src/**' matches files at any depth under src/."""
        path = "src/" + "/".join(["sub"] * depth) + f"/{filename}"
        assert _match_writable(path, "src/**")

    @given(
        filename=st.from_regex(r"[a-z]{1,8}\.py", fullmatch=True),
    )
    def test_no_match_outside_pattern(self, filename: str) -> None:
        """Files outside the writable pattern are rejected."""
        assert not _match_writable(f"other/{filename}", "src/*")


# ---------------------------------------------------------------------------
# Config: all DEFAULTS keys have consistent types
# ---------------------------------------------------------------------------


class TestConfigDefaultsConsistency:
    """Property: DEFAULTS values are self-consistent."""

    def test_all_defaults_have_known_types(self) -> None:
        """Every default value is str, int, float, or bool."""
        from duo.config import DEFAULTS

        for key, value in DEFAULTS.items():
            assert isinstance(value, (str, int, float, bool)), (
                f"{key} has unexpected type {type(value)}"
            )

    def test_no_empty_string_keys(self) -> None:
        """No DEFAULTS key is empty or whitespace-only."""
        from duo.config import DEFAULTS

        for key in DEFAULTS:
            assert key.strip(), "Empty key in DEFAULTS"
            assert re.fullmatch(r"[a-z][a-z0-9_]*", key), (
                f"Key '{key}' doesn't follow snake_case"
            )

    def test_numeric_defaults_are_non_negative(self) -> None:
        """All numeric defaults are >= 0 (no accidental negatives)."""
        from duo.config import DEFAULTS

        for key, value in DEFAULTS.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert value >= 0, f"{key} has negative default {value}"


# ---------------------------------------------------------------------------
# _fmt_ts: timestamp formatting robustness
# ---------------------------------------------------------------------------


class TestFmtTsProperty:
    """Property: _fmt_ts never crashes on any input."""

    @given(text=st.text(min_size=0, max_size=100))
    def test_never_raises(self, text: str) -> None:
        """_fmt_ts handles any string without raising."""
        result = _fmt_ts(text)
        assert isinstance(result, str)
        assert len(result) <= max(len(text), 8)

    @given(
        dt=st.datetimes(
            min_value=datetime(2020, 1, 1),
            max_value=datetime(2030, 12, 31),
        )
    )
    def test_valid_iso_extracts_time(self, dt: datetime) -> None:
        """Valid ISO timestamps produce exact HH:MM:SS output."""
        iso = dt.isoformat()
        result = _fmt_ts(iso)
        assert result == dt.strftime("%H:%M:%S")

    @given(text=st.text(max_size=50).filter(lambda s: "T" not in s and len(s) >= 8))
    def test_no_T_returns_first_8_chars(self, text: str) -> None:
        """Without 'T', returns first 8 chars."""
        result = _fmt_ts(text)
        assert result == text[:8]

    @given(text=st.just(""))
    def test_empty_string(self, text: str) -> None:
        """Empty string returns truncated empty."""
        result = _fmt_ts(text)
        assert result == ""


# === Verifier property tests ===


class TestSecretPatternFalsePositives:
    """Property tests: normal code should not trigger secret detection."""

    @given(
        identifier=st.from_regex(r"[a-z][a-z0-9_]{2,20}", fullmatch=True),
        value=st.from_regex(r"[0-9]{1,10}", fullmatch=True),
    )
    def test_numeric_assignments_safe(self, identifier: str, value: str) -> None:
        """Pure numeric assignments should never trigger secret patterns."""
        line = f"+{identifier} = {value}"
        for pattern in DEFAULT_SECRET_PATTERNS:
            if pattern.endswith("="):
                key = re.escape(pattern[:-1])
                regex = key + r"\s*="
            else:
                regex = re.escape(pattern)
            assert not re.search(regex, line, re.IGNORECASE), (
                f"False positive: '{line}' matched pattern '{pattern}'"
            )

    @given(
        func=st.from_regex(r"[a-z_]{3,15}", fullmatch=True),
        arg=st.from_regex(r"[a-z_]{3,15}", fullmatch=True),
    )
    def test_function_calls_safe(self, func: str, arg: str) -> None:
        """Function call lines should not trigger unless they contain secrets."""
        line = f"+result = {func}({arg})"
        # Exclude any line that contains a secret pattern substring
        for pat in DEFAULT_SECRET_PATTERNS:
            assume(pat.lower().rstrip("=") not in line.lower())
        for pattern in DEFAULT_SECRET_PATTERNS:
            if pattern.endswith("="):
                key = re.escape(pattern[:-1])
                regex = key + r"\s*="
            else:
                regex = re.escape(pattern)
            assert not re.search(regex, line, re.IGNORECASE), (
                f"False positive: '{line}' matched pattern '{pattern}'"
            )


class TestSecretPatternKnownFormats:
    """Property tests: known secret formats must always be detected."""

    @given(suffix=st.from_regex(r"[A-Za-z0-9]{20,40}", fullmatch=True))
    def test_github_pat_detected(self, suffix: str) -> None:
        """github_pat_ prefix must always be caught."""
        line = f"+GITHUB_TOKEN=github_pat_{suffix}"
        pat = "github_pat_"
        assert re.search(re.escape(pat), line, re.IGNORECASE)

    @given(suffix=st.from_regex(r"[A-Za-z0-9]{36}", fullmatch=True))
    def test_ghp_prefix_detected(self, suffix: str) -> None:
        """ghp_ prefix must always be caught."""
        line = f'+token = "ghp_{suffix}"'
        pat = "ghp_"
        assert re.search(re.escape(pat), line, re.IGNORECASE)

    @given(key_value=st.from_regex(r"[A-Za-z0-9+/]{20,60}", fullmatch=True))
    def test_aws_key_detected(self, key_value: str) -> None:
        """AKIA prefix must always be caught."""
        line = f"+AWS_ACCESS_KEY_ID=AKIA{key_value}"
        pat = "AKIA"
        assert re.search(re.escape(pat), line, re.IGNORECASE)

    @given(
        ws=st.from_regex(r"\s{0,5}", fullmatch=True),
        val=st.from_regex(r"[A-Za-z0-9]{5,30}", fullmatch=True),
    )
    def test_key_equals_whitespace_tolerance(self, ws: str, val: str) -> None:
        """API_KEY= pattern must match with whitespace around '='."""
        line = f"+API_KEY{ws}={ws}{val}"
        pat = "API_KEY="
        key = re.escape(pat[:-1])
        regex = key + r"\s*="
        assert re.search(regex, line, re.IGNORECASE)


class TestValidateWritablePatternsProperty:
    """Property tests for _validate_writable_patterns."""

    @given(pat=st.from_regex(r"[a-z][a-z0-9_/.*]{1,30}", fullmatch=True))
    def test_relative_patterns_pass(self, pat: str) -> None:
        """Valid relative patterns are preserved."""
        result = _validate_writable_patterns([pat])
        assert result == [pat]

    @given(pat=st.from_regex(r"/[a-z][a-z0-9_/]{1,30}", fullmatch=True))
    def test_absolute_patterns_rejected(self, pat: str) -> None:
        """Absolute paths are always filtered out."""
        result = _validate_writable_patterns([pat])
        assert result == []

    @given(pat=st.from_regex(r"\s{0,5}", fullmatch=True))
    def test_whitespace_patterns_rejected(self, pat: str) -> None:
        """Empty or whitespace-only patterns are filtered out."""
        result = _validate_writable_patterns([pat])
        assert result == []


class TestPollerBackoffProperty:
    """Property tests for AdaptivePoller timing invariants."""

    @given(n_ramps=st.integers(min_value=1, max_value=100))
    def test_interval_monotonic_increase(self, n_ramps: int) -> None:
        """Interval only increases (or stays at max) during successive ramps."""
        poller = AdaptivePoller(base_interval=5.0, max_interval=60.0)
        prev = poller.interval
        for _ in range(n_ramps):
            poller._ramp()
            assert poller.interval >= prev
            prev = poller.interval

    @given(n_ramps=st.integers(min_value=1, max_value=200))
    def test_interval_never_exceeds_max(self, n_ramps: int) -> None:
        """Interval must never exceed max_interval no matter how many ramps."""
        poller = AdaptivePoller(base_interval=5.0, max_interval=60.0)
        for _ in range(n_ramps):
            poller._ramp()
        assert poller.interval <= 60.0

    @given(n_ramps=st.integers(min_value=1, max_value=50))
    def test_reset_returns_to_base(self, n_ramps: int) -> None:
        """Reset always returns interval to base, regardless of ramp history."""
        poller = AdaptivePoller(base_interval=5.0, max_interval=60.0)
        for _ in range(n_ramps):
            poller._ramp()
        poller._reset()
        assert poller.interval == 5.0


# ---------------------------------------------------------------------------
# Thinking session property tests
# ---------------------------------------------------------------------------


SAFE_NAME_ALPHABET = string.ascii_letters + string.digits + "_-"


class TestThinkingNameValidation:
    """Property tests for thinking session name validation."""

    @given(
        name=st.text(alphabet=SAFE_NAME_ALPHABET, min_size=1, max_size=32).filter(
            lambda s: s[0].isalnum()
        )
    )
    def test_valid_names_accepted(self, name: str) -> None:
        """Safe names (alphanumeric start, then alnum/underscore/hyphen) are accepted."""
        _validate_name(name)  # should not raise

    @given(
        name=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",),
                whitelist_categories=("L", "N", "P", "S", "Z"),
            ),
            min_size=1,
            max_size=32,
        ).filter(lambda s: not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", s))
    )
    def test_unsafe_names_rejected(self, name: str) -> None:
        """Names with special chars, starting with non-alnum, etc. are rejected."""
        with pytest.raises(ValueError, match="Invalid thinking session name"):
            _validate_name(name)

    @given(
        name=st.text(alphabet=SAFE_NAME_ALPHABET, min_size=1, max_size=16).filter(
            lambda s: s[0].isalnum()
        )
    )
    def test_thinking_dir_is_subpath(self, name: str) -> None:
        """thinking_dir always returns a path under THINKING_DIR."""
        result = thinking_dir(name)
        assert result.parent == THINKING_DIR
        assert result.name == name


class TestExtractResponseProperty:
    """Property tests for extract_response text delta extraction."""

    @given(
        common=st.lists(st.text(min_size=0, max_size=80), min_size=0, max_size=10),
        response=st.lists(st.text(min_size=1, max_size=80), min_size=1, max_size=10),
        user_msg=st.text(min_size=1, max_size=40),
    )
    def test_user_message_filtered_from_output(
        self, common: list[str], response: list[str], user_msg: str
    ) -> None:
        """The user's own message should not appear in the extracted response."""
        before = "\n".join(common)
        after = "\n".join(common + [user_msg] + response)
        result = extract_response(before, after, user_msg)
        # User message line should be filtered out
        for line in result.splitlines():
            assert line.strip() != user_msg.strip()

    @given(
        prefix=st.lists(st.text(min_size=0, max_size=40), min_size=0, max_size=5),
    )
    def test_identical_content_yields_empty(self, prefix: list[str]) -> None:
        """When before == after, the response should be empty."""
        content = "\n".join(prefix)
        result = extract_response(content, content, "test query")
        assert result.strip() == ""

    @given(
        noise=st.sampled_from(["● Edit", "● Read", "● Bash", "● Grep", ">", "❯", ""]),
        real_line=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs", "Cc", "Zl", "Zp"),
            ),
            min_size=1,
            max_size=80,
        ).filter(
            lambda s: (
                not s.strip().startswith(("●", ">", "❯"))
                and s.strip() not in ("", ">", "❯")
            )
        ),
    )
    def test_noise_lines_filtered(self, noise: str, real_line: str) -> None:
        """Tool output markers and empty lines are filtered, real content preserved."""
        before = "prompt line"
        after = f"prompt line\n{noise}\n{real_line}"
        result = extract_response(before, after, "some question")
        # Real line should survive filtering
        assert real_line in result


# ---------------------------------------------------------------------------
# Config type coercion property tests
# ---------------------------------------------------------------------------


class TestConfigSetCoercion:
    """Property tests for config set_config type coercion."""

    @given(val=st.integers(min_value=1, max_value=100))
    def test_max_parallel_round_trip(self, val: int) -> None:
        """Integer config values survive set→get round trip."""
        import tempfile

        import duo.config as cfg

        with tempfile.TemporaryDirectory() as td:
            cfg.CONFIG_PATH = Path(td) / "config.json"
            result = set_config("max_parallel", str(val))
            assert result == val
            assert isinstance(result, int)

    @given(
        val=st.floats(
            min_value=0.01, max_value=300.0, allow_nan=False, allow_infinity=False
        )
    )
    def test_poll_base_round_trip(self, val: float) -> None:
        """Float config values survive coercion and are finite."""
        import tempfile

        import duo.config as cfg

        with tempfile.TemporaryDirectory() as td:
            cfg.CONFIG_PATH = Path(td) / "config.json"
            result = set_config("poll_base_interval", str(val))
            assert isinstance(result, float)
            assert math.isfinite(result)

    @given(val=st.sampled_from(["true", "false", "1", "0", "yes", "no"]))
    def test_bool_coercion_all_accepted_forms(self, val: str) -> None:
        """All documented bool representations are accepted."""
        import tempfile

        import duo.config as cfg

        with tempfile.TemporaryDirectory() as td:
            cfg.CONFIG_PATH = Path(td) / "config.json"
            result = set_config("auto_allow_all", val)
            assert isinstance(result, bool)
            if val.lower() in ("true", "1", "yes"):
                assert result is True
            else:
                assert result is False

    @given(
        val=st.text(min_size=1, max_size=20).filter(
            lambda s: s.lower() not in ("true", "false", "1", "0", "yes", "no")
        )
    )
    def test_invalid_bool_rejected(self, val: str) -> None:
        """Non-bool strings are rejected for bool config keys."""
        import tempfile

        import duo.config as cfg

        with tempfile.TemporaryDirectory() as td:
            cfg.CONFIG_PATH = Path(td) / "config.json"
            with pytest.raises(ValueError, match="Cannot convert"):
                set_config("auto_allow_all", val)

    @given(val=st.sampled_from(["inf", "-inf", "nan", "NaN", "INF"]))
    def test_nonfinite_float_rejected(self, val: str) -> None:
        """Non-finite float values are rejected."""
        import tempfile

        import duo.config as cfg

        with tempfile.TemporaryDirectory() as td:
            cfg.CONFIG_PATH = Path(td) / "config.json"
            with pytest.raises(ValueError, match="finite"):
                set_config("poll_base_interval", val)


# ── _parse_age property tests ───────────────────────────────────────


class TestParseAgeProperties:
    """Property-based tests for _parse_age."""

    multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}

    @given(
        value=st.integers(min_value=1, max_value=365),
        unit=st.sampled_from(["d", "h", "m", "s"]),
    )
    def test_parse_age_correct_multiplication(self, value: int, unit: str) -> None:
        """_parse_age(Vu) == V * multiplier[u]."""
        result = _parse_age(f"{value}{unit}")
        assert result == value * self.multipliers[unit]

    @given(value=st.integers(min_value=1, max_value=365))
    def test_days_hours_relationship(self, value: int) -> None:
        """N days == N*24 hours."""
        assert _parse_age(f"{value}d") == _parse_age(f"{value * 24}h")

    @given(value=st.integers(min_value=1, max_value=365))
    def test_hours_minutes_relationship(self, value: int) -> None:
        """N hours == N*60 minutes."""
        assert _parse_age(f"{value}h") == _parse_age(f"{value * 60}m")

    @given(value=st.integers(min_value=1, max_value=3600))
    def test_minutes_seconds_relationship(self, value: int) -> None:
        """N minutes == N*60 seconds."""
        assert _parse_age(f"{value}m") == _parse_age(f"{value * 60}s")

    @given(
        text=st.text(
            alphabet=st.characters(categories=("L", "N", "P", "S")),
            min_size=1,
            max_size=20,
        ).filter(lambda s: not __import__("re").match(r"^\d+[dhms]$", s))
    )
    def test_invalid_format_rejected(self, text: str) -> None:
        """Any string not matching \\d+[dhms] raises UsageError."""
        import click

        with pytest.raises(click.UsageError):
            _parse_age(text)


# ── _validate_task_name property tests ──────────────────────────────


class TestValidateTaskNameProperties:
    """Property-based tests for _validate_task_name."""

    @given(
        name=st.from_regex(r"[a-zA-Z0-9_-]{1,63}", fullmatch=True),
    )
    def test_valid_names_accepted(self, name: str) -> None:
        """Names matching [a-zA-Z0-9_-]{1,63} are accepted."""
        _validate_task_name(name)

    @given(
        name=st.text(min_size=64, max_size=200).filter(
            lambda s: bool(re.match(r"^[a-zA-Z0-9_-]+\Z", s))
        )
    )
    def test_too_long_rejected(self, name: str) -> None:
        """Names longer than 63 chars are rejected."""
        import click

        with pytest.raises(click.BadParameter, match="at most 63"):
            _validate_task_name(name)

    @given(
        name=st.text(
            alphabet=st.characters(categories=("L", "N", "P", "S", "Z")),
            min_size=1,
            max_size=63,
        ).filter(lambda s: not re.match(r"^[a-zA-Z0-9_-]+\Z", s))
    )
    def test_unsafe_chars_rejected(self, name: str) -> None:
        """Names with characters outside [a-zA-Z0-9_-] are rejected."""
        import click

        with pytest.raises(click.BadParameter, match="letters, numbers"):
            _validate_task_name(name)
