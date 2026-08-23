"""Validated, time-bounded title matching for channel watches.

Watch title patterns intentionally support a bounded subset of Python's stdlib
``re`` grammar: ordinary literals, escapes, classes/ranges, capturing (including
named) and noncapturing groups, alternation, ordinary greedy/lazy quantifiers,
anchors, categories, lookarounds, and the ``i/a/u/m/s`` flags. Backreferences,
conditionals, verbose mode/comments, atomic groups, possessive quantifiers, and
syntax accepted only by the third-party ``regex`` engine are rejected.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata
import warnings

import regex

try:
    # CPython's private parser has no public typing surface; _parser_contract()
    # validates every attribute and shape before any parsed input is trusted.
    from re import _parser as re_parser  # type: ignore[attr-defined]
except (ImportError, AttributeError):
    re_parser = None


MAX_PATTERN_LENGTH = 256
MATCH_TIMEOUT_SECONDS = 0.050
_TURKISH_I_CHARS = "iIİı"
_TURKISH_I_CLASS = f"[{_TURKISH_I_CHARS}]"
_QUANTIFIER = re.compile(
    r"\{(?:[0-9]+|[0-9]+,[0-9]*|,[0-9]+)\}"
)
_ALLOWED_INLINE_FLAGS = re.I | re.A | re.U | re.M | re.S
_SEQUENCE_OPCODE_NAMES = (
    "ANY",
    "ASSERT",
    "ASSERT_NOT",
    "AT",
    "BRANCH",
    "CATEGORY",
    "IN",
    "LITERAL",
    "MAX_REPEAT",
    "MIN_REPEAT",
    "NOT_LITERAL",
    "SUBPATTERN",
)
_SET_OPCODE_NAMES = ("CATEGORY", "LITERAL", "NEGATE", "RANGE")
_UNSUPPORTED_OPCODE_NAMES = (
    "ATOMIC_GROUP",
    "GROUPREF",
    "GROUPREF_EXISTS",
    "GROUPREF_IGNORE",
    "GROUPREF_LOC_IGNORE",
    "GROUPREF_UNI_IGNORE",
    "POSSESSIVE_REPEAT",
    "POSSESSIVE_REPEAT_ONE",
)
_ANCHOR_NAMES = (
    "AT_BEGINNING",
    "AT_BEGINNING_LINE",
    "AT_BEGINNING_STRING",
    "AT_BOUNDARY",
    "AT_END",
    "AT_END_LINE",
    "AT_END_STRING",
    "AT_LOC_BOUNDARY",
    "AT_LOC_NON_BOUNDARY",
    "AT_NON_BOUNDARY",
    "AT_UNI_BOUNDARY",
    "AT_UNI_NON_BOUNDARY",
)
_CATEGORY_NAMES = (
    "CATEGORY_DIGIT",
    "CATEGORY_LINEBREAK",
    "CATEGORY_LOC_NOT_WORD",
    "CATEGORY_LOC_WORD",
    "CATEGORY_NOT_DIGIT",
    "CATEGORY_NOT_LINEBREAK",
    "CATEGORY_NOT_SPACE",
    "CATEGORY_NOT_WORD",
    "CATEGORY_SPACE",
    "CATEGORY_UNI_DIGIT",
    "CATEGORY_UNI_LINEBREAK",
    "CATEGORY_UNI_NOT_DIGIT",
    "CATEGORY_UNI_NOT_LINEBREAK",
    "CATEGORY_UNI_NOT_SPACE",
    "CATEGORY_UNI_NOT_WORD",
    "CATEGORY_UNI_SPACE",
    "CATEGORY_UNI_WORD",
    "CATEGORY_WORD",
)


class WatchPatternError(ValueError):
    """A watch title pattern is unsafe or cannot be compiled."""


class WatchPatternTimeout(WatchPatternError):
    """A watch title pattern exceeded its per-match time budget."""


def validate_title_pattern(value: object) -> str | None:
    """Validate the documented watch subset before persistence.

    ``None`` and the empty string retain their historical "no filter" meaning.
    The original pattern is returned so persisted watches remain portable and
    inspectable; the compatibility transform is separately cached.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise WatchPatternError("title pattern must be a string")
    if len(value) > MAX_PATTERN_LENGTH:
        raise WatchPatternError(
            f"title pattern must be at most {MAX_PATTERN_LENGTH} characters"
        )
    _validated_compatible_pattern(value)
    return value


