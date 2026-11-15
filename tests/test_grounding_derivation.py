"""The one-step derivation rule in agent/grounding.py: what it may and may not bridge.

The guard accepts a figure that is one arithmetic step away from two evidence
numbers, so that revenue = gross_profit / gross_margin and market cap = shares
x price are not punished. Run-b of the eval (2026-11-10, case
nvda-effective-tax-rate-mdna) showed the cost of leaving that rule loose: a
draft saying "NVIDIA's net margin was 21.93%" -- a number no tool returned --
was discarded by the guard, and on the retry the same 21.93% went through,
because the retrieved MD&A text contained "a 29% increase in compensation" and
the hit's section label was "Item 7 - ...", and 29 - 7 = 22.0 is within the
0.5% tolerance of 21.93. A growth rate and an item number "derived" a margin.

Two things close that, and these tests pin both:

1. A derived figure has to match to the precision the answer wrote it with --
   half a unit in its last place -- not to a relative band. 22.0 is not 21.93.
2. The operands have to be figures: a numeric field of a tool result, or a
   number in text that carries a currency, percent, scale word, decimals or
   thousands grouping. A bare integer in prose or in a label ("Item 7", "page
   29", "Note 13") is never an operand.

The legitimate derivations the rule exists for are tested alongside, so a
tightening cannot quietly turn into a guard that refuses correct arithmetic.
"""

from __future__ import annotations

import warnings

import pytest

from agent.grounding import supported, unsupported_figures
from agent.orchestrator import Step

pytestmark = pytest.mark.safety


def _ok(tool: str, result, iteration: int = 1) -> Step:
    return Step(iteration=iteration, tool=tool, arguments={}, result=result,
                latency_ms=1.0, model_latency_ms=1.0, ok=True)


def _texts(figures) -> list[str]:
    return [f.text for f in figures]


def _flagged(number: str, figures) -> bool:
    """Whether a figure whose text contains `number` is among `figures`.

    Substring, because a candidate's text carries what it was wearing:
    "22.31 percentage points", "$5.48 trillion"."""
    return any(number in f.text for f in figures)


# ── the regression ────────────────────────────────────────────────────────────

MDNA_HIT = {
    "ok": True, "count": 1,
    "results": [{
        "ticker": "NVDA", "fiscal_period": "FY2026",
        "section": "Item 7 - Management's Discussion and Analysis of Financial "
                   "Condition and Results of Operations",
        "score": 0.7452,
        "excerpt": (
            "Research and development expenses for fiscal year 2026 were driven "
            "by a 29% increase in compensation and benefits. The net effect on "
            "our gross margin was an unfavorable impact of 2.6% and 2.3% in "
            "fiscal years 2026 and 2025, respectively."
        ),
    }],
}


def test_a_growth_rate_minus_an_item_number_does_not_support_a_margin():
    """The run-b failure, reduced to its two operands."""
    steps = [_ok("search_filings", MDNA_HIT)]
    answer = ("Based on the 10-K filing for NVIDIA's fiscal year 2026, the net "
              "margin was 21.93%. The filing mentions an unfavorable impact of "
              "2.6% on gross margin.")
    unsupported = unsupported_figures(answer, steps)
    assert _flagged("21.93", unsupported), (
        "21.93% traces to no tool result; 29% (a compensation growth rate) "
        "minus 7 (the item number in a section label) must not count as a "
        "derivation of it")
    assert not _flagged("2.6", unsupported), "2.6% is quoted from the excerpt and is fine"


def test_the_section_label_number_is_not_an_operand_even_at_exact_precision():
    """Even an answer that states exactly 22% is not supported by 29% - Item 7."""
    steps = [_ok("search_filings", MDNA_HIT)]
    assert _flagged("22", unsupported_figures("The net margin was 22%.", steps))


UNRELATED_PERCENTAGES = {
    "ok": True, "results": [{
        "section": "Item 7 - MD&A",
        "excerpt": "Compensation rose 29% while headcount grew 7% over the year.",
    }],
}


