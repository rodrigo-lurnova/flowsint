from unittest.mock import MagicMock, patch

from tools import dockertool
from tools.security.trufflehog import TrufflehogTool


def test_output_does_not_depend_on_daemon_log_driver():
    client = MagicMock()
    client.containers.run.return_value = b"out"
    with patch.object(dockertool, "from_env", return_value=client):
        assert TrufflehogTool().launch("cmd") == "out"
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["log_config"] == {"Type": "json-file"}
