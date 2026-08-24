"""Report serialization tests."""

from custom_components.speedster import report


def test_js_num_rejects_non_finite_values() -> None:
    """Emit valid JavaScript for corrupted numeric CSV cells."""
    assert report._js_num("NaN") == "null"
    assert report._js_num("Infinity") == "null"
    assert report._js_num("not-a-number") == "null"
    assert report._js_num("1.25") == "1.25"


def test_js_str_cannot_close_script_element() -> None:
    """Escape report data before embedding it in script block."""
    assert report._js_str("</script>\n") == '"\\u003c/script>\\n"'
