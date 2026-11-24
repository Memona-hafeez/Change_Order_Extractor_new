from chunker import select_relevant, split_chunks, score


def test_short_documents_pass_through_unchanged():
    t = "Change order\nThis Change Order: $1,000.00"
    assert select_relevant(t) == t


def test_long_document_keeps_the_money_heavy_chunk():
    filler = "lorem ipsum dolor sit amet. " * 1200           # ~33k chars, no keywords
    money = "\nOriginal Contract Sum: $1,000,000.00\nThis Change Order: $5,000.00\nNew Contract Sum: $1,005,000.00\n"
    text = filler[:20000] + money + filler[:15000]
    out = select_relevant(text, max_chars=6000)
    assert "New Contract Sum: $1,005,000.00" in out
    assert len(out) < len(text)


def test_score_rewards_keywords_and_dollar_amounts():
    assert score("contract sum $5,000 owner") > score("nothing relevant here")


def test_split_chunks_overlap():
    chunks = split_chunks("a" * 3000, size=1500, overlap=200)
    assert chunks[1][0] == 1300
