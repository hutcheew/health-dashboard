/**
 * checkin-sync-worker.js
 * ------------------------------------------------------------------
 * Securely commits Achilles check-in entries to checkins.json and run
 * journal reflections to journal.json in the health-dashboard GitHub
 * repo, without ever exposing a GitHub write token in the public
 * dashboard's page source.
 *
 * Flow:
 *   Browser (dashboard) --POST(payload, SYNC_SECRET)--> this Worker
 *   Worker validates SYNC_SECRET, then reads/writes checkins.json or
 *   journal.json via the GitHub Contents API using a GitHub token that
 *   only ever lives as an encrypted Cloudflare secret.
 *
 * Required Cloudflare secrets/vars (set via `wrangler secret put` or
 * the Cloudflare dashboard — see SETUP.md):
 *   GITHUB_TOKEN   - fine-grained PAT, scoped to ONLY this repo,
 *                    "Contents: Read and write" permission, nothing else
 *   SYNC_SECRET    - a random string only you know; the dashboard asks
 *                    for this once and stores it in the browser's
 *                    localStorage (never committed, never in page source)
 *   GITHUB_OWNER   - e.g. "hutcheew"
 *   GITHUB_REPO    - e.g. "health-dashboard"
 *   GITHUB_BRANCH  - e.g. "main" (optional, defaults to "main")
 *   ALLOWED_ORIGIN - e.g. "https://hutcheew.github.io" (your Pages origin,
 *                    used for CORS — restrict this so other sites can't
 *                    call your Worker from a user's browser)
 */

const FILES = {
  checkin: "checkins.json",
  journal: "journal.json",
};

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(body, status, env) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(env) },
  });
}

function isValidDate(d) {
  return typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d);
}

function isValidScore(v) {
  return v === undefined || v === null || (Number.isInteger(v) && v >= 0 && v <= 10);
}

function isWithinDays(dateStr, days) {
  const today = new Date();
  const entryDate = new Date(dateStr + "T00:00:00Z");
  const diffDays = Math.abs((today - entryDate) / 86400000);
  return diffDays <= days;
}

function validateEntry(body) {
  if (!isValidDate(body.date)) return "Invalid or missing date (expected YYYY-MM-DD)";

  if (body.kind === "journal") {
    if (!(body.activity_id || body.date)) return "journal needs activity_id or date";
    if (!isValidScore(body.rpe)) return "rpe must be an integer 0-10";
    if (!isValidScore(body.pain)) return "pain must be an integer 0-10";
    if (body.tags !== undefined && !Array.isArray(body.tags)) return "tags must be an array";
    return null;
  }

  if (!isValidScore(body.stiffness, true)) return "stiffness must be an integer 0-10";
  if (!isValidScore(body.first_steps_pain, true)) return "first_steps_pain must be an integer 0-10";
  if (!isValidScore(body.post_run_pain, true)) return "post_run_pain must be an integer 0-10";
  if (typeof body.calf_raises !== "boolean") return "calf_raises must be true/false";

  // Reject dates too far in the past/future — limits how much damage a
  // leaked SYNC_SECRET could do (can't backfill/poison arbitrary history).
  if (!isWithinDays(body.date, 3)) return "date must be within 3 days of today";

  return null;
}

function b64EncodeUnicode(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

function b64DecodeUnicode(str) {
  return decodeURIComponent(escape(atob(str)));
}

async function githubRequest(env, method, body, filePath) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/contents/${filePath}`;
  return fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "checkin-sync-worker",
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(env) });
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "Method not allowed" }, 405, env);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return jsonResponse({ error: "Invalid JSON body" }, 400, env);
    }

    if (!env.SYNC_SECRET || payload.secret !== env.SYNC_SECRET) {
      return jsonResponse({ error: "Unauthorized" }, 401, env);
    }

    const kind = payload.kind === "journal" ? "journal" : "checkin";
    const filePath = FILES[kind];

    const validationError = validateEntry(payload);
    if (validationError) {
      return jsonResponse({ error: validationError }, 400, env);
    }

    const savedAt = new Date().toISOString();
    let entry;
    if (kind === "journal") {
      entry = {
        kind: "journal",
        activity_id: payload.activity_id || null,
        date: payload.date,
        rpe: payload.rpe,
        feel: payload.feel || "",
        pain: payload.pain,
        worked: payload.worked || "",
        change: payload.change || "",
        tags: Array.isArray(payload.tags) ? payload.tags : [],
        source: "browser",
        saved_at: savedAt,
      };
    } else {
      entry = {
        date: payload.date,
        stiffness: payload.stiffness,
        first_steps_pain: payload.first_steps_pain,
        post_run_pain: payload.post_run_pain,
        calf_raises: payload.calf_raises,
        source: "browser",
        saved_at: savedAt,
      };
    }

    // 1. Fetch current file (need its sha to update it)
    let currentList = [];
    let sha = undefined;
    const getResp = await githubRequest(env, "GET", undefined, filePath);
    if (getResp.status === 200) {
      const data = await getResp.json();
      sha = data.sha;
      try {
        currentList = JSON.parse(b64DecodeUnicode(data.content.replace(/\n/g, "")));
        if (!Array.isArray(currentList)) currentList = [];
      } catch {
        currentList = [];
      }
    } else if (getResp.status !== 404) {
      const errText = await getResp.text();
      return jsonResponse({ error: "GitHub read failed", detail: errText }, 502, env);
    }

    // 2. Merge — overwrite the entry matching the same key (date for
    //    checkins, activity_id-or-date for journal), keep everything else.
    const key = kind === "journal" ? (entry.activity_id || entry.date) : entry.date;
    const collides = (c) =>
      kind === "journal"
        ? (c.activity_id && entry.activity_id && String(c.activity_id) === String(entry.activity_id)) ||
          (c.date && c.date === entry.date)
        : c.date === key;
    currentList = currentList.filter((c) => !collides(c));
    currentList.push(entry);
    currentList.sort((a, b) => (a.date < b.date ? -1 : 1));

    // 3. Commit the updated file
    const newContent = b64EncodeUnicode(JSON.stringify(currentList, null, 2));
    const putResp = await githubRequest(env, "PUT", {
      message: `${kind === "journal" ? "Journal update" : "Check-in update"} ${entry.date}`,
      content: newContent,
      branch: env.GITHUB_BRANCH || "main",
      ...(sha ? { sha } : {}),
    }, filePath);

    if (!putResp.ok) {
      const errText = await putResp.text();
      return jsonResponse({ error: "GitHub write failed", detail: errText }, 502, env);
    }

    return jsonResponse({ ok: true, kind, date: entry.date, total_entries: currentList.length }, 200, env);
  },
};
