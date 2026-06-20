# Setting up automatic check-in sync

This lets the "Save today" button on your dashboard commit directly to
`checkins.json` in your repo, without you ever exporting/pasting/pushing
manually. The real GitHub write-token never appears in your public
dashboard — it lives only inside Cloudflare, encrypted.

Takes about 15-20 minutes, one time only.

---

## 1. Create a scoped GitHub token

Use a **fine-grained personal access token**, locked to just this repo,
with only the permission it actually needs. This way, even in the
unlikely event it ever leaked, the damage is capped at "someone can edit
files in this one repo" — not your whole GitHub account.

1. GitHub → Settings → Developer settings → Personal access tokens →
   **Fine-grained tokens** → Generate new token
2. **Repository access**: "Only select repositories" → choose
   `health-dashboard`
3. **Permissions**: Repository permissions → **Contents** → Read and write.
   Leave everything else as "No access."
4. Generate, copy the token somewhere safe — you'll paste it into
   Cloudflare in step 3, then never need it again.

## 2. Create a free Cloudflare account + Worker

1. Sign up at https://dash.cloudflare.com (free tier is plenty for this)
2. Workers & Pages → Create → **Create Worker**
3. Give it a name, e.g. `checkin-sync` — note the URL it gives you,
   something like `https://checkin-sync.yoursubdomain.workers.dev`
4. Click **Edit code**, delete the placeholder content, paste in the
   full contents of `checkin-sync-worker.js`, click **Deploy**

## 3. Set the Worker's secrets

Still in the Worker's dashboard page → **Settings** → **Variables and
Secrets** → add these (use "Encrypt" for the token and secret):

| Name | Value | Encrypt? |
|---|---|---|
| `GITHUB_TOKEN` | the token from step 1 | Yes |
| `SYNC_SECRET` | a random string you make up (e.g. generate one at https://1password.com/password-generator, 20+ chars) | Yes |
| `GITHUB_OWNER` | your GitHub username, e.g. `hutcheew` | No |
| `GITHUB_REPO` | `health-dashboard` | No |
| `GITHUB_BRANCH` | `main` | No |
| `ALLOWED_ORIGIN` | your GitHub Pages URL, e.g. `https://hutcheew.github.io` | No |

Save and redeploy if prompted.

**Write down `SYNC_SECRET` somewhere — you'll need to type it into the
dashboard once, in step 5.**

## 4. Put the Worker URL in the dashboard

In `health_dashboard.py`, find the line:

```js
const SYNC_WORKER_URL = ''; // <-- paste your Cloudflare Worker URL here
```

and fill in the URL from step 2 (e.g.
`https://checkin-sync.yoursubdomain.workers.dev`). This URL itself isn't
sensitive — it's just an endpoint, useless without the secret.

Commit and push so it's live on the dashboard.

## 5. One-time setup on each device/browser you use

Open the dashboard, fill in a check-in, hit **Save today**. The first
time, it'll prompt you to paste in the `SYNC_SECRET` from step 3 — this
gets stored in that browser's `localStorage` only (never committed,
never visible in page source). You'll need to do this once per
device/browser you check in from (phone, laptop, etc.) — it's a
deliberate tradeoff so the secret never ends up in the public repo.

After that, every "Save today" automatically commits straight to
`checkins.json` — no export, no manual git push.

---

## How it works day to day

1. You fill in the sliders, hit Save.
2. Saved instantly to `localStorage` (so it still works offline / shows
   in the trend table immediately).
3. Sent to the Worker, which checks your `SYNC_SECRET`, validates the
   data (rejects anything that's not a clean 0-10 integer or more than
   3 days old/in the future), then commits it straight to
   `checkins.json` in your repo using its own private GitHub token.
4. Next time `health_dashboard.py` runs (via the existing GitHub
   Action), it picks up the new check-in automatically.

The **Export history** button still exists as a manual backup/escape
hatch if the Worker is ever down or you want a local copy.

## Security notes

- The GitHub token never appears in any code your browser downloads —
  it only exists inside Cloudflare's encrypted secret storage.
- `SYNC_SECRET` *does* end up in your browser's `localStorage`, which
  isn't as strong as a server-only secret — but it's never in the
  committed/public `index.html`, so a casual visitor to your dashboard
  can't extract it just by viewing source.
- Worst case if `SYNC_SECRET` did leak: someone could write fake
  check-in entries (capped to within 3 days of today by the Worker's
  validation) — annoying, recoverable from git history, not a takeover
  of your repo or dashboard code.
- If you ever want to rotate `SYNC_SECRET`, just change it in Cloudflare
  and re-enter it in your browser(s) next time you check in.
