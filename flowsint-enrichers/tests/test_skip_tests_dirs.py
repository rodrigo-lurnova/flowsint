import sys

import flowsint_enrichers
import flowsint_enrichers.registry as registry
from flowsint_enrichers.registry import load_all_enrichers


def test_tests_directories_are_not_imported(monkeypatch, tmp_path):
    """Test files mounted inside the package must not be imported as enrichers."""
    (tmp_path / "mounted" / "tests" / "nested").mkdir(parents=True)
    for path in (
        "mounted/real.py",
        "mounted/tests/conftest.py",
        "mounted/tests/test_x.py",
        "mounted/tests/nested/helper.py",
    ):
        (tmp_path / path).write_text("LOADED = True\n")
    monkeypatch.delenv("FLOWSINT_DISABLED_ENRICHERS", raising=False)
    monkeypatch.setattr(flowsint_enrichers, "__path__", [str(tmp_path)])
    monkeypatch.setattr(registry, "_enrichers_loaded", False)
    prefix = "flowsint_enrichers.mounted"
    try:
        load_all_enrichers()
        loaded = {m for m in sys.modules if m.startswith(prefix)}
    finally:
        for name in [m for m in sys.modules if m.startswith(prefix)]:
            del sys.modules[name]
        vars(flowsint_enrichers).pop("mounted", None)
    assert loaded == {prefix, f"{prefix}.real"}
