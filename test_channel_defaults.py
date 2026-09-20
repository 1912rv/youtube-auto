import pytest

from script import refresh_youtube_credentials


def test_refresh_youtube_credentials_handles_revoked_refresh_token():
    class FakeCreds:
        expired = True
        refresh_token = "stale-refresh-token"
        valid = False

        def refresh(self, request):
            raise Exception("invalid_grant: Token has been expired or revoked.")

    with pytest.raises(RuntimeError, match="expired or revoked|YOUTUBE_TOKEN_JSON"):
        refresh_youtube_credentials(FakeCreds())
