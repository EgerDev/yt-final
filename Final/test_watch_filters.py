import random
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import watch_filters

from watch_filters import (
    WatchPatternError,
    WatchPatternTimeout,
    title_matches,
    validate_title_pattern,
)

UNSUPPORTED_PATTERNS = [
    (r"^(i)\1$", "backreference"),
    (r"^(?P<letter>i)(?P=letter)$", "backreference"),
    (r"^(a)?(?(1)b|c)$", "conditional"),
    ("(?x)^i # comment\n$", "verbose"),
    (r"^(?# comment)i$", "comment"),
    (r"(?V1)^[i]$", "stdlib"),
    (r"^(?:foo){i<=1}$", "extension"),
    (r"^[[a-z]--[x]]$", "character-set"),
    (r"^(?>i)$", "atomic"),
    (r"^i++$", "possessive"),
]


class _ParserProxy:
    def __init__(self, parser, *, parse=None, missing=()):
        self._parser = parser
        self._parse = parse or parser.parse
        self._missing = frozenset(missing)

    def parse(self, pattern, flags):
        return self._parse(pattern, flags)

    def __getattr__(self, name):
        if name in self._missing:
            raise AttributeError(name)
        return getattr(self._parser, name)


def test_catastrophic_pattern_times_out_within_wall_clock_bound():
    start = time.monotonic()
    with pytest.raises(WatchPatternTimeout, match="50 ms"):
        title_matches(r"^(a|aa)+$", "a" * 1000 + "!")
    assert time.monotonic() - start < 0.25


def test_pattern_length_and_syntax_are_validated():
    with pytest.raises(WatchPatternError, match="256"):
        validate_title_pattern("x" * 257)
    with pytest.raises(WatchPatternError, match="invalid"):
        validate_title_pattern("(")


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(
            r"^a{4294967296,4294967297}$",
            id="oversized-lower-bound",
        ),
        pytest.param(
            r"^a{1,4294967296}$",
            id="oversized-upper-bound",
        ),
        pytest.param(
            r"^a{4294967296,}$",
            id="one-sided-lower-bound",
        ),
        pytest.param(
            r"^a{,4294967296}$",
            id="one-sided-upper-bound",
        ),
        pytest.param(
            r"^a{4294967296}$",
            id="exact-bound",
        ),
    ],
)
def test_oversized_repeat_bounds_have_actionable_typed_error(pattern):
    with pytest.raises(
            WatchPatternError, match=r"(?i)repetition.*(?:large|smaller)"):
        validate_title_pattern(pattern)


def test_none_and_empty_patterns_preserve_no_filter_semantics():
    assert validate_title_pattern(None) is None
    assert validate_title_pattern("") is None


def test_non_string_pattern_has_a_clean_typed_error():
    with pytest.raises(WatchPatternError, match="string"):
        validate_title_pattern(["not", "a", "pattern"])


def test_transform_failure_is_wrapped_as_watch_pattern_error(monkeypatch):
    def fail_transform(_pattern):
        raise RuntimeError("transform failed")

    monkeypatch.setattr(
        watch_filters,
        "_stdlib_ignorecase_pattern",
        fail_transform,
    )
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(WatchPatternError, match="safely prepared"):
        validate_title_pattern("^unique-transform-i$")


def test_transformed_compile_failure_is_wrapped_as_watch_pattern_error(
        monkeypatch):
    def fail_compile(*_args, **_kwargs):
        raise watch_filters.regex.error("compile failed")

    monkeypatch.setattr(watch_filters.regex, "compile", fail_compile)
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(WatchPatternError, match="safely prepared"):
        validate_title_pattern("^unique-compile-i$")


