from pkg.strings import reverse, upper, word_count


def test_reverse():
    assert reverse("hello") == "olleh"   # will FAIL due to bug in strings.py


def test_upper():
    assert upper("hello") == "HELLO"


def test_word_count():
    assert word_count("the quick brown fox") == 4