def test_known_limitation_a_round_figure_equal_to_two_unrelated_percentages_passes():
    """Pins a gap this change does NOT close, so it is visible rather than assumed.

    29% and 7% are both real figures, so both are operands, and 29 - 7 = 22
    exactly. An answer that invents "22%" claims only +/- 0.5, and the
    derivation lands inside that. The precision rule blocks "21.9%" and the
    operand rule blocks "Item 7", but a round invented figure that happens to
    equal the difference of two unrelated percentages goes through.

    Recorded in docs/KNOWN_LIMITATIONS.md. The planned fix is to require a
    derived figure's operands to come from calculate_ratio's own output rather
    than from retrieved text; when that lands, flip the first assertion and
    move this test up to the regression section.
    """
    steps = [_ok("search_filings", UNRELATED_PERCENTAGES)]
    assert not _flagged("22", unsupported_figures("The net margin was 22%.", steps)), (
        "if this now fails, the limitation is closed: update KNOWN_LIMITATIONS.md "
        "and turn this into a regression test")
    assert _flagged("21.9", unsupported_figures("The net margin was 21.9%.", steps))


# ── the legacy two-argument form cannot be used by accident ───────────────────

def test_the_two_argument_form_is_the_legacy_rule_and_warns():
    with pytest.warns(DeprecationWarning, match="legacy derivation rule"):
        assert supported(22.0, {29.0, 7.0}) is True   # the old rule, loud


def test_the_orchestrator_path_never_takes_the_legacy_rule():
    """unsupported_figures is what agent/orchestrator.py calls. Run it with the
    deprecation promoted to an error: if it ever falls back, this raises."""
    steps = [_ok("search_filings", MDNA_HIT),
             _ok("calculate_ratio", {"ok": True, "value": 0.556025,
                                     "inputs": {"revenue": 215938000000.0,
                                                "net_income": 120067000000.0}},
                 iteration=2)]
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        flagged = unsupported_figures(
            "Net margin was 55.60% on revenue of $215.9 billion; the filing cites a "
            "29% rise in compensation. The net margin was 21.93%.", steps)
    assert _flagged("21.93", flagged)
    assert not _flagged("55.60", flagged) and not _flagged("215.9", flagged)


# ── the derivations the rule exists for ───────────────────────────────────────

def test_revenue_derived_from_gross_profit_over_margin_is_supported():
    """The eval answer that derives revenue from a ratio result's inputs."""
    # Revenue itself is deliberately absent from the result: the only way to
    # reach $215.9 billion is gross_profit / value.
    steps = [_ok("calculate_ratio", {
        "ok": True, "ratio": "gross_margin", "value": 0.710681,
        "inputs": {"gross_profit": 153463000000.0},
        "formatted": "71.07%",
    })]
    answer = ("Gross profit was $153.46 billion at a 71.07% margin, so revenue "
              "was approximately $215.9 billion.")
    assert _texts(unsupported_figures(answer, steps)) == []


def test_market_cap_from_a_filing_share_count_and_a_tool_price_is_supported():
    """Operands from two different tools: a share count from a statement row and
    a numeric price field. Stated to three significant figures, as a model would.

    The share count is in absolute units, as the statement chunk's structured
    row carries it (`values_absolute`). The rendered text says "24,514" under
    an "in millions" note, and the guard deliberately does not rescale an
    operand by a million to make a derivation work -- that is the density
    problem DERIVED_RESCALINGS exists to avoid -- so the absolute figure has to
    be in the evidence for market cap to be derivable at all."""
    steps = [
        _ok("get_stock_price", {"ok": True, "ticker": "NVDA",
                                "last": {"date": "2026-08-07", "close": 223.71}}),
        _ok("search_filings", {"ok": True, "count": 1, "results": [{
            "section": "Income Statement", "ticker": "NVDA",
            "excerpt": "Diluted weighted average shares 24,514 24,804 24,940",
            "rows": [{"label": "Diluted", "kind": "share_count",
                      "values": [24514.0, 24804.0, 24940.0],
                      "values_absolute": [24514000000.0, 24804000000.0, 24940000000.0]}],
        }]}, iteration=2),
    ]
    answer = ("With 24,514 million diluted shares and a close of $223.71, the "
              "implied market capitalisation is about $5.48 trillion.")
    assert _texts(unsupported_figures(answer, steps)) == []


