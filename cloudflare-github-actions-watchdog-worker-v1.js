// Acton Bridge External GitHub Actions Watchdog Worker v1
// Deploy as a separate Cloudflare Worker with a Cron Trigger, e.g. every 5 or 10 minutes.
// Required Cloudflare secrets/vars:
//   GITHUB_TOKEN        fine-grained PAT with Actions read/write access to the repo
//   GITHUB_OWNER        e.g. MorganWilliams
//   GITHUB_REPO         e.g. Acton-Bridge-Rail-Count
//   GITHUB_BRANCH       optional, default: main
//   WATCHDOG_SECRET     optional, for manual browser trigger /run?token=...

const ACTIVE_STATUSES = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);

const DEFAULT_WORKFLOWS = [
  // filename, stale minutes
  ["collector.yml", 35],
  ["td_collector.yml", 30],
  ["public_snapshot_builder.yml", 20],
  ["route_backfill_loop.yml", 75],
  ["vstp_collector.yml", 60],
  ["schedule_loader.yml", 720],
  ["pathing_power_injector_v6.yml", 180]
];

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff"
    }
  });
}

function envValue(env, key, fallback = "") {
  return String(env[key] || fallback).trim();
}

function workflowList(env) {
  const raw = env.WATCHED_WORKFLOWS;
  if (!raw) return DEFAULT_WORKFLOWS.map(([file, minutes]) => ({ file, minutes }));
  return String(raw)
    .split(/\n|,/)
    .map(s => s.trim())
    .filter(Boolean)
    .map(line => {
      const [file, minutesRaw] = line.split(":").map(x => x.trim());
      return { file, minutes: Number(minutesRaw || 60) || 60 };
    });
}

async function gh(env, path, init = {}) {
  const owner = envValue(env, "GITHUB_OWNER");
  const repo = envValue(env, "GITHUB_REPO");
  const token = envValue(env, "GITHUB_TOKEN");
  if (!owner || !repo || !token) throw new Error("Missing GITHUB_OWNER, GITHUB_REPO or GITHUB_TOKEN");

  const url = `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      "accept": "application/vnd.github+json",
      "authorization": `Bearer ${token}`,
      "x-github-api-version": "2022-11-28",
      "user-agent": "acton-bridge-cloudflare-actions-watchdog",
      ...(init.headers || {})
    }
  });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const msg = typeof body === "string" ? body : JSON.stringify(body);
    throw new Error(`GitHub ${res.status} ${res.statusText}: ${msg}`);
  }
  return body;
}

function minutesSince(dateString) {
  if (!dateString) return Infinity;
  return (Date.now() - new Date(dateString).getTime()) / 60000;
}

async function checkWorkflow(env, item, dryRun = false) {
  const branch = envValue(env, "GITHUB_BRANCH", "main");
  const file = item.file;
  const staleMinutes = item.minutes;

  const encoded = encodeURIComponent(file);
  const runs = await gh(env, `/actions/workflows/${encoded}/runs?branch=${encodeURIComponent(branch)}&per_page=10`);
  const workflowRuns = runs.workflow_runs || [];

  const active = workflowRuns.find(r => ACTIVE_STATUSES.has(r.status));
  if (active) {
    return {
      file,
      action: "skip_active",
      active_status: active.status,
      active_run: active.html_url,
      stale_minutes: staleMinutes
    };
  }

  const latest = workflowRuns[0];
  const age = latest ? minutesSince(latest.created_at || latest.run_started_at || latest.updated_at) : Infinity;
  const conclusion = latest?.conclusion || "none";
  const isFailed = latest && ["failure", "cancelled", "timed_out", "action_required"].includes(conclusion);
  const isStale = age >= staleMinutes;

  if (!latest || isFailed || isStale) {
    if (!dryRun) {
      await gh(env, `/actions/workflows/${encoded}/dispatches`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ref: branch })
      });
    }
    return {
      file,
      action: dryRun ? "would_dispatch" : "dispatched",
      reason: !latest ? "no_runs" : isFailed ? `last_${conclusion}` : `stale_${Math.round(age)}m`,
      last_age_minutes: Number.isFinite(age) ? Math.round(age) : null,
      last_conclusion: conclusion,
      stale_minutes: staleMinutes,
      last_run: latest?.html_url || null
    };
  }

  return {
    file,
    action: "ok_recent",
    last_age_minutes: Math.round(age),
    last_conclusion: conclusion,
    stale_minutes: staleMinutes,
    last_run: latest?.html_url || null
  };
}

async function runWatchdog(env, dryRun = false) {
  const started = new Date().toISOString();
  const workflows = workflowList(env);
  const results = [];
  for (const wf of workflows) {
    try {
      results.push(await checkWorkflow(env, wf, dryRun));
    } catch (err) {
      results.push({ file: wf.file, action: "error", error: String(err && err.message ? err.message : err) });
    }
  }
  return { ok: true, mode: dryRun ? "dry_run" : "live", started, finished: new Date().toISOString(), results };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runWatchdog(env, false));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, worker: "acton-bridge-actions-watchdog", time: new Date().toISOString() });

    if (url.pathname === "/run" || url.pathname === "/dry-run") {
      const secret = envValue(env, "WATCHDOG_SECRET");
      if (secret && url.searchParams.get("token") !== secret) return json({ ok: false, error: "Forbidden" }, 403);
      const dry = url.pathname === "/dry-run" || url.searchParams.get("dry") === "1";
      try {
        return json(await runWatchdog(env, dry));
      } catch (err) {
        return json({ ok: false, error: String(err && err.message ? err.message : err) }, 500);
      }
    }

    return json({
      ok: true,
      message: "Acton Bridge external GitHub Actions watchdog. Use /health, /dry-run, or /run.",
      configured_workflows: workflowList(env)
    });
  }
};
