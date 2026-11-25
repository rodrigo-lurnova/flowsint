import pytest
from pydantic import ValidationError

from flowsint_types import Repository, get_model_for_type
from flowsint_types.registry import get_type


def test_repository_registered():
    assert get_type("Repository", case_sensitive=True) is Repository
    assert get_model_for_type("repository") is Repository


def test_repository_primary_field():
    assert Repository.model_fields["url"].json_schema_extra == {"primary": True}


def test_repository_requires_url():
    with pytest.raises(ValidationError):
        Repository()


def test_repository_label_prefers_name():
    repo = Repository(url="https://github.com/acme/app.git", name="acme/app")
    assert repo.nodeLabel == "acme/app"


def test_repository_label_falls_back_to_url():
    repo = Repository(url="https://github.com/acme/app.git")
    assert repo.nodeLabel == "https://github.com/acme/app.git"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://github.com/x/y", True),
        ("https://gitlab.com/x/y", True),
        ("git@github.com:x/y.git", True),
        ("https://example.com/repo.git", True),
        ("just some text", False),
        ("", False),
    ],
)
def test_repository_detect(value, expected):
    assert Repository.detect(value) is expected