def test_module_import_survives_unavailable_private_parser():
    script = textwrap.dedent(
        """
        import builtins

        real_import = builtins.__import__

        def import_without_re_parser(name, globals=None, locals=None,
                                     fromlist=(), level=0):
            if name == "re" and fromlist and "_parser" in fromlist:
                raise ImportError("private parser unavailable")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = import_without_re_parser
        import watch_filters

        try:
            watch_filters.validate_title_pattern("^release$")
        except watch_filters.WatchPatternError as exc:
            assert "parser" in str(exc).lower()
            assert "unavailable" in str(exc).lower()
        else:
            raise AssertionError("validation did not fail closed")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_validation_fails_typed_when_private_parser_becomes_unavailable(
        monkeypatch):
    monkeypatch.setattr(watch_filters, "re_parser", None)
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(
            WatchPatternError, match=r"(?i)parser.*unavailable"):
        validate_title_pattern("^parser-unavailable-i$")


def test_validation_fails_typed_when_known_parser_internal_is_missing(
        monkeypatch):
    proxy = _ParserProxy(
        watch_filters.re_parser,
        missing={"GROUPREF"},
    )
    monkeypatch.setattr(watch_filters, "re_parser", proxy)
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(
            WatchPatternError, match=r"(?i)parser.*GROUPREF"):
        validate_title_pattern("^missing-known-opcode-i$")


def test_validation_rejects_synthetic_unknown_parser_opcode(monkeypatch):
    real_parser = watch_filters.re_parser

    def parse_with_unknown(pattern, flags):
        parsed = real_parser.parse(pattern, flags)
        parsed.append((object(), None))
        return parsed

    monkeypatch.setattr(
        watch_filters,
        "re_parser",
        _ParserProxy(real_parser, parse=parse_with_unknown),
    )
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(
            WatchPatternError, match=r"(?i)unknown.*opcode"):
        validate_title_pattern("^unknown-opcode-i$")


def test_validation_rejects_malformed_known_parser_structure(monkeypatch):
    real_parser = watch_filters.re_parser

    def parse_with_malformed_subpattern(pattern, flags):
        parsed = real_parser.parse(pattern, flags)
        parsed.append((real_parser.SUBPATTERN, (None, 0)))
        return parsed

    monkeypatch.setattr(
        watch_filters,
        "re_parser",
        _ParserProxy(real_parser, parse=parse_with_malformed_subpattern),
    )
    watch_filters._validated_compatible_pattern.cache_clear()

    with pytest.raises(
            WatchPatternError, match=r"(?i)malformed.*SUBPATTERN"):
        validate_title_pattern("^malformed-subpattern-i$")


@pytest.mark.parametrize(("pattern", "message"), UNSUPPORTED_PATTERNS)
def test_unsupported_pattern_subset_is_rejected_with_actionable_error(
        pattern, message):
    with pytest.raises(WatchPatternError, match=message):
        validate_title_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "^a{١}$",
        "^a{١,٢}$",
        "^a{１}$",
        "^a{１,２}$",
    ],
)
def test_non_ascii_quantifier_digits_are_rejected_as_unescaped_braces(
        pattern):
    with pytest.raises(WatchPatternError, match="brace"):
        validate_title_pattern(pattern)


@pytest.mark.parametrize(
    ("pattern", "title"),
    [
        (r"^\(\?#\)$", "(?#)"),
        (r"^[()?#]+$", "(?#)"),
        (r"^(?P<backreference>i)$", "ı"),
        (r"^(?P<conditional>i)$", "İ"),
        (r"^\\1$", r"\1"),
        (r"^a\{e<=1\}$", "a{e<=1}"),
    ],
)
def test_subset_detection_does_not_misclassify_escaped_text_classes_or_names(
        pattern, title):
    assert validate_title_pattern(pattern) == pattern
    assert title_matches(pattern, title)


def test_normal_matching_is_unicode_aware_and_case_insensitive():
    assert title_matches(r"^café\s+\w+$", "CAFÉ résumé")
    assert not title_matches(r"^café\s+\w+$", "tea résumé")


@pytest.mark.parametrize("pattern", ["i", "I", "İ", "ı"])
@pytest.mark.parametrize("title", ["i", "I", "İ", "ı"])
def test_turkish_i_preserves_all_stdlib_ignorecase_pairs(pattern, title):
    assert title_matches(f"^{pattern}$", title)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^[i]$",
        r"^[İ]$",
        r"^[a-z]$",
        r"^[\x49]$",
        r"^[\u0130]$",
        r"^\x69$",
        r"^\u0131$",
    ],
)
@pytest.mark.parametrize("title", ["i", "I", "İ", "ı"])
def test_turkish_i_compatibility_covers_classes_ranges_and_escapes(
        pattern, title):
    assert title_matches(pattern, title)


@pytest.mark.parametrize("pattern", [r"^[^i]$", r"^[^\u0131]$", r"^[^a-z]$"])
@pytest.mark.parametrize("title", ["i", "I", "İ", "ı"])
def test_turkish_i_compatibility_covers_negative_classes(pattern, title):
    assert not title_matches(pattern, title)


def test_quantified_negative_classes_match_stdlib_for_all_ordinary_quantifiers():
    quantifiers = [
        "*",
        "+",
        "?",
        "*?",
        "+?",
        "??",
        "{2}",
        "{1,3}",
        "{2,}",
        "{,2}",
        "{2}?",
        "{1,3}?",
        "{2,}?",
        "{,2}?",
    ]
    bodies = [
        "",
        "a",
        "aa",
        "aaa",
        "i",
        "I",
        "İ",
        "ı",
        "ai",
        "aI",
        "aİ",
        "aı",
        "baıb",
    ]
    for excluded in "iIİı":
        for prefix in ("", "x", "mix-"):
            for quantifier in quantifiers:
                pattern = (
                    f"^{re.escape(prefix)}[^{excluded}]{quantifier}$"
                )
                for body in bodies:
                    title = prefix + body
                    expected = re.search(pattern, title, re.I) is not None
                    assert title_matches(pattern, title) is expected, (
                        pattern,
                        title,
                    )


def test_turkish_i_shim_does_not_rewrite_group_names_or_inline_flags():
    assert title_matches(r"^(?P<i>i)$", "ı")
    assert title_matches(r"^(?P<İ>İ)$", "ı")
    assert title_matches(r"^(?i:i)$", "ı")
    assert title_matches(r"^(?-i:i)$", "i")
    assert not title_matches(r"^(?-i:i)$", "I")
    assert title_matches(r"^(?a:i)$", "I")
    assert not title_matches(r"^(?a:i)$", "İ")
    assert not title_matches(r"^(?a:i)$", "ı")
    assert title_matches(r"^(?a:[i])$", "I")
    assert not title_matches(r"^(?a:[i])$", "İ")
    assert not title_matches(r"^(?a:[i])$", "ı")
    assert not title_matches(r"^(?a:[^i])$", "I")
    assert title_matches(r"^(?a:[^i])$", "İ")
    assert title_matches(r"^(?a:[^i])$", "ı")
    assert title_matches(r"^(?u:i)$", "ı")


def test_accepted_subset_matches_stdlib_ignorecase_reference_corpus():
    patterns = [
        r"^(?P<inside>i|café)+$",
        r"^(?=i)i$",
        r"^(?!x)i$",
        r"(?<=a)i",
        r"(?<!a)i",
        r"^(?-i:i)$",
        r"^(?i-m:i)$",
        r"^(?a:i)$",
        r"^(?u:i)$",
        r"^[\x49-\x69]+$",
        r"^\(\?#\)$",
        r"^[^i]$",
    ]
    titles = [
        "i",
        "I",
        "İ",
        "ı",
        "ai",
        "xi",
        "CAFÉ",
        "(?#)",
        "",
    ]
    for pattern in patterns:
        for title in titles:
            expected = re.search(pattern, title, re.I) is not None
            assert title_matches(pattern, title) is expected, (pattern, title)


def test_accepted_subset_matches_stdlib_ignorecase_seeded_fuzz():
    rng = random.Random(20260728)
    atoms = [
        "i",
        "I",
        "İ",
        "ı",
        "a",
        ".",
        r"\x69",
        r"\u0131",
        "[i]",
        "[^i]",
        "[a-z]",
        r"\w",
    ]
    quantifiers = ["", "?", "+", "{1,2}"]
    alphabet = "iIİıab1_"
    for _ in range(150):
        pattern = "^" + "".join(
            f"(?:{rng.choice(atoms)}){rng.choice(quantifiers)}"
            for _ in range(rng.randint(1, 3))
        ) + "$"
        title = "".join(
            rng.choice(alphabet) for _ in range(rng.randint(0, 5))
        )
        expected = re.search(pattern, title, re.I) is not None
        assert title_matches(pattern, title) is expected, (pattern, title)


def test_turkish_i_compatibility_keeps_other_unicode_ignorecase_semantics():
    assert title_matches(r"^café$", "CAFÉ")
    assert title_matches(r"^ſ$", "S")
    assert title_matches(r"^K$", "k")


def test_turkish_i_compatibility_still_honors_one_timeout_wall():
    start = time.monotonic()
    with pytest.raises(WatchPatternTimeout, match="50 ms"):
        title_matches(r"^ı(a|aa)+$", "İ" + "a" * 1000 + "!")
    assert time.monotonic() - start < 0.25
