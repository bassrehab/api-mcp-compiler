"""The vocabulary this project uses to read what wording implies.

One question recurs wherever a surface is judged: **does the language contradict the declared
class?** An operation whose HTTP method says read while its name says purge is either
mislabelled or misimplemented, and an agent reading the description will act on the wording
either way.

The same question is asked about a tool descriptor that claims to be read-only, about a stored
procedure named `SP_DELETE_CLAIM`, and about anything else that arrives with a self-description
and a claim about itself. So the vocabulary lives here rather than inside the OpenAPI adapter
that happened to need it first.

It is public because a caller outside this package asks the same question about a surface this
compiler did not produce. Two lists of what "destructive" sounds like would give two answers
about the same word, and the one anybody acted on would be whichever they happened to load.

This is deliberately a small, blunt list rather than a classifier. It exists to raise a
question for a person, never to answer one: everything that consults it treats a hit as a
reason to look, and never as a reason to act.
"""

from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

#: Words that describe destroying something. Whole words only, so `undelete` and `deleted_at`
#: do not match on a substring the way a naive `in` test would.
DESTRUCTIVE_TOKENS = frozenset(
    {"delete", "purge", "destroy", "erase", "wipe", "drop", "remove", "revoke", "terminate"}
)


def word_tokens(text: str) -> set[str]:
    """Split an identifier or sentence into lowercase whole words.

    Handles the three ways a name arrives in practice at once: `deleteOrder`, `delete_order`
    and "Delete an order" all yield `delete`.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return {piece.lower() for piece in _NON_WORD.split(spaced) if piece}


def destructive_signals(*texts: str) -> list[str]:
    """Every destructive word appearing in any of these texts, sorted.

    Sorted rather than a set so a message built from it reads the same on every run, which
    matters when the message ends up in a report somebody compares against last month's.
    """
    tokens: set[str] = set()
    for text in texts:
        if text:
            tokens |= word_tokens(text)
    return sorted(DESTRUCTIVE_TOKENS & tokens)