def _escape_end(pattern: str, start: int) -> int:
    """Return the first index after one regex escape beginning at *start*."""
    if start + 1 >= len(pattern):
        return len(pattern)
    kind = pattern[start + 1]
    widths = {"x": 2, "u": 4, "U": 8}
    if kind in widths:
        return min(len(pattern), start + 2 + widths[kind])
    if kind in "NpP" and start + 2 < len(pattern) and pattern[start + 2] == "{":
        end = pattern.find("}", start + 3)
        return len(pattern) if end < 0 else end + 1
    if kind in "gk" and start + 2 < len(pattern) and pattern[start + 2] in "<{":
        close = ">" if pattern[start + 2] == "<" else "}"
        end = pattern.find(close, start + 3)
        return len(pattern) if end < 0 else end + 1
    if kind in "01234567":
        end = start + 2
        while end < min(len(pattern), start + 4) and pattern[end] in "01234567":
            end += 1
        return end
    return min(len(pattern), start + 2)


def _escaped_codepoint(token: str) -> str | None:
    """Decode only escapes that denote one literal codepoint, never regex syntax."""
    try:
        if token.startswith(r"\x") and len(token) == 4:
            return chr(int(token[2:], 16))
        if token.startswith(r"\u") and len(token) == 6:
            return chr(int(token[2:], 16))
        if token.startswith(r"\U") and len(token) == 10:
            return chr(int(token[2:], 16))
        if token.startswith(r"\N{") and token.endswith("}"):
            return unicodedata.lookup(token[3:-1])
        if (
            len(token) == 4
            and token.startswith("\\")
            and all(char in "01234567" for char in token[1:])
        ):
            return chr(int(token[1:], 8))
    except (KeyError, ValueError):
        pass
    return None


def _class_end(pattern: str, start: int) -> int:
    """Locate a validated character class's closing bracket."""
    index = start + 1
    if index < len(pattern) and pattern[index] == "^":
        index += 1
    # A closing bracket in the first class position is a literal.
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern):
        if pattern[index] == "\\":
            index = _escape_end(pattern, index)
            continue
        if pattern[index] == "]":
            return index + 1
        index += 1
    return len(pattern)


def _contains_comment_group(pattern: str) -> bool:
    """Find a real ``(?#...)`` token without inspecting escapes or classes."""
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\":
            index = _escape_end(pattern, index)
            continue
        if pattern[index] == "[":
            index = _class_end(pattern, index)
            continue
        if pattern.startswith("(?#", index):
            return True
        index += 1
    return False


def _contains_non_quantifier_brace(pattern: str) -> bool:
    """Reject braces that the timeout engine could reinterpret as extensions."""
    index = 0
    while index < len(pattern):
        if pattern[index] == "\\":
            index = _escape_end(pattern, index)
            continue
        if pattern[index] == "[":
            index = _class_end(pattern, index)
            continue
        if pattern[index] == "{":
            quantifier = _QUANTIFIER.match(pattern, index)
            if quantifier is None:
                return True
            index = quantifier.end()
            continue
        if pattern[index] == "}":
            return True
        index += 1
    return False


