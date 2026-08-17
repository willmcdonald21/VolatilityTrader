from __future__ import annotations

from warrior_bot.scanner.catalyst import classify_headlines


def test_classifies_earnings_headline():
    result = classify_headlines(["XYZ Reports Q2 Earnings, Beats Estimates"])
    assert result.category == "earnings"
    assert result.has_catalyst is True


def test_classifies_fda_headline():
    result = classify_headlines(["XYZ Announces FDA Clearance for New Device"])
    assert result.category == "fda"


def test_classifies_merger_headline():
    result = classify_headlines(["XYZ to be Acquired by ABC Corp in Definitive Agreement"])
    assert result.category == "merger"


def test_classifies_contract_headline():
    result = classify_headlines(["XYZ Wins Contract Award from Department of Defense"])
    assert result.category == "contract"


def test_classifies_insider_buying_headline():
    result = classify_headlines(["XYZ Director Buys 50,000 Shares in Open Market"])
    assert result.category == "insider_buying"


def test_classifies_upgrade_headline():
    result = classify_headlines(["Analyst Upgraded XYZ, Price Target Raised to $15"])
    assert result.category == "upgrade"


def test_no_match_returns_empty_catalyst_info():
    result = classify_headlines(["XYZ Stock Moves in Active Trading"])
    assert result.category is None
    assert result.headline is None
    assert result.has_catalyst is False


def test_empty_headline_list_returns_empty_catalyst_info():
    result = classify_headlines([])
    assert result.has_catalyst is False


def test_first_matching_headline_wins_in_priority_order():
    # fda comes before earnings in CATALYST_KEYWORDS -- confirms scan order,
    # not just presence of a match
    result = classify_headlines(["XYZ Reports Earnings", "XYZ Gets FDA Approval"])
    assert result.category == "earnings"  # first headline in the list wins, not category priority
    assert result.headline == "XYZ Reports Earnings"
