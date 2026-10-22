from traversal.source_redaction import redact_source_payload


def test_redacts_gt_tags_and_scenario_role_headers():
    payload = {
        "start_line": 4,
        "end_line": 8,
        "actual_end_line": 8,
        "source": (
            "// ===================== S1 culprit =====================\n"
            "void CResourceOwner::q3(Resource*& var) {\n"
            "    // @GT S1 culprit CResourceOwner::q3 DELETE var\n"
            "    delete var;\n"
        ),
        "numbered_source": "",
        "content_hash_matches": True,
        "computed_content_hash": "original-hash",
    }

    redacted, metadata = redact_source_payload(payload)

    assert "culprit" not in redacted["source"].lower()
    assert "@GT" not in redacted["source"]
    assert "delete var;" in redacted["source"]
    assert metadata["applied"] is True
    assert metadata["hash_verified_before_redaction"] is True
    assert redacted["computed_content_hash"] == "original-hash"

    numbered_lines = redacted["numbered_source"].splitlines()
    assert len(numbered_lines) == 4
    assert numbered_lines[0].startswith("4 |")
    assert numbered_lines[-1].startswith("7 |")
    assert metadata["redacted_line_numbers"] == [4, 6]


def test_redacts_inline_annotation_but_preserves_code_and_line_number():
    payload = {
        "start_line": 9,
        "actual_end_line": 9,
        "source": "    delete var; // @GT S1 culprit DELETE var",
        "content_hash_matches": True,
    }

    redacted, metadata = redact_source_payload(payload)

    assert redacted["source"].startswith("    delete var;")
    assert "@GT" not in redacted["source"]
    assert redacted["numbered_source"].startswith("9 |")
    assert metadata["redacted_line_numbers"] == [9]


def test_preserves_ordinary_comments_and_code():
    original = (
        "// T2 is faster\n"
        "SleepMs(100);\n"
        "// ordinary explanation about a resource"
    )
    payload = {
        "start_line": 5,
        "actual_end_line": 7,
        "source": original,
        "content_hash_matches": True,
    }

    redacted, metadata = redact_source_payload(payload)

    assert redacted["source"] == original
    assert metadata["applied"] is False
    assert metadata["redacted_line_numbers"] == []


def test_failed_original_hash_is_not_promoted_to_verified():
    payload = {
        "start_line": 10,
        "actual_end_line": 10,
        "source": "// @GT S1 culprit example",
        "content_hash_matches": False,
    }

    redacted, metadata = redact_source_payload(payload)

    assert redacted["content_hash_matches"] is False
    assert metadata["hash_verified_before_redaction"] is False
    assert metadata["applied"] is True