def _parser_contract():
    """Resolve the private stdlib parser contract without import-time dereferences."""
    if re_parser is None:
        raise WatchPatternError(
            "title pattern parser unavailable in this Python runtime; "
            "use a supported CPython 3.11+ build"
        )

    names = (
        ("parse", "SubPattern", "MAXREPEAT")
        + _SEQUENCE_OPCODE_NAMES
        + _SET_OPCODE_NAMES
        + _UNSUPPORTED_OPCODE_NAMES
        + _ANCHOR_NAMES
        + _CATEGORY_NAMES
    )
    resolved = {}
    missing = []
    for name in dict.fromkeys(names):
        try:
            resolved[name] = getattr(re_parser, name)
        except AttributeError:
            missing.append(name)
        except Exception as exc:
            raise WatchPatternError(
                "title pattern parser unavailable: could not inspect "
                f"runtime internal {name} ({exc})"
            ) from exc
    if missing:
        raise WatchPatternError(
            "title pattern parser contract is incompatible with this Python "
            f"runtime; missing internals: {', '.join(missing)}"
        )
    if not callable(resolved["parse"]) or not isinstance(
            resolved["SubPattern"], type):
        raise WatchPatternError(
            "title pattern parser contract is incompatible with this Python "
            "runtime; parse/SubPattern internals are malformed"
        )
    if not _is_parser_int(resolved["MAXREPEAT"]):
        raise WatchPatternError(
            "title pattern parser contract is incompatible with this Python "
            "runtime; MAXREPEAT is malformed"
        )

    opcode_names = tuple(dict.fromkeys(
        _SEQUENCE_OPCODE_NAMES
        + _SET_OPCODE_NAMES
        + _UNSUPPORTED_OPCODE_NAMES
    ))
    opcode_by_value: dict[object, str] = {}
    for name in opcode_names:
        value = resolved[name]
        try:
            duplicate = opcode_by_value.get(value)
            opcode_by_value[value] = name
        except (TypeError, ValueError) as exc:
            raise WatchPatternError(
                "title pattern parser contract is incompatible with this "
                f"Python runtime; opcode {name} is malformed"
            ) from exc
        if duplicate is not None and duplicate != name:
            raise WatchPatternError(
                "title pattern parser contract is incompatible with this "
                f"Python runtime; opcodes {duplicate} and {name} overlap"
            )

    try:
        anchors = frozenset(resolved[name] for name in _ANCHOR_NAMES)
        categories = frozenset(resolved[name] for name in _CATEGORY_NAMES)
    except (TypeError, ValueError) as exc:
        raise WatchPatternError(
            "title pattern parser contract is incompatible with this Python "
            "runtime; anchor/category constants are malformed"
        ) from exc
    if len(anchors) != len(_ANCHOR_NAMES) or len(categories) != len(
            _CATEGORY_NAMES):
        raise WatchPatternError(
            "title pattern parser contract is incompatible with this Python "
            "runtime; anchor/category constants overlap"
        )

    return {
        "parse": resolved["parse"],
        "subpattern": resolved["SubPattern"],
        "maxrepeat": resolved["MAXREPEAT"],
        "opcode_by_value": opcode_by_value,
        "anchors": anchors,
        "categories": categories,
    }


def _is_parser_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parser_malformed(name: str, detail: str) -> WatchPatternError:
    return WatchPatternError(
        f"title pattern parser returned malformed {name} structure: {detail}"
    )


