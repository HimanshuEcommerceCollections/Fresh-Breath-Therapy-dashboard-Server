"""Spreadsheet import.

Layering, and the rule that holds the whole design together:

    the model PROPOSES a mapping — code DECIDES what is written

`matcher.py` may consult an LLM to guess that a column headed "Client Nm"
means `name`. That guess is a *config*: a {source_header -> field} dict the
admin sees, edits and approves. Everything downstream of it — parsing,
normalizing, validating, resolving foreign keys, writing rows — is ordinary
deterministic Python that never sees a model. So a bad guess produces a
visibly wrong mapping on the review screen, not a silently corrupted patient
record, and fixing one mapping line re-corrects every row at once.

Nothing here requires an LLM to be configured. The matcher's deterministic
pass (exact match, then alias table, then edit distance) is the default; the
model is an optional accuracy improvement on top.
"""
