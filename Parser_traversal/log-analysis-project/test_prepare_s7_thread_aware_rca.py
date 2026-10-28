from __future__ import annotations

import pytest

from prepare_s7_thread_aware_rca import (
    ERROR_MESSAGE,
    extract_logged_function,
    select_representative_line,
)


def make_line(function: str) -> str:
    return (
        "20-10-2026_19:01:53.196 | 417.975668 | 1088 | ERROR | "
        f"{function} {ERROR_MESSAGE}"
    )


def test_extract_logged_function_from_raw_record() -> None:
    assert extract_logged_function(make_line("CS7Workers::runT3")) == "CS7Workers::runT3"


def test_representative_selection_chooses_anchor_not_first_message_match() -> None:
    rows = [
        (15, make_line("CS7Workers::runT3")),
        (19, make_line("CS7Workers::runT1")),
        (29, make_line("CS7Workers::runT2")),
    ]
    line_number, raw_line = select_representative_line(rows, "CS7Workers::runT2")

    assert line_number == 29
    assert extract_logged_function(raw_line) == "CS7Workers::runT2"


def test_representative_selection_fails_when_anchor_has_no_matching_event() -> None:
    rows = [(15, make_line("CS7Workers::runT3"))]
    with pytest.raises(ValueError, match="No error log record names preferred representative function"):
        select_representative_line(rows, "CS7Workers::runT2")


def test_parsed_representative_function_must_match_raw_line() -> None:
    line_number, raw_line = select_representative_line(
        [(15, make_line("CS7Workers::runT3")), (29, make_line("CS7Workers::runT2"))],
        "CS7Workers::runT2",
    )
    representative_error = {
        "qualified_function": extract_logged_function(raw_line),
        "raw_line": raw_line,
        "log_line_number": line_number,
    }
    assert representative_error["qualified_function"] in representative_error["raw_line"]
    assert representative_error["qualified_function"] == "CS7Workers::runT2"