def test_the_difference_of_two_margins_is_supported():
    steps = [
        _ok("calculate_ratio", {"ok": True, "ticker": "NVDA", "value": 0.710681,
                                "formatted": "71.07%"}),
        _ok("calculate_ratio", {"ok": True, "ticker": "AAPL", "value": 0.487856,
                                "formatted": "48.79%"}),
    ]
    answer = ("NVIDIA's gross margin was 71.07%, while Apple's was 48.79%. The "
              "difference is 22.28 percentage points.")
    assert _texts(unsupported_figures(answer, steps)) == []


def test_a_derived_figure_stated_beyond_what_the_arithmetic_gives_is_not_supported():
    """71.07 - 48.79 = 22.28. An answer that writes 22.31 did not do that sum."""
    steps = [
        _ok("calculate_ratio", {"ok": True, "value": 0.710681, "formatted": "71.07%"}),
        _ok("calculate_ratio", {"ok": True, "value": 0.487856, "formatted": "48.79%"}),
    ]
    assert _flagged("22.31", unsupported_figures(
        "The difference is 22.31 percentage points.", steps))


def test_table_cells_under_a_units_note_count_as_operands_as_written_and_scaled():
    """A rendered statement states its units once, above the grid. A bare cell
    under "(In millions, except per share data)" is a figure in millions -- and
    a per-share cell in the same grid is not, so the cell counts both ways."""
    rendered = (
        "Consolidated Statements of Income\n"
        "(In millions, except per share data)\n\n"
        "Period                       Jan 25, 2026    Jan 26, 2025\n"
        "Interest income                        68              53\n"
        "Revenue                           215,938         130,497\n"
        "Net income per share - diluted       4.90            2.94\n"
    )
    steps = [
        _ok("search_filings", {"ok": True, "results": [{
            "section": "Income Statement", "excerpt": rendered}]}),
        _ok("get_stock_price", {"ok": True, "last": {"close": 223.71}}, iteration=2),
    ]
    # 68 + 53, read under the note, is $121 million.
    assert _texts(unsupported_figures(
        "Interest income over the two years totalled $121 million.", steps)) == []
    # 223.71 / 4.90, with the per-share cell left as written, is 45.7x.
    assert _texts(unsupported_figures(
        "At $223.71 the fiscal 2026 diluted EPS of $4.90 implies a P/E of 45.7x.",
        steps)) == []


def test_a_percent_column_counts_as_operands():
    """Cells wearing the column's % sign, as the MD&A segment table renders them."""
    steps = [_ok("search_filings", {"ok": True, "results": [{
        "section": "Item 7 - MD&A",
        "excerpt": ("Year Ended Jan 25, 2026 Jan 26, 2025 $Change %Change ($ in millions)"
                    "Compute & Networking $193,479 $116,193 $77,286 67 % Graphics "
                    "22,459 14,304 8,155 57 %"),
    }]})]
    assert _texts(unsupported_figures(
        "Compute & Networking grew 10 percentage points faster than Graphics "
        "(67% against 57%).", steps)) == []


def test_rounding_to_the_stated_precision_is_allowed():
    """24,514 million x 223.71 = $5,484,027 million. '$5.5 trillion' is that, rounded."""
    steps = [
        _ok("get_stock_price", {"ok": True, "last": {"close": 223.71}}),
        _ok("search_filings", {"ok": True, "results": [{
            "excerpt": "Diluted weighted average shares 24,514",
            "rows": [{"label": "Diluted", "values_absolute": [24514000000.0]}]}]}),
    ]
    assert _texts(unsupported_figures("That is roughly $5.5 trillion.", steps)) == []


