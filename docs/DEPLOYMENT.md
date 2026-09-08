# Deployment

Run these from a workstation with `bash`, `ssh`, `rsync`, `python3` (3.9+), and
`openssl`. Deploy the **manager host first**, then add the rest as agents.

Legend: `[ws]` = your workstation · `[host]` = SSH'd into a target host.

## 1. SSH keys

`deploy.sh` never accepts a password on the command line.

```
[ws] ssh-copy-id -i ~/.ssh/id_ed25519.pub deploy@<host>       # for each host
[ws] ssh deploy@<host> true && echo ok
```

The SSH user needs `sudo` (a password prompt is fine — `deploy.sh` uses
`ssh -t sudo`).

## 2. (optional) Retire an existing bare-metal runner setup

If the host already runs bare-metal runners you want to replace:

```
[ws]   rsync -a --exclude .git --exclude secrets ./ deploy@<host>:/tmp/erf/
[ws]   ssh -t deploy@<host>
[host] sudo bash /tmp/erf/migration/00-backup.sh              # snapshot first
[host] sudo bash /tmp/erf/migration/02-retire-bare-metal.sh          # dry run — read it
[host] sudo bash /tmp/erf/migration/02-retire-bare-metal.sh --apply
```

Old GitHub-side registrations go offline; delete them from the org/repo
**Runners** page, or use `migration/03-retire-runner.sh` (deregisters cleanly,
after draining).

## 3. (optional) Grow the runner-data filesystem

The runner image plus per-job container overlays live under `/home`. Budget
~25 GB of steady-state cache for 3–4 slots plus 10–20 GB of transient overlay
per concurrent job.

```
[host] sudo dnf install -y cloud-utils-growpart
[host] sudo bash /tmp/erf/migration/01-reclaim-swap.sh --apply   # if you have an unused swap LV
[host] sudo bash /tmp/erf/migration/grow-home.sh --apply         # after enlarging the virtual disk
```

## 4. Create the GitHub App

On your org (Settings → Developer settings → GitHub Apps → New GitHub App):

- Name: anything; Homepage URL: anything.
- **Untick** Webhook → Active.
- **Organization permissions → Self-hosted runners: Read and write.**
- (optional) **Organization permissions → Administration: Read-only** — only
  makes runner-group names appear in the UI.
- "Only on this account". Create it, note the **App ID**.
- **Generate a private key** → downloads a `.pem`.
- Left sidebar → **Install App** → install on your org. The install URL ends in
  `/installations/<number>` — that's the **Installation ID**.

## 5. Configure

```
[ws] cp hosts.toml.example hosts.toml
[ws] $EDITOR hosts.toml
```

```toml
[[host]]
id   = "build-farm"
ssh  = "deploy@10.0.0.10"
role = "manager"                 # installs manager + agent; the web UI lives here
lan_cidr = "10.0.0.0/16"         # firewall scope for UI port 8080 (comma-separate for multiple)

[[host]]
id   = "build-farm-2"
ssh  = "deploy@10.0.1.20"
role = "agent"                   # agent only
allow_from = "10.0.0.10"         # only the manager may reach agent port 8181
```

```
[ws] ./deploy.sh init                        # writes secrets/ (tokens, cookie key, agent configs)
[ws] cp ~/Downloads/<app>.private-key.pem secrets/github-app.pem
[ws] $EDITOR secrets/manager.toml            # set [github] app_id + installation_id
```

Everything else in `secrets/manager.toml` — `secret_key`, the per-host bearer
tokens — is generated; leave it. `agent_tls_fingerprint` is filled in during
`deploy.sh push`.

The manager also runs fine with `app_id`/`installation_id` left at `0` — the UI
and disk monitoring work, only runner registration is disabled until they are
set.

## 6. Deploy

```
[ws] ./deploy.sh push build-farm             # manager host first
```

Runs `setup-host.sh` (creates `ghrunner`, linger, cgroup delegation, firewall
port), installs the agent + manager, builds the runner image, starts both
services, and prints the **seed admin password** — copy it. If sudo prompts for
a password, that is expected.

```
[ws] curl -sS http://10.0.0.10:8080/healthz       # {"status":"ok"}
[ws] ./deploy.sh push build-farm-2                 # each additional host
```

`deploy.sh push` is idempotent — re-run it to upgrade a host in place. It
records each agent's TLS fingerprint into `secrets/manager.toml` and restarts
the manager so the pin takes effect.

Open `http://<manager-host>:8080`, sign in as `admin`, change the password
under **Settings**. **Settings → GitHub App** should read "token OK".

Lost the seed password? On the manager host:

```
[host] cd ~ghrunner/sp-runner-manager
[host] sudo -u ghrunner SP_MANAGER_CONFIG=~ghrunner/.config/sp-runner/manager.toml \
         .venv/bin/python manage.py reset-password admin
```

## 7. Prove one ephemeral slot

In the UI → **Fleet → Create runners**: count `1`, a distinct label like
`podman-test`, no group, memory `12g`, cpus `2`.

Within ~30 s the slot shows **idle** and a `<host>-slot-1` runner appears in the
org Runners page. Run a scratch workflow:

```yaml
on: workflow_dispatch
jobs:
  smoke:
    runs-on: [self-hosted, podman-test]
    steps:
      - run: echo "hello from $(hostname)"; free -h; df -h /
```

Expected: Fleet shows the slot **running** then back to **idle**; no leftover
container (`sudo -u ghrunner podman ps -a`); `/home` usage flat before/after.

Fault test:

```
[host] sudo -u ghrunner XDG_RUNTIME_DIR=/run/user/$(id -u ghrunner) podman kill sp-runner-1
```

→ systemd recreates it within ~10 s; the Fleet restart counter increments.

Only after a real build passes on `podman-test` should you move slots into your
production runner group.

## 8. Enable additional hosts

Non-manager hosts are created `enabled = false` in `secrets/manager.toml`. After
`./deploy.sh push <host>`, set that host's `enabled = true` and re-run
`./deploy.sh push <manager-host>` to refresh the manager.

## Troubleshooting

| symptom | check |
|---|---|
| `deploy.sh push` fails building the venv | `ssh <host> cat /etc/sp-runner-python` — must be a Python ≥ 3.11 path |
| agent not active | `ssh <host> 'sudo -u ghrunner XDG_RUNTIME_DIR=/run/user/$(id -u ghrunner) systemctl --user status sp-runner-agent'` |
| manager 500s | same, unit `sp-runner-manager` |
| "agent TLS fingerprint mismatch" | re-run `./deploy.sh push <host>` so the pin is re-recorded |
| slot never starts a container | image built? `ssh <host> 'sudo -u ghrunner podman images'`; memory value has a unit? (`12g`, not `12`) |
| container OOM-killed immediately | cgroup `memory` controller not delegated — re-run `setup-host.sh`, which restarts the user manager |
| "host is disk-frozen" | the first mount is over 92% — free space or run `grow-home.sh` |
| runner never picks up jobs | label / group mismatch between the workflow `runs-on` and the slot |
| image build hangs at "Copying blob" | the network is throttling a registry CDN — see the runner-image note in ARCHITECTURE.md |
