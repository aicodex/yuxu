"""Gateway inline_expander — $ARGUMENTS / $1 / $foo substitution and
`!cmd` / fenced `!` preamble execution."""
from __future__ import annotations

import pytest

from yuxu.bundled.gateway.inline_expander import (
    execute_preambles,
    expand_inline_skill,
    parse_named_args,
    run_shell,
    substitute_args,
)


# -- named arg parsing ------------------------------------------


def test_parse_named_args_list_form():
    assert parse_named_args({"argument_names": ["a", "b"]}) == ["a", "b"]


def test_parse_named_args_kebab_form():
    assert parse_named_args({"argument-names": ["x", "y"]}) == ["x", "y"]


def test_parse_named_args_string_form():
    assert parse_named_args({"argument_names": "foo bar baz"}) == ["foo", "bar", "baz"]


def test_parse_named_args_missing():
    assert parse_named_args({}) == []


# -- argument substitution --------------------------------------


def test_substitute_arguments_verbatim():
    out = substitute_args("hello $ARGUMENTS",
                          args_raw="the args", positional=[], named={})
    assert out == "hello the args"


def test_substitute_positional():
    out = substitute_args("first=$1 second=$2 missing=$9",
                          args_raw="a b", positional=["a", "b"], named={})
    assert out == "first=a second=b missing="


def test_substitute_named_longest_first():
    # `$foobar` must win over `$foo` when both are registered
    out = substitute_args("$foo $foobar",
                          args_raw="",
                          positional=[],
                          named={"foo": "X", "foobar": "Y"})
    assert out == "X Y"


def test_substitute_numeric_word_boundary():
    # $10 should match position 10 (missing → ""), not $1 followed by "0"
    out = substitute_args("$10",
                          args_raw="a", positional=["a"], named={})
    assert out == ""


# -- shell preambles --------------------------------------------


@pytest.mark.asyncio
async def test_run_shell_captures_stdout():
    out = await run_shell("echo hello")
    assert "hello" in out


@pytest.mark.asyncio
async def test_run_shell_reports_nonzero_exit():
    out = await run_shell("false")
    assert "[exit 1]" in out


@pytest.mark.asyncio
async def test_run_shell_timeout():
    out = await run_shell("sleep 5", timeout=0.1)
    assert "timed out" in out


@pytest.mark.asyncio
async def test_execute_preambles_inline():
    out = await execute_preambles("date=!`echo fixed` end")
    assert "date=fixed" in out
    assert "end" in out


@pytest.mark.asyncio
async def test_execute_preambles_fenced():
    src = "before\n```!\necho hi\n```\nafter"
    out = await execute_preambles(src)
    assert "before" in out and "after" in out and "hi" in out


# -- end-to-end expand -----------------------------------------


@pytest.mark.asyncio
async def test_expand_inline_skill_substitutes_then_runs_preambles():
    body = "subject=$subject date=!`echo 2026-04-22`"
    out = await expand_inline_skill(
        body, args_raw="market", frontmatter={"argument_names": ["subject"]},
    )
    assert "subject=market" in out
    assert "date=2026-04-22" in out
