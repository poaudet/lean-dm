"""WP0 - ThinkFilter must use real tag literals, not empty strings.

With empty tags, "".find("") returns 0 forever and feed() never terminates, so
test_think_filter_splits_tags_across_chunks hangs rather than fails.

The tag strings are assembled from parts so no invisible character can creep into
the literals; ThinkFilter matches plain tags, not zero-width variants.
"""
import dm

OPEN = "<" + "think>"
CLOSE = "<" + "/think>"


def test_think_filter_splits_tags_across_chunks():
    f = dm.ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hi <thi", "nk>secret</th", "ink> there"]) + f.flush()
    assert out == "Hi  there"


def test_whole_tag_in_one_chunk():
    f = dm.ThinkFilter()
    assert f.feed("A" + OPEN + "hidden" + CLOSE + "B") + f.flush() == "AB"


def test_unterminated_block_stays_hidden():
    f = dm.ThinkFilter()
    assert f.feed("A" + OPEN + "never closed") + f.flush() == "A"


def test_plain_text_passes_through():
    f = dm.ThinkFilter()
    assert f.feed("just words") + f.flush() == "just words"


def test_two_blocks_in_sequence():
    f = dm.ThinkFilter()
    src = "x" + OPEN + "a" + CLOSE + "y" + OPEN + "b" + CLOSE + "z"
    assert f.feed(src) + f.flush() == "xyz"
