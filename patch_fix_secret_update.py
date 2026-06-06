"""
patch_fix_secret_update.py
--------------------------
Fixes KeyError 'key' in update_github_secret function.
"""

TARGET = r"C:\Users\Nat\Desktop\health-dashboard-repo\refresh_withings_token.py"

old = '''    key_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers
    )
    key_data = key_resp.json()
    public_key = key_data["key"]
    key_id     = key_data["key_id"]'''

new = '''    key_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers
    )
    key_data = key_resp.json()
    print(f"Public key response: {key_data}")
    public_key = key_data.get("key") or key_data.get("public_key", "")
    key_id     = key_data.get("key_id", "")
    if not public_key:
        print(f"Could not get public key: {key_data}")
        return'''

content = open(TARGET, encoding="utf-8").read()
if old in content:
    content = content.replace(old, new)
    open(TARGET, "w", encoding="utf-8").write(content)
    print("Patched successfully!")
else:
    print("Target not found.")
    for i, line in enumerate(content.split("\n")):
        if "key_data" in line or "public_key" in line:
            print(f"  {i}: {line}")
