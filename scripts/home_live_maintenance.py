#!/usr/bin/python3 -IB
"""Node-only maintenance for the existing authenticated home installation."""

import hashlib
import json
import os
import re
import runpy
import secrets
import subprocess
import sys
from pathlib import Path

SITE = Path("/srv/lead-prod-vault/lead-onboard-87d151a")
MONITOR = ("b48825dd-69b9-438c-b73c-407cdf215cb8", "urn:lead:local-release-health", "node-health-reader")
TOOLS = Path(__file__).resolve().parent


def site_sql(query):
    # The root-owned site adapter supplies local DB credentials without exporting them.
    return runpy.run_path(str(SITE / "site-state.py"))["sql"](query)


def property_of(role, name):
    return subprocess.check_output(["systemctl", "show", f"lead-home-{role}", "-p", name, "--value"], text=True).strip()


def health():
    tenant, issuer, subject = MONITOR
    pid = property_of("api", "MainPID")
    if not pid.isdigit() or int(pid) < 2:
        raise ValueError("API unavailable")
    # Exactly one viewer membership across all tenants. It has no business agents.
    allowed = site_sql(f"""SELECT count(*)=1 AND bool_and(tenant_id='{tenant}' AND role='viewer' AND status='active')
        AND NOT EXISTS(SELECT FROM lead.agents WHERE tenant_id='{tenant}')
        FROM lead.memberships WHERE oidc_issuer='{issuer}' AND oidc_subject='{subject}'""")
    if allowed != "t":
        raise ValueError("isolated health identity has changed")
    secret = "lead_s_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    site_sql(f"""INSERT INTO lead.auth_sessions(secret_sha256,tenant_id,issuer,subject,expires_at)
        VALUES('{digest}','{tenant}','{issuer}','{subject}',now()+interval '30 seconds')""")
    try:
        result = subprocess.run(["nsenter", "-t", pid, "-n", "--", "curl", "-q", "-fsS", "--max-time", "8",
                                 "--noproxy", "*", "-K", "-", "http://127.0.0.1:8088/api/health"],
                                input=f'cookie = "lead_session={secret}"\n', text=True, capture_output=True, check=True)
        if property_of("api", "MainPID") != pid:
            raise ValueError("API restarted during check")
        return json.loads(result.stdout)
    finally:
        site_sql(f"DELETE FROM lead.auth_sessions WHERE secret_sha256='{digest}'")


def verify_workflow(sha, invocation):
    if not re.fullmatch("[a-f0-9]{32}", invocation) or property_of("worker", "InvocationID") != invocation:
        raise ValueError("worker invocation changed")
    # Run the reviewed independent verifier with the installed SDK in the private network.
    program = (TOOLS / "home_verify_workflow.py").read_text()
    tls = SITE / "temporal/tls"
    payload = {"sha": sha, "inv": invocation, "ca": (tls/"ca.crt").read_text(),
               "crt": (tls/"verifier.crt").read_text(), "key": (tls/"verifier.key").read_text()}
    pid = property_of("worker", "MainPID")
    subprocess.run(["nsenter", "-t", pid, "-m", "-n", "-e", "--", "setpriv", "--reuid=lead-r-app",
                    "--regid=lead-r-app", "--clear-groups", "/run/lead-home/bin/py/bin/python3.12", "-B", "-c", program, "--site-stdin", sha, invocation],
                   input=json.dumps(payload), text=True, check=True, timeout=30)
    if property_of("worker", "InvocationID") != invocation:
        raise ValueError("worker changed during verification")


# Tables which could admit or change an external action are locked while marking idle agents.
# The separate long-lived release fence still checks the full set (including leases) before cutover.
LOCK = """SET LOCAL lock_timeout='4s';
LOCK TABLE lead.agents,lead.tasks,lead.actions,lead.browser_runs,lead.chat_jobs,lead.takeover_sessions,
lead.social_accounts,lead.account_targets,lead.authorization_grants,lead.interaction_cards IN SHARE ROW EXCLUSIVE MODE;"""
BUSY = """EXISTS(SELECT FROM lead.tasks WHERE state IN ('running','queued')) OR
    EXISTS(SELECT FROM lead.actions WHERE state IN ('dispatching','unknown','prepared')) OR
    EXISTS(SELECT FROM lead.browser_runs WHERE state='running') OR
    EXISTS(SELECT FROM lead.chat_jobs WHERE status IN ('pending','processing')) OR
    EXISTS(SELECT FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now())"""
KEY = "home_release_maintenance"
FINGERPRINT = """jsonb_build_object('control',a.control_version,'config',a.config_revision,'authorization',a.authorization_version,
    'targets',(SELECT md5(string_agg(to_jsonb(t)::text,',' ORDER BY id)) FROM lead.account_targets t WHERE agent_id=a.id),
    'accounts',(SELECT md5(string_agg(jsonb_build_array(id,external_account_id,state,automation_enabled)::text,',' ORDER BY id))
                FROM lead.social_accounts WHERE agent_id=a.id),
    'grants',(SELECT md5(string_agg(to_jsonb(g)::text,',' ORDER BY id)) FROM lead.authorization_grants g WHERE agent_id=a.id),
    'delegations',(SELECT md5(string_agg(jsonb_build_array(id,state,expires_at,payload)::text,',' ORDER BY id))
                   FROM lead.interaction_cards WHERE agent_id=a.id AND type='manage_community'))"""


def maintenance(action, sha):
    # The same transaction journals the original authorization fingerprint and changes only
    # scheduling intent. No domain pause/resume action (which cancels tasks/redelegates) runs.
    if action == "hold":
        query = f"""DO $$ BEGIN
        IF EXISTS(SELECT FROM lead.agents WHERE onboarding_state ? '{KEY}' AND
           onboarding_state#>>'{{{KEY},sha}}' IS DISTINCT FROM '{sha}') THEN RAISE EXCEPTION 'other maintenance'; END IF;
        IF {BUSY} THEN RAISE EXCEPTION 'waiting for active work'; END IF;
        END $$;
        UPDATE lead.agents a SET desired_state='paused',onboarding_state=onboarding_state ||
            jsonb_build_object('{KEY}',jsonb_build_object('sha','{sha}','before',{FINGERPRINT}))
        WHERE desired_state='running' AND NOT onboarding_state ? '{KEY}';"""
    else:
        query = f"""DO $$ BEGIN
        IF EXISTS(SELECT FROM lead.agents a WHERE onboarding_state ? '{KEY}' AND
            (onboarding_state#>>'{{{KEY},sha}}' IS DISTINCT FROM '{sha}' OR desired_state<>'paused'
             OR onboarding_state#>'{{{KEY},before}}' IS DISTINCT FROM {FINGERPRINT}))
        THEN RAISE EXCEPTION 'state changed during maintenance'; END IF;
        END $$;
        UPDATE lead.agents SET desired_state='running',onboarding_state=onboarding_state-'{KEY}'
        WHERE onboarding_state#>>'{{{KEY},sha}}'='{sha}';"""
    site_sql("BEGIN;" + LOCK + query + "COMMIT;")


def main(args):
    if os.geteuid() != 0:
        raise ValueError("root required")
    if args == ["ready"]:
        print("busy" if site_sql("SELECT " + BUSY) == "t" else "idle")
        return
    if args == ["health"]:
        print(json.dumps(health()))
        return
    if len(args) < 2 or not re.fullmatch("[a-f0-9]{40}", args[1]):
        raise ValueError("expected exact application revision")
    if args[0] == "fence" and len(args) == 3 and re.fullmatch("[a-f0-9]{40}", args[2]):
        unit = f"lead-home-site-fence@{args[1]}-{args[2]}.service"
        pid = subprocess.check_output(["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True).strip()
        if not pid.isdigit() or int(pid) < 2:
            raise ValueError("release fence not held")
        subprocess.run(["nsenter", "-t", pid, "-m", "-n", "-e", "--", "setpriv", "--reuid=lead-release-fence",
                        "--regid=lead-release-fence", "--clear-groups", "/run/lead-home/bin/py/bin/python3.12",
                        "-B", "/run/lead-home/fence.py", *args[1:]], check=True, timeout=10)
    elif args[0] == "workflow" and len(args) == 3:
        verify_workflow(args[1], args[2])
    elif args[0] in ("hold", "restore") and len(args) == 2:
        maintenance(*args)
    else:
        raise ValueError("unknown maintenance command")


if __name__ == "__main__":
    main(sys.argv[1:])