# ── operands the answer itself rounded ────────────────────────────────────────
#
# A model computes on the numbers it writes. The tool says 223.7097, the answer
# says $223.71, and the product it states is the product of what it wrote. These
# pin that the guard accepts exactly that rounding and no more.

PRICE_STEP = _ok("get_stock_price", {"ok": True, "ticker": "NVDA",
                                     "last": {"date": "2026-08-07", "close": 223.7097}})
SHARES_STEP = _ok("search_filings", {"ok": True, "count": 1, "results": [{
    "section": "Income Statement", "ticker": "NVDA",
    "excerpt": "Diluted weighted average shares 24,514",
    "rows": [{"label": "Diluted", "kind": "share_count", "values": [24514.0],
              "values_absolute": [24514000000.0]}],
}]}, iteration=2)


def test_a_market_cap_on_the_price_as_the_answer_rounds_it_passes():
    """24,514 million x 223.71 = $5,484.03 billion. Against the exact close it is
    $5,484.02 billion, $7.3 million away -- outside half a unit of $0.01 billion."""
    answer = ("With 24,514 million diluted shares and a close of $223.71, the "
              "implied market capitalisation is $5,484.03 billion.")
    assert _texts(unsupported_figures(answer, [PRICE_STEP, SHARES_STEP])) == []


def test_the_same_market_cap_written_out_in_full_passes():
    answer = ("24,514,000,000 shares x $223.71 = $5,484,026,940,000 implied "
              "market capitalisation.")
    assert _texts(unsupported_figures(answer, [PRICE_STEP, SHARES_STEP])) == []


def test_without_the_rounded_price_in_the_answer_the_exact_close_is_used():
    """Nothing is rounded on the answer's behalf: it never wrote $223.71, so
    $5,484.03 billion is checked against 223.7097 and is out by $7.3 million."""
    answer = "The implied market capitalisation is $5,484.03 billion."
    assert _flagged("5,484.03", unsupported_figures(answer, [PRICE_STEP, SHARES_STEP]))


def test_an_invented_figure_just_outside_the_rounded_product_is_blocked():
    """$5,484.04 billion is $13.06 million from the rounded product, beyond the
    $5 million half-unit the answer's own precision allows."""
    answer = ("With 24,514 million diluted shares and a close of $223.71, the "
              "implied market capitalisation is $5,484.04 billion.")
    assert _flagged("5,484.04", unsupported_figures(answer, [PRICE_STEP, SHARES_STEP]))


def test_a_pe_from_a_rounded_price_and_a_rounded_eps_passes():
    """171.25 / 7.91 = 21.650. On the exact inputs, 171.2539 / 7.9123 = 21.644,
    which reads as 21.64 -- so only the answer's own roundings get it to 21.65."""
    steps = [_ok("calculate_ratio", {"ok": True, "ticker": "NVDA",
                                     "inputs": {"price": 171.2539, "trailing_eps": 7.9123}})]
    answer = ("At a price of $171.25 and trailing EPS of $7.91, the P/E is 21.65x.")
    assert _texts(unsupported_figures(answer, steps)) == []


def test_rounding_does_not_reopen_the_item_number_derivation():
    """The run-b case again, with the answer quoting the 29% it might have used.
    29% rounds a real operand; 7 is still not one, so 21.93% stays refused."""
    steps = [_ok("search_filings", MDNA_HIT)]
    answer = ("The filing cites a 29% increase in compensation. The net margin "
              "was 21.93%.")
    assert _flagged("21.93", unsupported_figures(answer, steps))


# ── typographic hyphens (hosted run 1, 2026-11-15) ────────────────────────────
#
# openai/gpt-oss-120b typesets "10-K" with U+2011, a non-breaking hyphen. The
# form rule in core/numerics.py matches the ASCII hyphen only, so the guard read
# "Form 10‑K" as the number 10 and discarded a correct answer.

