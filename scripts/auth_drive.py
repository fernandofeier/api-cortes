"""
One-time script to generate OAuth2 refresh token for Google Drive.

Usage:
  1. Place your client_secret.json in credentials/
  2. Run: python scripts/auth_drive.py
  3. A browser will open for you to authorize
  4. The token will be saved to credentials/token.json
"""

import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRET = os.path.join("credentials", "client_secret.json")
TOKEN_OUTPUT = os.path.join("credentials", "token.json")


def main():
    if not os.path.exists(CLIENT_SECRET):
        print(f"ERROR: {CLIENT_SECRET} not found.")
        print("Download it from Google Cloud Console -> Credentials -> OAuth 2.0 Client IDs")
        return

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    with open(TOKEN_OUTPUT, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nToken saved to {TOKEN_OUTPUT}")
    print("You can now start the API. Drive uploads will use your account.")


if __name__ == "__main__":
    main()
