"""The shared vocabulary for reading what wording implies.

Small, and worth its own tests because two callers now depend on it agreeing with itself:
this compiler asks whether an HTTP method contradicts an operation's name, and a caller
outside asks whether a tool descriptor contradicts its own read-only claim. A word that means
destructive in one place and not the other would give a product two answers.
"""

from __future__ import annotations

from api_mcp_compiler.language import (
    DESTRUCTIVE_TOKENS,
    base_form,
    destructive_signals,
    word_tokens,
)


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


def test_a_description_written_the_way_descriptions_are_written_is_matched() -> None:
    """"Deletes a customer" is the most likely wording there is, and it used to miss.

    Names carry the base form and summaries carry the third person, so matching only the base
    form caught the half of the corpus written by machines and missed the half written by
    people.
    """
    assert destructive_signals("Deletes a customer") == ["delete"]
    assert destructive_signals("Removes rows older than the retention window") == ["remove"]
    assert destructive_signals("Purges the cache") == ["purge"]
    assert destructive_signals("Erases every attachment") == ["erase"]


def test_the_base_form_is_reported_rather_than_the_form_found() -> None:
    """A report gets compared against last month's, so the wording has to be stable."""
    assert destructive_signals("Deletes things", "delete_thing") == ["delete"]


def test_a_past_participle_is_not_a_verb_somebody_performed() -> None:
    """`deleted_at` is a column on almost every table that has ever existed.

    Folding participles would flag every schema that records when something was removed, and
    the first false positive on something that universal ends the report's credibility.
    """
    assert destructive_signals("deleted_at") == []
    assert destructive_signals("is_deleted") == []
    assert destructive_signals("Lists deleted records") == []
    assert destructive_signals("removing", "dropped", "purged") == []


def test_folding_does_not_invent_matches_from_ordinary_words() -> None:
    """The rule only strips a trailing s, so it must not turn innocent words destructive."""
    assert destructive_signals("address", "status", "process", "bus", "gas") == []
    assert base_form("address") == "address"
    assert base_form("is") == "is"


def test_the_singular_of_a_short_word_is_left_alone() -> None:
    """Stripping from a three-letter token produces noise rather than a word."""
    assert base_form("its") == "its"
