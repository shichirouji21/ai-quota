from ai_quota.ansi import strip_ansi


def test_strip_csi_color():
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_multiple_sequences():
    s = "\x1b[1;32mgreen bold\x1b[0m and \x1b[2m dim \x1b[0m"
    assert strip_ansi(s) == "green bold and  dim "


def test_strip_cursor_moves():
    assert strip_ansi("hello\x1b[2Kworld") == "helloworld"


def test_strip_generic_escape():
    assert strip_ansi("a\x1b7b\x1b8c") == "abc"


def test_no_ansi_passthrough():
    assert strip_ansi("plain text") == "plain text"


def test_empty():
    assert strip_ansi("") == ""