RATIO = {"ok": True, "ratio": "gross_margin", "value": 0.710681, "formatted": "71.07%",
         "inputs": {"revenue": 215938000000.0, "gross_profit": 153463000000.0}}


@pytest.mark.parametrize("hyphen", ["-", "‐", "‑", "‒", "–", "−"])
def test_a_form_name_is_not_a_figure_whichever_hyphen_it_is_typeset_with(hyphen):
    steps = [_ok("calculate_ratio", RATIO)]
    answer = (f"NVIDIA reported a gross margin of 71.07% for fiscal year 2026, "
              f"as shown in its Form 10{hyphen}K.")
    assert _texts(unsupported_figures(answer, steps)) == []


def test_the_hosted_draft_that_was_discarded_no_longer_flags_the_form_name():
    """The draft from nvda-gross-margin-pct-mdna, hosted run 1, as written."""
    steps = [_ok("calculate_ratio", RATIO)]
    draft = ("NVIDIA reported a gross margin of **71.07%** for its fiscal year 2026, "
             "as shown in the MD&A section of its Form 10‑K. Gross profit of "
             "$153.463 billion divided by revenue of $215.938 billion yields "
             "a gross margin of 0.710681 (71.07%).\n\nFinal figure: 71.07%")
    assert _texts(unsupported_figures(draft, steps)) == []


def test_an_honest_refusal_naming_the_form_with_a_typographic_hyphen_passes():
    answer = "The filing search failed, so I have nothing to report from the 10‑K."
    assert unsupported_figures(answer, [_ok("calculate_ratio", RATIO)]) == []


def test_an_invented_figure_beside_a_typographic_hyphen_is_still_blocked():
    steps = [_ok("calculate_ratio", RATIO)]
    answer = "Per the Form 10‑K, NVIDIA's net‑margin was 21.93%."
    assert _flagged("21.93", unsupported_figures(answer, steps))


def test_a_number_after_a_typographic_hyphen_reads_as_it_does_after_a_plain_one():
    """Normalising makes the two spellings equal; it adds no new leniency."""
    steps = [_ok("calculate_ratio", RATIO)]
    for template in ("The margin was up{h}9.99% on the year.", "A top{h}40 customer list."):
        plain = _texts(unsupported_figures(template.format(h="-"), steps))
        typeset = _texts(unsupported_figures(template.format(h="‑"), steps))
        assert plain == typeset and plain, template


def test_an_em_dash_is_not_turned_into_a_minus_sign():
    steps = [_ok("calculate_ratio", RATIO)]
    assert unsupported_figures("Gross margin—71.07% for the year.", steps) == []


# ── derivations in a turn with more than DERIVATION_LIMIT operands ────────────
#
# Hosted run 1, aapl-gross-margin-pct-derived: the draft computed 195,201 /
# 416,161 = 46.9% from the statement. Both operands were in the tool results,
# but the turn held 126 operands against a limit of 120, so no derivation was
# checked and a correct figure was refused.

def _big_turn():
    """A statement table and a price series: well over DERIVATION_LIMIT operands."""
    from agent.grounding import DERIVATION_LIMIT, derivation_operands

    statement = {"ok": True, "results": [{
        "section": "Income Statement", "units": "USD millions",
        # As agent/tools.py returns a table: each row as written and in dollars.
        "rows": [
            {"label": label, "values": values,
             "values_absolute": [v * 1e6 for v in values]}
            for label, values in (
                ("Total net sales", [416161.0, 391035.0, 383285.0]),
                ("Gross margin", [195201.0, 180683.0, 169148.0]),
                ("Net income", [112010.0, 93736.0, 96995.0]),
            )]}]}
    prices = {"ok": True, "prices": [
        {"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": 201.37 + 0.73 * i}
        for i in range(200)]}
    steps = [_ok("search_filings", statement), _ok("get_stock_price", prices, iteration=2)]
    assert len(derivation_operands(steps)) > DERIVATION_LIMIT
    return steps


