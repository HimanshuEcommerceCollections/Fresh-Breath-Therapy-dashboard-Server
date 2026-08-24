"""Proposing which source column belongs in which field.

Three passes, cheapest and most certain first:

  1. exact  — the header already is the field name or its label
  2. alias  — the header is in the field's synonym list (registry.py)
  3. fuzzy  — edit distance, above a deliberately high threshold

Whatever is still unmapped can optionally be handed to a language model. That
call is the ONLY place a model appears in the whole import, and what comes
back is a `{header: field}` dict the admin reviews — never a value, never a
row. Two properties make that safe:

  * the model's output is constrained to `mappable_field_names()`, an `enum`
    in its response schema, so it cannot name a field that doesn't exist —
    "don't invent columns" is enforced by the schema, not requested in a
    prompt that can be ignored;
  * every proposal is then checked against real data by `_parse_rate`, so a
    confident-sounding "Ph -> email" is caught by the fact that none of the
    sample values parse as an email address.

Nothing here is required. With no model configured the deterministic passes
run alone and the admin fills the gaps from a dropdown.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from typing import Protocol

from app.services.importer import normalizers
from app.services.log_redaction import redact
from app.services.importer.parser import ParsedSheet
from app.services.importer.registry import (
    FieldKind, FieldSpec, get_entity, mappable_field_names,
)

logger = logging.getLogger(__name__)

# Above this, a fuzzy match is offered pre-selected; below it the column is
# left unmapped. Set high on purpose — a wrong pre-filled mapping that looks
# plausible is worse than an obvious blank, because the blank gets fixed.
FUZZY_THRESHOLD = 0.82
# A mapping whose sample values mostly fail to parse is contradicted by the
# data, whatever its name suggests.
MIN_PARSE_RATE = 0.5
# At or under this many distinct values, a column is a vocabulary (statuses,
# methods) rather than free text, and can be shown to a model verbatim.
LOW_CARDINALITY = 25

# Kinds where a parse rate is meaningful. Text and FK accept any string, so a
# rate would always be 1.0 and tell nobody anything.
_CHECKABLE = {
    FieldKind.EMAIL, FieldKind.PHONE, FieldKind.DATE, FieldKind.TIME,
    FieldKind.MONEY, FieldKind.INT, FieldKind.BOOL, FieldKind.ENUM,
}


def _norm(text: str) -> str:
    """Header -> comparison key: lowercase, alphanumeric only.

    Collapses "Client Nm.", "client_nm" and "CLIENT NM" to one string, which
    is most of what header matching needs before any cleverness.
    """
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class ColumnSuggestion:
    header: str
    field: str | None = None
    confidence: float = 0.0
    # "exact" | "alias" | "fuzzy" | "llm" | "none" — shown in the UI so the
    # admin knows whether to trust a row at a glance.
    reason: str = "none"
    samples: list[str] = dc_field(default_factory=list)
    distinct_count: int = 0
    # Fraction of sampled values that parse as the mapped field's type.
    parse_rate: float | None = None
    warning: str | None = None


@dataclass
class MappingProposal:
    columns: list[ColumnSuggestion]
    # Required fields with no column pointed at them. While this is non-empty
    # the commit is blocked — every one of these is NOT NULL in the database.
    unmapped_required: list[str]
    date_order: str = "MDY"
    date_order_confident: bool = False

    def as_mapping(self) -> dict[str, str | None]:
        return {c.header: c.field for c in self.columns}


class MappingModel(Protocol):
    """Optional LLM hook. Implementations live outside this module.

    `column_profiles` is PHI-reduced by `build_column_profiles` before it gets
    here. Any implementation must be pointed at an endpoint covered by a BAA
    — consumer Gemini is not one.
    """

    async def propose(
        self, *, entity: str, column_profiles: list[dict], allowed_fields: list[str]
    ) -> dict[str, str | None]:
        ...


_model: MappingModel | None = None


def set_mapping_model(model: MappingModel | None) -> None:
    global _model
    _model = model


def _mask(value: str) -> str:
    """Preserve a value's shape, discard its content.

    "Jane Doe" -> "Xxxx Xxx", "jane@x.com" -> "xxxx@x.xxx", "919-300-6717" ->
    "999-999-9999". Enough for a model to tell a name column from an email
    column from a phone column; not enough to identify a patient.
    """
    out = []
    for ch in str(value)[:60]:
        if ch.isdigit():
            out.append("9")
        elif ch.isupper():
            out.append("X")
        elif ch.islower():
            out.append("x")
        else:
            out.append(ch)
    return "".join(out)


def build_column_profiles(sheet: ParsedSheet) -> list[dict]:
    """What a model is allowed to see about each column.

    Low-cardinality columns go verbatim — the six spellings of "ongoing" in a
    status column are a vocabulary, and the model needs them to propose the
    value mapping. Everything else is masked to its shape. Either way the
    payload is a description of the column, not its contents.
    """
    profiles = []
    for header in sheet.headers:
        distinct = sheet.distinct(header, limit=LOW_CARDINALITY + 1)
        samples = sheet.samples(header, limit=8)
        low_cardinality = 0 < len(distinct) <= LOW_CARDINALITY
        # Low-cardinality columns exist so the model can see an enum's
        # vocabulary ("Ongoing Therapy" -> ongoing_therapy), which needs the
        # real strings. But "few distinct values" is a proxy for "this is an
        # enum", and on a SMALL SHEET a name, email or phone column satisfies
        # it too — a 12-row import has at most 12 distinct emails. Those were
        # going out verbatim.
        #
        # So the vocabulary is still sent, with anything PHI-SHAPED scrubbed
        # out of it first. An enum value is unaffected; an address or a phone
        # number is not a vocabulary and loses nothing by being redacted.
        values = (
            [_scrub_phi(v) for v in distinct]
            if low_cardinality else [_mask(s) for s in samples]
        )
        profiles.append({
            "header": header,
            "distinct_count": len(sheet.value_counts(header)),
            "low_cardinality": low_cardinality,
            "values": values,
            "masked": not low_cardinality,
        })
    return profiles


# Longest value worth showing a model. Past this it is free text, not a
# vocabulary, and a long cell is exactly where a note or an address lives.
MAX_PROFILE_VALUE_LENGTH = 60


def _scrub_phi(value) -> str:
    """A low-cardinality value with anything identifying removed.

    Reuses the log filter's patterns so there is ONE definition of
    "PHI-shaped" in the codebase rather than two that drift.
    """
    text = str(value)[:MAX_PROFILE_VALUE_LENGTH]
    return redact(text)


def _parse_rate(spec: FieldSpec, samples: list, date_order: str) -> float | None:
    """Fraction of sample values that survive this field's parser.

    This is the check that catches a plausible-sounding but wrong mapping. A
    column headed "Ph" mapped to `email` scores 0.0, and the review screen can
    say so before anything is written.
    """
    if spec.kind not in _CHECKABLE or not samples:
        return None
    ok = 0
    for value in samples:
        try:
            normalizers.normalize_cell(spec, value, date_order=date_order)
            ok += 1
        except normalizers.CellError:
            pass
        except Exception:  # a parser bug must not fail the whole upload
            logger.exception("Unexpected error sampling %s", spec.name)
    return ok / len(samples)


def _deterministic_match(header: str, specs: list[FieldSpec]) -> tuple[str | None, float, str]:
    key = _norm(header)
    if not key:
        return None, 0.0, "none"

    for spec in specs:
        if key == _norm(spec.name) or key == _norm(spec.label):
            return spec.name, 1.0, "exact"

    for spec in specs:
        for alias in spec.aliases:
            if key == _norm(alias):
                return spec.name, 0.95, "alias"

    # Fuzzy, over names, labels and aliases alike; best score wins.
    best_field, best_score = None, 0.0
    for spec in specs:
        for candidate in (spec.name, spec.label, *spec.aliases):
            score = _similar(key, _norm(candidate))
            if score > best_score:
                best_field, best_score = spec.name, score
    if best_score >= FUZZY_THRESHOLD:
        return best_field, best_score, "fuzzy"
    return None, best_score, "none"


def _enforce_one_to_one(columns: list[ColumnSuggestion]) -> None:
    """Two columns cannot fill the same field.

    Sheets often carry "Therapist" and "Therapist Notes"; both fuzzy-match
    `therapist`. The stronger claim keeps it, the weaker is unmapped with a
    note rather than silently overwriting on alternate rows.
    """
    claimed: dict[str, ColumnSuggestion] = {}
    for column in columns:
        if not column.field:
            continue
        held = claimed.get(column.field)
        if held is None:
            claimed[column.field] = column
            continue
        loser, winner = (
            (column, held) if column.confidence <= held.confidence else (held, column)
        )
        claimed[column.field] = winner
        loser.warning = (
            f'Also looked like "{loser.field}", but "{winner.header}" is the '
            "better match. Pick a field if this column should be imported."
        )
        loser.field, loser.confidence, loser.reason = None, 0.0, "none"


def propose_mapping(entity_key: str, sheet: ParsedSheet) -> MappingProposal:
    """Deterministic pass. No model, no network, no configuration required."""
    entity = get_entity(entity_key)
    specs = list(entity.mappable_fields)

    # Set the date order first: every subsequent date parse check depends on
    # it. Sampled across all columns, since we don't yet know which are dates.
    date_samples: list = []
    for header in sheet.headers:
        date_samples.extend(sheet.samples(header, limit=6))
    date_order, confident = normalizers.detect_date_order(date_samples)

    columns: list[ColumnSuggestion] = []
    for header in sheet.headers:
        samples = sheet.samples(header)
        field_name, score, reason = _deterministic_match(header, specs)
        column = ColumnSuggestion(
            header=header,
            field=field_name,
            confidence=round(score, 3),
            reason=reason,
            samples=[str(s)[:80] for s in samples[:5]],
            distinct_count=len(sheet.value_counts(header)),
        )
        if field_name:
            spec = entity.field(field_name)
            column.parse_rate = _parse_rate(spec, samples, date_order)
            if column.parse_rate is not None and column.parse_rate < MIN_PARSE_RATE:
                pct = int(column.parse_rate * 100)
                if reason == "fuzzy":
                    # A guess the data contradicts is worse than no guess.
                    column.warning = (
                        f'Looked like "{spec.label}", but only {pct}% of values '
                        "parse that way — left unmapped."
                    )
                    column.field, column.confidence, column.reason = None, 0.0, "none"
                else:
                    column.warning = (
                        f"Only {pct}% of sampled values look like a valid "
                        f"{spec.label.lower()}."
                    )
        columns.append(column)

    _enforce_one_to_one(columns)

    mapped = {c.field for c in columns if c.field}
    return MappingProposal(
        columns=columns,
        unmapped_required=[
            f.name for f in entity.required_fields if f.name not in mapped
        ],
        date_order=date_order,
        date_order_confident=confident,
    )


async def propose_mapping_with_model(
    entity_key: str, sheet: ParsedSheet
) -> MappingProposal:
    """Deterministic pass, then the model on whatever is left over.

    The model only ever sees columns the cheap passes couldn't place, and can
    only fill blanks — it is never allowed to overturn an exact or alias
    match, both of which are already certain. Its suggestions go through the
    same parse-rate check as everything else, and any failure here degrades to
    the deterministic result rather than failing the upload.
    """
    proposal = propose_mapping(entity_key, sheet)
    if _model is None:
        return proposal

    leftovers = [c for c in proposal.columns if not c.field]
    if not leftovers:
        return proposal

    entity = get_entity(entity_key)
    taken = {c.field for c in proposal.columns if c.field}
    available = [f for f in mappable_field_names(entity_key) if f not in taken]
    if not available:
        return proposal

    leftover_headers = {c.header for c in leftovers}
    profiles = [
        p for p in build_column_profiles(sheet) if p["header"] in leftover_headers
    ]

    try:
        suggested = await _model.propose(
            entity=entity_key, column_profiles=profiles, allowed_fields=available,
        )
    except Exception:
        logger.exception("Mapping model failed; using deterministic mapping only")
        return proposal

    by_header = {c.header: c for c in proposal.columns}
    for header, field_name in (suggested or {}).items():
        column = by_header.get(header)
        # Ignore anything outside the allowed set. The response schema should
        # already prevent it; this is the belt to that braces, because a model
        # naming a non-existent field must never reach the database layer.
        if column is None or column.field or field_name not in available:
            continue
        spec = entity.field(field_name)
        if spec is None:
            continue
        rate = _parse_rate(spec, sheet.samples(header), proposal.date_order)
        if rate is not None and rate < MIN_PARSE_RATE:
            column.warning = (
                f'Suggested "{spec.label}", but only {int(rate * 100)}% of '
                "values parse that way — left unmapped."
            )
            continue
        column.field = field_name
        column.confidence = 0.6           # a suggestion, not a certainty
        column.reason = "llm"
        column.parse_rate = rate
        available.remove(field_name)

    _enforce_one_to_one(proposal.columns)
    mapped = {c.field for c in proposal.columns if c.field}
    proposal.unmapped_required = [
        f.name for f in entity.required_fields if f.name not in mapped
    ]
    return proposal


def unmapped_required(entity_key: str, mapping: dict[str, str | None]) -> list[str]:
    """Required fields still missing from an admin-edited mapping.

    Re-checked server-side on approval: the UI disables the button, but the
    button is not the guard — these columns are NOT NULL in Postgres.
    """
    entity = get_entity(entity_key)
    mapped = {v for v in mapping.values() if v}
    return [f.name for f in entity.required_fields if f.name not in mapped]
