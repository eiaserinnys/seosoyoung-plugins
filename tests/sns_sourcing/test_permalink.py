from seosoyoung_plugins.sns_sourcing.permalink import build_slack_permalink


def test_builds_deterministic_permalink():
    assert build_slack_permalink(
        "thelinegames.slack.com",
        "C123",
        "1700000000.123456",
    ) == "https://thelinegames.slack.com/archives/C123/p1700000000123456"


def test_normalizes_https_domain():
    assert build_slack_permalink(
        "https://thelinegames.slack.com/",
        "C123",
        "1700000000.123456",
    ) == "https://thelinegames.slack.com/archives/C123/p1700000000123456"

