from flowsint_enrichers import ENRICHER_REGISTRY
from flowsint_enrichers.email.to_holehe import EmailToHoleheEnricher
from flowsint_types.social_account import SocialAccount


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
def test_email_to_holehe_is_registered():
    enricher = ENRICHER_REGISTRY.get_enricher("email_to_holehe", "123", "123")
    assert enricher.name() == "email_to_holehe"


def test_email_to_holehe_metadata():
    assert EmailToHoleheEnricher.category() == "Email"
    assert EmailToHoleheEnricher.key() == "email"
    assert EmailToHoleheEnricher.input_schema()["type"] == "Email"
    assert EmailToHoleheEnricher.output_schema()["type"] == "SocialAccount"


# ---------------------------------------------------------------------------
# _parse_holehe_output (no network)
# ---------------------------------------------------------------------------
def test_parse_keeps_only_existing_accounts():
    out = [
        {"name": "twitter", "domain": "twitter.com", "exists": True},
        {"name": "github", "domain": "github.com", "exists": False},
        {"name": "spotify", "domain": "spotify.com", "exists": None},
    ]
    accounts = EmailToHoleheEnricher._parse_holehe_output("bob@example.com", out)
    assert [a.platform for a in accounts] == ["twitter.com"]
    assert all(isinstance(a, SocialAccount) for a in accounts)


def test_parse_builds_unique_id_and_label():
    out = [{"name": "twitter", "domain": "twitter.com", "exists": True}]
    (account,) = EmailToHoleheEnricher._parse_holehe_output("bob@example.com", out)
    # SocialAccount derives its id from username(email)+platform, so two
    # different emails on the same service stay distinct nodes.
    assert account.id == "bob@example.com@twitter.com"
    assert account.nodeLabel == "bob@example.com @ twitter.com"
    assert account.display_name == "twitter"
    assert account.associated_emails == ["bob@example.com"]


def test_parse_surfaces_recovery_hints_in_bio():
    out = [
        {
            "name": "instagram",
            "domain": "instagram.com",
            "exists": True,
            "emailrecovery": "b****@gmail.com",
            "phoneNumber": "0*****78",
        }
    ]
    (account,) = EmailToHoleheEnricher._parse_holehe_output("bob@example.com", out)
    assert "recovery email: b****@gmail.com" in account.bio
    assert "recovery phone: 0*****78" in account.bio


def test_parse_falls_back_to_name_when_no_domain():
    out = [{"name": "someservice", "exists": True}]
    (account,) = EmailToHoleheEnricher._parse_holehe_output("bob@example.com", out)
    assert account.platform == "someservice"


def test_parse_handles_empty_and_malformed():
    assert EmailToHoleheEnricher._parse_holehe_output("bob@example.com", []) == []
    out = ["not-a-dict", {"exists": True}, {"name": "x", "exists": True}]
    accounts = EmailToHoleheEnricher._parse_holehe_output("bob@example.com", out)
    # the bare-string entry and the dict missing a name are dropped
    assert [a.display_name for a in accounts] == ["x"]
