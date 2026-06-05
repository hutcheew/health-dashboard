"""
refresh_withings_token.py
--------------------------
Refreshes Withings access token using refresh token,
then updates the WITHINGS_TOKENS GitHub secret automatically.

Called by GitHub Actions workflow after each run.
"""

import os
import json
import base64
import requests
from datetime import datetime

TOKEN_FILE = os.path.expanduser("~/.withings/withings_tokens.json")

def refresh_token():
    data = json.load(open(TOKEN_FILE))
    client_id     = os.environ["WITHINGS_CLIENT_ID"]
    client_secret = os.environ["WITHINGS_CLIENT_SECRET"]
    refresh_tok   = data["refresh_token"]

    resp = requests.post(
        "https://wbsapi.withings.net/v2/oauth2",
        data={
            "action":        "requesttoken",
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_tok,
        }
    )
    result = resp.json()
    if result.get("status") != 0:
        print(f"Token refresh failed: {result}")
        return False

    body = result["body"]
    new_tokens = {
        "access_token":  body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_in":    body["expires_in"],
        "token_type":    body["token_type"],
        "userid":        body["userid"],
    }

    # Save locally
    json.dump(new_tokens, open(TOKEN_FILE, "w"), indent=2)
    print(f"Token refreshed. Expires in {body['expires_in']}s")
    return new_tokens

def update_github_secret(token_json):
    """Update WITHINGS_TOKENS secret in GitHub repo."""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    repo         = os.environ.get("GITHUB_REPOSITORY", "hutcheew/health-dashboard")

    if not github_token:
        print("No GITHUB_TOKEN — skipping secret update")
        return

    # Get repo public key for encryption
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    key_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers
    )
    key_data = key_resp.json()
    public_key = key_data["key"]
    key_id     = key_data["key_id"]

    # Encrypt secret value using libsodium
    from base64 import b64encode
    try:
        from nacl import encoding, public
        sealed_box = public.SealedBox(public.PublicKey(public_key.encode(), encoding.Base64Encoder))
        encrypted  = b64encode(sealed_box.encrypt(json.dumps(token_json).encode())).decode()
    except ImportError:
        print("PyNaCl not installed — installing...")
        os.system("pip install PyNaCl -q")
        from nacl import encoding, public
        sealed_box = public.SealedBox(public.PublicKey(public_key.encode(), encoding.Base64Encoder))
        encrypted  = b64encode(sealed_box.encrypt(json.dumps(token_json).encode())).decode()

    # Update secret
    update_resp = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/WITHINGS_TOKENS",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_id}
    )
    if update_resp.status_code in (201, 204):
        print("GitHub secret WITHINGS_TOKENS updated successfully")
    else:
        print(f"Secret update failed: {update_resp.status_code} {update_resp.text}")

if __name__ == "__main__":
    print("Refreshing Withings token...")
    new_tokens = refresh_token()
    if new_tokens:
        print("Updating GitHub secret...")
        update_github_secret(new_tokens)
