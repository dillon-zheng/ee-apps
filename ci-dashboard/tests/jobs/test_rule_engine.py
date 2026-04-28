from __future__ import annotations

from ci_dashboard.jobs.rule_engine import RuleEngine


def test_rule_engine_matches_network_infra_rule() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="dial tcp 10.10.10.10:443: i/o timeout",
        build={"job_name": "ghpr_check2", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is not None
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "NETWORK"
    assert classification.source == "rule:infra_network"


def test_rule_engine_matches_unit_test_rule_using_job_name_and_log_text() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="--- FAIL: TestDDLBasic (0.00s)\nFAIL\n",
        build={"job_name": "ghpr_unit_test", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is not None
    assert classification.l1_category == "UT"
    assert classification.l2_subcategory == "TEST_FAILURE"


def test_rule_engine_returns_none_on_rule_miss() -> None:
    engine = RuleEngine.from_file()

    classification = engine.classify(
        log_text="some brand new unknown failure shape",
        build={"job_name": "mystery_job", "url": "https://prow.tidb.net/job/x"},
    )

    assert classification is None