def _validate_subset(parsed, pattern: str, contract) -> None:
    """Allow only AST opcodes and argument shapes the transformer preserves."""
    subpattern_type = contract["subpattern"]
    try:
        global_flags = parsed.state.flags
    except Exception as exc:
        raise _parser_malformed("SubPattern", "missing integer state flags") from exc
    if not _is_parser_int(global_flags):
        raise _parser_malformed("SubPattern", "state flags are not an integer")
    if global_flags & re.X:
        raise WatchPatternError(
            "unsupported title pattern: verbose x mode is not supported; "
            "remove (?x) and write the pattern without # comments"
        )
    if global_flags & ~_ALLOWED_INLINE_FLAGS:
        raise WatchPatternError(
            "unsupported title pattern: only i/a/u/m/s flags are supported"
        )
    if _contains_comment_group(pattern):
        raise WatchPatternError(
            "unsupported title pattern: regex comment groups are not supported; "
            "remove the (?#...) comment"
    )

    opcode_by_value = contract["opcode_by_value"]

    def opcode_name(opcode) -> str:
        try:
            name = opcode_by_value.get(opcode)
        except (TypeError, ValueError) as exc:
            raise WatchPatternError(
                "unsupported title pattern: runtime parser returned an "
                "unknown unhashable opcode"
            ) from exc
        if name is None:
            raise WatchPatternError(
                "unsupported title pattern: runtime parser returned an "
                f"unknown opcode {opcode!r}; use a supported CPython runtime"
            )
        return name

    def codepoint(value, owner: str) -> None:
        if not _is_parser_int(value) or not 0 <= value <= 0x10FFFF:
            raise _parser_malformed(owner, "expected one Unicode codepoint")

    def category(value, owner: str) -> None:
        try:
            known = value in contract["categories"]
        except (TypeError, ValueError) as exc:
            raise _parser_malformed(owner, "unhashable category") from exc
        if not known:
            raise _parser_malformed(owner, "unknown category")

    def visit_set(items) -> None:
        if type(items) is not list:
            raise _parser_malformed("IN", "expected a list of set operations")
        for index, item in enumerate(items):
            if type(item) is not tuple or len(item) != 2:
                raise _parser_malformed("IN", "set operation is not a pair")
            opcode, argument = item
            name = opcode_name(opcode)
            if name not in _SET_OPCODE_NAMES:
                raise _parser_malformed(
                    "IN", f"opcode {name} is not valid inside a class"
                )
            if name == "LITERAL":
                codepoint(argument, "IN/LITERAL")
            elif name == "RANGE":
                if type(argument) is not tuple or len(argument) != 2:
                    raise _parser_malformed(
                        "IN/RANGE", "expected a two-codepoint tuple"
                    )
                lower, upper = argument
                codepoint(lower, "IN/RANGE")
                codepoint(upper, "IN/RANGE")
                if lower > upper:
                    raise _parser_malformed(
                        "IN/RANGE", "lower endpoint exceeds upper endpoint"
                    )
            elif name == "CATEGORY":
                category(argument, "IN/CATEGORY")
            elif argument is not None or index != 0:
                raise _parser_malformed(
                    "IN/NEGATE", "expected None in the first position"
                )

    def visit(node) -> None:
        if not isinstance(node, subpattern_type):
            raise _parser_malformed("SubPattern", "expected a parser node")
        try:
            entries = list(node)
        except Exception as exc:
            raise _parser_malformed("SubPattern", "node is not iterable") from exc
        for item in entries:
            if type(item) is not tuple or len(item) != 2:
                raise _parser_malformed(
                    "SubPattern", "opcode entry is not a pair"
                )
            opcode, argument = item
            name = opcode_name(opcode)

            if name == "GROUPREF_EXISTS":
                raise WatchPatternError(
                    "unsupported title pattern: conditional groups are not "
                    "supported; use ordinary alternation instead"
                )
            if name.startswith("GROUPREF"):
                raise WatchPatternError(
                    "unsupported title pattern: backreferences are not supported; "
                    "repeat the subpattern explicitly"
                )
            if name == "ATOMIC_GROUP":
                raise WatchPatternError(
                    "unsupported title pattern: atomic groups are not supported; "
                    "use an ordinary group"
                )
            if name.startswith("POSSESSIVE_REPEAT"):
                raise WatchPatternError(
                    "unsupported title pattern: possessive quantifiers are not "
                    "supported; use an ordinary greedy or lazy quantifier"
                )
            if name not in _SEQUENCE_OPCODE_NAMES:
                raise _parser_malformed(
                    "SubPattern", f"opcode {name} is not valid in a sequence"
                )

            if name in ("LITERAL", "NOT_LITERAL"):
                codepoint(argument, name)
            elif name == "ANY":
                if argument is not None:
                    raise _parser_malformed("ANY", "expected None")
            elif name == "AT":
                try:
                    known_anchor = argument in contract["anchors"]
                except (TypeError, ValueError) as exc:
                    raise _parser_malformed("AT", "unhashable anchor") from exc
                if not known_anchor:
                    raise _parser_malformed("AT", "unknown anchor")
            elif name == "CATEGORY":
                category(argument, "CATEGORY")
            elif name == "IN":
                visit_set(argument)
            elif name == "BRANCH":
                if (
                    type(argument) is not tuple
                    or len(argument) != 2
                    or argument[0] is not None
                    or type(argument[1]) is not list
                ):
                    raise _parser_malformed(
                        "BRANCH", "expected (None, list[SubPattern])"
                    )
                for branch in argument[1]:
                    visit(branch)
            elif name == "SUBPATTERN":
                if type(argument) is not tuple or len(argument) != 4:
                    raise _parser_malformed(
                        "SUBPATTERN",
                        "expected (group, add_flags, del_flags, child)",
                    )
                group, add_flags, del_flags, child = argument
                if group is not None and (
                    not _is_parser_int(group) or group <= 0
                ):
                    raise _parser_malformed(
                        "SUBPATTERN", "group must be None or a positive integer"
                    )
                if not _is_parser_int(add_flags) or not _is_parser_int(
                        del_flags):
                    raise _parser_malformed(
                        "SUBPATTERN", "flags must be integers"
                    )
                changed_flags = add_flags | del_flags
                if changed_flags & re.X:
                    raise WatchPatternError(
                        "unsupported title pattern: verbose x mode is not "
                        "supported; remove the scoped x flag"
                    )
                if changed_flags & ~_ALLOWED_INLINE_FLAGS:
                    raise WatchPatternError(
                        "unsupported title pattern: only scoped i/a/u/m/s flags "
                        "are supported"
                    )
                visit(child)
            elif name in ("MAX_REPEAT", "MIN_REPEAT"):
                if type(argument) is not tuple or len(argument) != 3:
                    raise _parser_malformed(
                        name, "expected (minimum, maximum, child)"
                    )
                minimum, maximum, child = argument
                if (
                    not _is_parser_int(minimum)
                    or not _is_parser_int(maximum)
                    or minimum < 0
                    or maximum < minimum
                    or maximum > contract["maxrepeat"]
                ):
                    raise _parser_malformed(
                        name, "invalid integer repetition bounds"
                    )
                visit(child)
            elif name in ("ASSERT", "ASSERT_NOT"):
                if (
                    type(argument) is not tuple
                    or len(argument) != 2
                    or argument[0] not in (-1, 1)
                ):
                    raise _parser_malformed(
                        name, "expected (-1|1, child)"
                    )
                visit(argument[1])

    visit(parsed)


