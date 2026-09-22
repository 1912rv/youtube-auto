import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET_FILE = "client_secret.json"
OUTPUT_TOKEN_FILE = "youtube-token.json"


def main():
    if not Path(CLIENT_SECRET_FILE).exists():
        print(f"❌ Error: '{CLIENT_SECRET_FILE}' not found.")
        return

    print("🔐 Opening browser for fresh Google OAuth authentication...")
    flow = InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRET_FILE, SCOPES
    )
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }

    # Save to youtube-token.json and updated_token.json
    with open(OUTPUT_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)

    with open("updated_token.json", "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)

    print("✅ Successfully generated fresh token files!")


if __name__ == "__main__":
    main()
    