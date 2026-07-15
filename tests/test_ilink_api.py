import sys
from pathlib import Path
import types
import importlib.util

# Add project root to sys.path
root = Path(__file__).parent.parent
sys.path.insert(0, str(root))

# Mock parent package for relative imports to work
mock_package = types.ModuleType("mock_plugin")
mock_package.__path__ = [str(root)]
sys.modules["mock_plugin"] = mock_package

# Load crypto first as it's needed by ilink_api
spec_crypto = importlib.util.spec_from_file_location(
    "mock_plugin.crypto",
    str(root / "crypto.py")
)
crypto = importlib.util.module_from_spec(spec_crypto)
sys.modules["mock_plugin.crypto"] = crypto
spec_crypto.loader.exec_module(crypto)

# Load ilink_api
spec_api = importlib.util.spec_from_file_location(
    "mock_plugin.ilink_api",
    str(root / "ilink_api.py")
)
ilink_api = importlib.util.module_from_spec(spec_api)
sys.modules["mock_plugin.ilink_api"] = ilink_api
spec_api.loader.exec_module(ilink_api)

_is_stale_session_ret = ilink_api._is_stale_session_ret

class TestIsStaleSessionRet:
    """Regression test for distinguishing stale-session ret=-2 from rate-limit ret=-2."""

    def test_ret_minus_2_with_unknown_error_is_stale(self):
        assert _is_stale_session_ret(-2, None, "unknown error") is True

    def test_errcode_minus_2_with_unknown_error_is_stale(self):
        assert _is_stale_session_ret(None, -2, "unknown error") is True

    def test_unknown_error_case_insensitive(self):
        assert _is_stale_session_ret(-2, None, "Unknown Error") is True

    def test_ret_minus_2_with_freq_limit_is_not_stale(self):
        # Genuine rate limit — must NOT be treated as stale session.
        assert _is_stale_session_ret(-2, None, "freq limit") is False
        assert _is_stale_session_ret(-2, None, "Freq Limit Exceeded") is False

    def test_ret_minus_2_with_no_errmsg_is_stale(self):
        # Relaxed logic: errmsg is None or empty should be treated as stale session
        assert _is_stale_session_ret(-2, None, None) is True
        assert _is_stale_session_ret(-2, None, "") is True

    def test_errcode_minus_14_is_not_matched_here(self):
        # -14 is handled by the separate SESSION_EXPIRED_ERRCODE path; the
        # helper only disambiguates -2 from a genuine rate limit.
        assert _is_stale_session_ret(-14, None, "session expired") is False

    def test_success_codes_are_not_stale(self):
        assert _is_stale_session_ret(0, 0, "") is False
        assert _is_stale_session_ret(None, None, "unknown error") is False
