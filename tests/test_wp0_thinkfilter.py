"""WP0 - ThinkFilter must use real tag literals, not empty strings.

With empty tags, "".find("") returns 0 forever and feed() never terminates, so
test_think_filter_splits_tags_across_chunks hangs rather than fails.
"""
import dm


def test_think_filter_splits_tags_across_chunks():
    f = dm.ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hi <thi", "nk>secret</th", "ink> there"]) + f.flush()
    assert out == "Hi  there"


def test_whole_tag_in_one_chunk():
    f = dm.ThinkFilter()
    assert f.feed("A<think>hidden</think>B") + f.flush() == "AB"


def test_unterminated_block_stays_hidden():
    f = dm.ThinkFilter()
    assert f.feed("y<think>b</think>z"
    assert f.feed(src) + f.flush() == "xyz"
