from yuxu.core.frontmatter import parse_frontmatter


def test_no_frontmatter():
    fm, body = parse_frontmatter("hello")
    assert fm == {}
    assert body == "hello"


def test_basic():
    text = "---\nname: foo\nrun_mode: persistent\n---\nbody here\n"
    fm, body = parse_frontmatter(text)
    assert fm == {"name": "foo", "run_mode": "persistent"}
    assert body.strip() == "body here"


def test_list():
    text = "---\ndepends_on: [a, b, c]\n---\n"
    fm, _ = parse_frontmatter(text)
    assert fm["depends_on"] == ["a", "b", "c"]


def test_types():
    text = "---\nready_timeout: 30\nedit_warning: true\nscope: system\n---\n"
    fm, _ = parse_frontmatter(text)
    assert fm["ready_timeout"] == 30
    assert fm["edit_warning"] is True
    assert fm["scope"] == "system"


def test_unclosed_frontmatter():
    fm, body = parse_frontmatter("---\nfoo: bar\nno closing")
    assert fm == {}
    assert body.startswith("---")


def test_malformed_yaml():
    # unterminated flow mapping - truly malformed
    fm, body = parse_frontmatter("---\nkey: {unterminated\n---\nbody\n")
    assert fm == {}


def test_non_mapping_returns_empty():
    # top-level scalar/list is not a valid frontmatter dict
    fm, _ = parse_frontmatter("---\n- a\n- b\n---\n")
    assert fm == {}