def test_a_derivation_from_operands_the_answer_states_passes_in_a_big_turn():
    answer = ("Apple's gross margin of $195,201 million on total net sales of "
              "$416,161 million is 46.9%.")
    assert _texts(unsupported_figures(answer, _big_turn())) == []


def test_the_working_shown_as_a_ratio_and_a_percentage_passes_in_a_big_turn():
    answer = ("Gross margin = 195,201 million / 416,161 million = 0.469, "
              "which is 46.9%.")
    assert _texts(unsupported_figures(answer, _big_turn())) == []


def test_an_invented_figure_in_a_big_turn_is_still_blocked():
    """The cap was not raised: more than 120 figures does not make 21.93% derivable."""
    steps = _big_turn()
    for answer in ("Apple's net margin was 21.93%.",
                   "On net sales of $416,161 million, Apple's net margin was 21.93%.",
                   "Gross margin of $195,201 million on net sales of $416,161 million "
                   "gives a margin of 52.3%."):
        assert _flagged("21.93" if "21.93" in answer else "52.3",
                        unsupported_figures(answer, steps)), answer


def test_a_derivation_whose_operands_the_answer_does_not_state_still_falls_under_the_cap():
    """46.9% is true, but the answer names neither operand, so only the wider
    set could support it, and the wider set is over the limit. Unchanged."""
    assert _flagged("46.9", unsupported_figures("Apple's gross margin was 46.9%.", _big_turn()))


def test_a_stated_operand_that_no_tool_returned_does_not_count():
    """Showing working with an invented input does not launder the result."""
    answer = ("Gross margin of $650,000 million on total net sales of "
              "$416,161 million is 156.2%.")
    unsupported = unsupported_figures(answer, _big_turn())
    assert _flagged("650,000", unsupported) and _flagged("156.2", unsupported)


def test_the_stated_operand_rule_changes_nothing_in_a_small_turn():
    """Under the cap the wider rule already searched these pairs."""
    steps = [_ok("calculate_ratio", RATIO)]
    assert unsupported_figures("Gross profit of $153.463 billion on revenue of "
                               "$215.938 billion is a 71.07% margin.", steps) == []
    assert _flagged("21.93", unsupported_figures(
        "Gross profit of $153.463 billion on revenue of $215.938 billion is a "
        "21.93% margin.", steps))


# ── stated operands must themselves be grounded ───────────────────────────────

INVENTED_AND_CONSISTENT = [
    "Revenue was $100 million, costs were $60 million, and profit was $40 million.",
    "Revenue of 100 less costs of 60 leaves a profit of 40.",
    "With revenue of $912.5 billion and costs of $547.5 billion, profit was $365.0 billion, "
    "a 40.0% margin.",
]


@pytest.mark.parametrize("answer", INVENTED_AND_CONSISTENT)
@pytest.mark.parametrize("turn", ["small", "big"])
def test_invented_figures_that_agree_with_each_other_are_all_flagged(answer, turn):
    """Three made-up numbers that add up prove nothing: 100 - 60 = 40 is true
    arithmetic over operands no tool returned. The answer's own figures count as
    operands only when each one traces to a tool result, so a consistent
    invention cannot vouch for itself -- in a small turn or in one with more
    than DERIVATION_LIMIT figures."""
    steps = [_ok("calculate_ratio", RATIO)] if turn == "small" else _big_turn()
    unsupported = unsupported_figures(answer, steps)
    stated = [f.text for f in __import__("agent.grounding", fromlist=["answer_figures"])
              .answer_figures(answer)]
    assert len(stated) >= 3
    assert _texts(unsupported) == stated, "every invented figure must be flagged"


def test_one_real_figure_does_not_vouch_for_two_invented_ones():
    """Revenue is real; the cost and the profit are invented to fit it."""
    answer = ("On total net sales of $416,161 million, costs were $400,000 million, "
              "leaving a profit of $16,161 million.")
    unsupported = unsupported_figures(answer, _big_turn())
    assert not _flagged("416,161", unsupported)
    assert _flagged("400,000", unsupported) and _flagged("16,161 million", unsupported)
