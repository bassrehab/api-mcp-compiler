"""The shared vocabulary for reading what wording implies.

Small, and worth its own tests because two callers now depend on it agreeing with itself:
this compiler asks whether an HTTP method contradicts an operation's name, and a caller
outside asks whether a tool descriptor contradicts its own read-only claim. A word that means
destructive in one place and not the other would give a product two answers.
"""

from __future__ import annotations

from api_mcp_compiler.language import DESTRUCTIVE_TOKENS, destructive_signals, word_tokens


def test_the_three_ways_a_name_arrives_all_tokenise_the_same() -> None:
    """`deleteOrder`, `delete_order` and a sentence are the same claim written differently."""
    assert "delete" in word_tokens("deleteOrder")
    assert "delete" in word_tokens("delete_order")
    assert "delete" in word_tokens("Delete an order")
    assert "delete" in word_tokens("DELETE-ORDER")


def test_a_word_is_matched_whole_and_not_as_a_substring() -> None:
    """`undelete` restores things and `deleted_at` is a column. Neither destroys anything.

    A naive containment test flags both, and a report that cries wolf on a timestamp column
    is one nobody reads twice.
    """
    assert destructive_signals("undelete") == []
    assert destructive_signals("deletedAt") == []
    assert destructive_signals("get_deleted_records") == []


def test_signals_are_sorted_so_a_message_reads_the_same_every_run() -> None:
    """These end up in reports somebody compares against last month's."""
    assert destructive_signals("purgeAndDelete", "wipe everything") == [
        "delete",
        "purge",
        "wipe",
    ]


def test_the_vocabulary_is_lowercase_whole_words() -> None:
    """Anything else silently fails to match, since tokens are lowercased before comparison."""
    assert all(token == token.lower() and token.isalpha() for token in DESTRUCTIVE_TOKENS)


def test_empty_and_absent_text_contribute_nothing() -> None:
    assert destructive_signals() == []
    assert destructive_signals("", "get order") == []
