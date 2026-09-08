"""Refresh or recreate a Garmin Connect token for GitHub Actions.

The script deliberately uses garminconnect's low-level client. The high-level
``Garmin.login(tokenstore=...)`` path also loads the social profile, which is
not required by this project and can fail with a 401 even when the activity
API token is usable.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

from garminconnect import Garmin


def _client(is_cn: bool) -> Garmin:
    return Garmin(
        is_cn=is_cn,
        # The activity API is validated explicitly below. Avoid the optional
        # social-profile bootstrap performed by the wrapper's normal login.
        verify_login=False,
    )


def _validate_activity_token(client: Garmin) -> None:
    """Make sure the token works against the API used by this repository."""

    client.get_activities(0, 1, activitytype="running")


def _try_refresh(token_json: str, is_cn: bool) -> str | None:
    if not token_json.strip():
        return None

    client = _client(is_cn)
    try:
        client.client.loads(token_json.strip())
        # Force a refresh so a newly rotated refresh token is persisted back to
        # GitHub instead of remaining only in this ephemeral runner.
        client.client._refresh_di_token()
        _validate_activity_token(client)
        return client.client.dumps()
    except Exception as error:
        print(
            f"Existing Garmin token could not be refreshed ({type(error).__name__}); "
            "trying a fresh credential login.",
            file=sys.stderr,
        )
        return None


def _login(email: str, password: str, is_cn: bool) -> str:
    client = _client(is_cn)
    # MFA is intentionally not handled here: this workflow is designed for an
    # account that only requires the password, as configured by the owner.
    client.client.login(email, password)
    _validate_activity_token(client)
    return client.client.dumps()


def _write_token(path: str, token_json: str) -> None:
    token_path = Path(path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token_json, encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the current Garmin token or create a replacement."
    )
    parser.add_argument(
        "--token-json",
        default=os.getenv("GARMIN_CN_TOKEN", ""),
        help="Current token JSON; defaults to GARMIN_CN_TOKEN.",
    )
    parser.add_argument(
        "--email",
        default=os.getenv("GARMIN_EMAIL", ""),
        help="Garmin account email; defaults to GARMIN_EMAIL.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Owner-only file to receive the replacement token JSON.",
    )
    parser.add_argument("--is-cn", action="store_true", help="Use Garmin China.")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when GARMIN_PASSWORD is missing.",
    )
    options = parser.parse_args()

    token_json = _try_refresh(options.token_json, options.is_cn)
    if token_json is None:
        email = options.email.strip()
        password = os.getenv("GARMIN_PASSWORD", "")
        if not email:
            if options.non_interactive:
                raise RuntimeError("GARMIN_EMAIL is required in non-interactive mode")
            email = input("Garmin email: ").strip()
        if not password:
            if options.non_interactive:
                raise RuntimeError(
                    "GARMIN_PASSWORD is required in non-interactive mode"
                )
            password = getpass.getpass("Garmin password: ")
        try:
            token_json = _login(email, password, options.is_cn)
        finally:
            password = ""

    _write_token(options.output, token_json)
    print("Garmin token refreshed and validated.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"Garmin token recovery failed: {error}", file=sys.stderr)
        raise SystemExit(1)
