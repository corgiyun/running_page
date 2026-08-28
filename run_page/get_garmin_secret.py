"""Create a Garmin Connect token JSON for local use or GitHub Actions."""

import argparse
import getpass
import sys

from garminconnect import Garmin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("email", nargs="?", help="Garmin account email")
    parser.add_argument(
        "--is-cn", dest="is_cn", action="store_true", help="use Garmin China"
    )
    options = parser.parse_args()

    email = options.email or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    client = Garmin(
        email=email,
        password=password,
        is_cn=options.is_cn,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    try:
        client.login()
        # The token contains a refresh credential. Copy it only to GitHub
        # Secrets or another owner-only secure store.
        print(client.client.dumps())
    finally:
        password = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
