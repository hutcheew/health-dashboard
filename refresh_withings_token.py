"""
refresh_withings_token.py
--------------------------
Refreshes Withings access token and updates GitHub secret using PAT.
"""

import os
import json
import requests
from base64 import b64encode

TOKEN_FILE = os.path.expanduser("~/.withings/withings_tokens.json")

def refresh_token():
    data = json.load(open(TOKEN_FILE))
    resp = requests.post(
        "https://wbsapi.withings.net/v2/oauth2",
        data={
            "action":        "requesttoken",
            "grant_type":    "refresh_token",
            "client_id":     os.environ["WITHINGS_CLIENT_ID"],
            "client_secret": os.environ["WITHINGS_CLIENT_SECRET"],
            "refresh_token": data["refresh_token"],
        }
    )
    result = resp.json()
    if result.get("status") != 0:
        print(f"Token refresh failed: {result}")
        return None
    body = result["body"]
    new_tokens = {
        "access_token":  body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_in":    body["expires_in"],
        "token_type":    body["token_type"],
        "userid":        body["userid"],
    }
    json.dump(new_tokens, open(TOKEN_FILE, "w"), indent=2)
    print(f"Token refreshed. New access token: {body['access_token'][:10]}...")
    return new_tokens

def update_github_secret(token_json):
    pat = os.environ.get("GH_PAT", "")
    repo = "hutcheew/health-dashboard"

    if not pat:
        print("No GH_PAT — skipping secret update")
        return

    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Get repo public key
    key_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers
    )
    print(f"Public key status: {key_resp.status_code}")
    key_data = key_resp.json()
    print(f"Public key data: {key_data}")

    public_key_b64 = key_data.get("key", "")
    key_id = key_data.get("key_id", "")

    if not public_key_b64:
        print("Could not get public key — check GH_PAT has repo secrets permission")
        return

    # Encrypt with PyNaCl
    from nacl import encoding, public
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder)
    sealed = public.SealedBox(pk)
    encrypted = b64encode(sealed.encrypt(json.dumps(token_json).encode())).decode()

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