def _compatible_literal(char: str, unicode_mode: bool) -> str:
    """Return one fixed-width atom with stdlib's active case-mode semantics."""
    if unicode_mode:
        return _TURKISH_I_CLASS
    if char in "iI":
        return "(?-i:[iI])"
    return f"(?-i:{char})"


def _stdlib_compatible_class(fragment: str, unicode_mode: bool) -> str:
    """Wrap one class only when regex.I disagrees with stdlib re.I for Turkish I.

    The original class stays byte-for-byte intact. A fixed-width wrapper first prevents
    it from consuming any I-family codepoint, then explicitly admits exactly the subset
    stdlib would match. This handles Unicode and scoped ASCII modes, positive/negative
    classes, and ranges without rewriting leading ``]``/``-`` or class syntax.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            legacy = tuple(
                re.fullmatch(
                    fragment,
                    char,
                    re.IGNORECASE | (0 if unicode_mode else re.ASCII),
                ) is not None
                for char in _TURKISH_I_CHARS
            )
        current_fragment = (
            fragment if unicode_mode else f"(?a:{fragment})"
        )
        current = tuple(
            regex.fullmatch(
                current_fragment,
                char,
                flags=regex.IGNORECASE,
            ) is not None
            for char in _TURKISH_I_CHARS
        )
    except (re.error, regex.error):
        return fragment
    if legacy == current:
        return fragment
    guarded = f"(?!{_TURKISH_I_CLASS}){fragment}"
    allowed = "".join(
        char for char, matched in zip(_TURKISH_I_CHARS, legacy) if matched
    )
    if not allowed:
        return f"(?:{guarded})"
    return f"(?:{guarded}|(?-i:[{allowed}]))"


def _named_group_prefix_end(pattern: str, start: int) -> int | None:
    """Return the end of a named-group header, excluding its literal body."""
    if pattern.startswith("(?P<", start):
        close = pattern.find(">", start + 4)
        return None if close < 0 else close + 1
    return None


@lru_cache(maxsize=512)
def _stdlib_ignorecase_pattern(pattern: str) -> str:
    """Lexically preserve stdlib re.I's four-way Turkish-I equivalence.

    Only literal codepoints, literal-codepoint escapes, and complete character-class
    atoms are adapted. Named capture headers and inline flag headers are copied
    without inspection. Scoped ``(?-i:...)`` and ASCII ``(?a:...)`` groups suppress
    adaptation exactly as they suppress the caller's global Unicode IGNORECASE
    behavior. Validation excludes every broader grammar construct before this
    transformer runs. The result still performs one regex search with one timeout.
    """
    output: list[str] = []
    # (IGNORECASE enabled, Unicode matching enabled). The public call starts with
    # regex.I on a str pattern, equivalent to stdlib re.I's Unicode mode.
    flag_stack = [(True, True)]
    index = 0
    while index < len(pattern):
        char = pattern[index]

        if char == "\\":
            end = _escape_end(pattern, index)
            token = pattern[index:end]
            escaped = _escaped_codepoint(token)
            if (
                flag_stack[-1][0]
                and escaped is not None
                and escaped in _TURKISH_I_CHARS
            ):
                output.append(
                    _compatible_literal(escaped, flag_stack[-1][1])
                )
            else:
                output.append(token)
            index = end
            continue

        if char == "[":
            end = _class_end(pattern, index)
            fragment = pattern[index:end]
            output.append(
                _stdlib_compatible_class(fragment, flag_stack[-1][1])
                if flag_stack[-1][0]
                else fragment
            )
            index = end
            continue

        named_end = _named_group_prefix_end(pattern, index)
        if named_end is not None:
            output.append(pattern[index:named_end])
            flag_stack.append(flag_stack[-1])
            index = named_end
            continue

        if pattern.startswith("(?", index):
            flag_header = re.match(
                r"\(\?([aimsu]*)(?:-([ims]*))?([:)])",
                pattern[index:],
            )
            if flag_header:
                enabled, disabled, terminator = flag_header.groups()
                ignorecase, unicode_mode = flag_stack[-1]
                if "i" in enabled:
                    ignorecase = True
                if disabled and "i" in disabled:
                    ignorecase = False
                if "a" in enabled:
                    unicode_mode = False
                if "u" in enabled:
                    unicode_mode = True
                header = flag_header.group(0)
                output.append(header)
                if terminator == ":":
                    flag_stack.append((ignorecase, unicode_mode))
                else:
                    flag_stack[-1] = (ignorecase, unicode_mode)
                index += len(header)
                continue

        if char == "(":
            output.append(char)
            flag_stack.append(flag_stack[-1])
            index += 1
            continue

        if char == ")":
            output.append(char)
            if len(flag_stack) > 1:
                flag_stack.pop()
            index += 1
            continue

        if flag_stack[-1][0] and char in _TURKISH_I_CHARS:
            output.append(_compatible_literal(char, flag_stack[-1][1]))
        else:
            output.append(char)
        index += 1

    return "".join(output)


@lru_cache(maxsize=512)
def _validated_compatible_pattern(pattern: str) -> str:
    """Return the compiled-safe compatibility form for one accepted pattern."""
    try:
        with warnings.catch_warnings(record=True) as compile_warnings:
            warnings.simplefilter("always", FutureWarning)
            re.compile(pattern, re.I)
    except re.error as exc:
        raise WatchPatternError(
            "invalid title pattern: only Python stdlib re syntax is supported "
            f"({exc})"
        ) from exc
    except OverflowError as exc:
        raise WatchPatternError(
            "invalid title pattern: repetition bound is too large; "
            "use a smaller repetition bound"
        ) from exc

    contract = _parser_contract()
    try:
        with warnings.catch_warnings(record=True) as parser_warnings:
            warnings.simplefilter("always", FutureWarning)
            parsed = contract["parse"](pattern, re.I)
    except Exception as exc:
        raise WatchPatternError(
            "title pattern parser could not safely inspect this pattern: "
            f"{exc}"
        ) from exc

    syntax_warnings = compile_warnings + parser_warnings
    if syntax_warnings:
        raise WatchPatternError(
            "unsupported title pattern: ambiguous character-set syntax is not "
            "supported; escape literal [, &, or - characters"
        )
    _validate_subset(parsed, pattern, contract)
    if _contains_non_quantifier_brace(pattern):
        raise WatchPatternError(
            "unsupported title pattern: regex extension or unescaped literal "
            "brace; escape literal braces and use only {m}, {m,}, or {m,n} "
            "quantifiers"
        )
    try:
        compatible = _stdlib_ignorecase_pattern(pattern)
        regex.compile(compatible, flags=regex.I)
    except Exception as exc:
        raise WatchPatternError(
            f"title pattern could not be safely prepared: {exc}"
        ) from exc
    return compatible


def title_matches(pattern: str, title: str) -> bool:
    """Case-insensitively match *title* with a strict per-operation timeout."""
    checked = validate_title_pattern(pattern)
    if checked is None:
        return True
    compatible = _validated_compatible_pattern(checked)
    try:
        return regex.search(
            compatible,
            str(title or ""),
            flags=regex.IGNORECASE,
            timeout=MATCH_TIMEOUT_SECONDS,
        ) is not None
    except TimeoutError as exc:
        # Rendered FROM the constant, not hardcoded. Three tests assert on the literal "50 ms", so a
        # hardcoded message let the bound be widened (0.050 -> 0.200) with every assertion still
        # green while the user-facing and persisted error text silently became false. Deriving it
        # makes those three assertions load-bearing at no cost: 0.050 still renders "50 ms".
        raise WatchPatternTimeout(
            f"title pattern exceeded {MATCH_TIMEOUT_SECONDS * 1000:g} ms"
        ) from exc
    except regex.error as exc:
        raise WatchPatternError(
            f"title pattern could not be evaluated: {exc}"
        ) from exc
