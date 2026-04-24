"""dump_frontmatter — YAML-safe serialization with round-trip guarantee."""
from __future__ import annotations

import yaml

from yuxu.bundled._shared import dump_frontmatter


def _roundtrip(fm: dict) -> dict:
    """Serialize + parse through a real YAML loader."""
    text = dump_frontmatter(fm) + "\n"
    # Strip the `---` markers for yaml.safe_load
    body = text.strip().strip("-").strip()
    return yaml.safe_load(body)


def test_plain_strings_stay_bare():
    fm = {"name": "Simple", "description": "just words"}
    text = dump_frontmatter(fm)
    assert "name: Simple" in text
    assert 'name: "Simple"' not in text  # not over-quoted


def test_strings_with_colons_get_quoted():
    fm = {"description": "key: value inside text"}
    out = _roundtrip(fm)
    assert out["description"] == "key: value inside text"


def test_strings_starting_with_special_chars_get_quoted():
    # YAML reserved first-chars: `[`, `{`, `?`, `&`, `*`, `-`, `!`, `|`, `>`, `%`, `@`
    fm1 = {"d": "[bracket] leads"}
    fm2 = {"d": "{brace} leads"}
    fm3 = {"d": "? question leads"}
    assert _roundtrip(fm1)["d"] == "[bracket] leads"
    assert _roundtrip(fm2)["d"] == "{brace} leads"
    assert _roundtrip(fm3)["d"] == "? question leads"


def test_strings_with_quotes_and_braces_dont_break():
    fm = {"description": 'has "double" and {brace} and # hash'}
    out = _roundtrip(fm)
    assert out["description"] == 'has "double" and {brace} and # hash'


def test_json_like_content_survives():
    """The failure that actually happened: description contained raw
    JSONL content. YAML would choke on unquoted; our dump must quote."""
    fm = {"description": '{"type": "queue-operation", "operation": "enqueue"}'}
    out = _roundtrip(fm)
    assert '"type"' in out["description"]


def test_reserved_scalars_get_quoted():
    # YAML parses unquoted `yes` / `no` / `true` / `null` as bool/null.
    # Our dump must keep them as strings.
    fm = {"d": "yes", "e": "true", "f": "null"}
    out = _roundtrip(fm)
    assert out["d"] == "yes"
    assert out["e"] == "true"
    assert out["f"] == "null"


def test_bool_value_preserved():
    fm = {"flag": True, "flip": False}
    out = _roundtrip(fm)
    assert out["flag"] is True
    assert out["flip"] is False


def test_none_value_preserved():
    fm = {"x": None}
    out = _roundtrip(fm)
    assert out["x"] is None


def test_list_value_json_formatted():
    fm = {"tags": ["a", "b", "c"]}
    out = _roundtrip(fm)
    assert out["tags"] == ["a", "b", "c"]


def test_dict_value_json_formatted():
    fm = {"score": {"applied": 3, "helped": 0, "hurt": 0}}
    out = _roundtrip(fm)
    assert out["score"] == {"applied": 3, "helped": 0, "hurt": 0}


def test_unicode_strings_round_trip():
    fm = {"description": "恢复记忆 — Phase 4+5 closeout"}
    out = _roundtrip(fm)
    assert out["description"] == "恢复记忆 — Phase 4+5 closeout"


def test_empty_string_is_quoted():
    fm = {"d": ""}
    out = _roundtrip(fm)
    assert out["d"] == ""


def test_multiline_string_handled():
    fm = {"d": "line one\nline two"}
    out = _roundtrip(fm)
    assert out["d"] == "line one\nline two"


def test_real_session_entry_frontmatter_round_trips():
    """Regression: the full shape session_compressor writes."""
    fm = {
        "name": "Session 2026-04-24 023fac6d — Phase 4+5 closeout",
        "description": ('The user requested to complete the final phases '
                         '(我们把memory的plan mode的最后phase完成吧), then '
                         'wrap up: "存记忆，存对话jsonl".'),
        "type": "session",
        "scope": "project",
        "evidence_level": "observed",
        "status": "current",
        "tags": ["session"],
        "originSessionId": "023fac6d-08cb-4e76-9459-bc650b217663",
        "source_path": "/home/x/.claude/projects/<proj>/023fac6d.jsonl",
        "source_bytes": 2768886,
        "compressed_bytes": 6901,
        "compression_ratio": 0.9975,
        "fallback_used": False,
        "updated": "2026-04-24",
    }
    out = _roundtrip(fm)
    for k, v in fm.items():
        if k == "updated":
            # yaml.safe_load coerces YYYY-MM-DD to datetime.date — accept both
            assert str(out[k]) == v
        else:
            assert out[k] == v, f"mismatch on {k!r}: {out[k]!r} != {v!r}"
