# Architecture

## Goals

- A crashed or killed runner is indistinguishable from a finished one: systemd
  starts a fresh container. No manual recovery, ever.
- Nothing accumulates on disk between jobs except two bounded, prunable caches.
- One unprivileged owner per host. No root daemon, no sudoers rules.
- More than one host, feeding the same runner group, so no host is a
  build-blocking single point of failure.
- Everything driven from a browser, including live disk usage.

## Components

```
workstation ── deploy.sh ──► each host: setup-host.sh (root, once)
                                        └► agent + slot units + runner image (ghrunner user)
                              manager host also: manager (ghrunner user)

manager ──HTTPS(pinned)──► agent on each host ──systemctl --user / podman──► runner slots
   │
   └──GitHub App──► api.github.com   (registration tokens, runner list, delete)
```

### One unprivileged owner: `ghrunner`

A single system user owns the agent, the podman image store, the runner
containers, and the cache volumes. Everything runs as *user* services inside
`ghrunner`'s own `systemd --user` manager, so the agent controls slots with
plain `systemctl --user` and inspects containers with plain `podman`.

`loginctl enable-linger ghrunner` makes the whole stack start at boot with no
login session. A `user@.service` drop-in delegates the `cpu cpuset io memory
pids` cgroup controllers so rootless `podman --memory` / `--cpus` actually work.

### Manager + agents (not manager-over-SSH)

The manager talks to a small agent on each host over HTTPS, never SSH. Two
reasons: sampling disk every 60 s over SSH is wasteful and fragile, and an
agent keeps a persistent view of local systemd/podman state instead of paying
for a new session per query. Both manager and agent run fully unprivileged.

Each agent serves HTTPS with a self-signed certificate. The manager pins it by
SHA-256 fingerprint (recorded by `deploy.sh` during install) rather than
trusting a CA. Manager↔agent also carry a per-host bearer token in each
direction.

### Ephemeral slot lifecycle

`sp-runner@.service` is a systemd template — one instance per concurrent job
slot:

```
ExecStartPre  sp-runner-prestart <n>
              ├─ if a drain marker exists: disable the unit, exit 69
              │     (RestartPreventExitStatus=69 → systemd stops here, at a job boundary)
              └─ else: ask the agent for a registration token, write a tmpfs env file
ExecStart     sp-runner-run <n>
              └─ podman run --rm ... localhost/sp-runner:<tag>
                 └─ entrypoint: config.sh --ephemeral → run.sh → ONE job → exit
Restart=always, RestartSec=10, StartLimitIntervalSec=0
```

Because the container is `--rm` and `_work` lives inside it, `_work` and
`_diag` cannot accumulate on the host. The registration token is minted
host-side and passed through a tmpfs env file; the GitHub App key never enters
a container.

### Persistent caches — the only thing that survives a job

- **Per-slot tool cache** — volume `sp-tool-<n>` → `/home/runner/_work/_tool`.
  Per-slot because concurrent `setup-java` / `setup-node` installs of the same
  version race on a shared directory.
- **Shared pnpm store** — volume `sp-pnpm` → `/home/runner/.local/share/pnpm`.
  pnpm's store is content-addressed and concurrency-safe, so sharing it is both
  correct and the biggest build-speed win.

### Cross-host fault tolerance

Both hosts contribute slots to the same runner group, so GitHub schedules
across them.

- Agent unreachable → host shown **degraded** in the UI; no destructive action.
- Still unreachable after `reap_grace_minutes` → its GitHub runner
  registrations are deleted so queued jobs stop targeting a dead node.
- Agent returns → slots re-register on next start. No manual step.
- The manager is **not** on the build path: if it is down, running slots on
  every host keep taking jobs. Only creating or changing slots pauses.

### The runner image

The official `ghcr.io/actions/actions-runner` image is not used, because some
networks throttle ghcr.io's blob CDN. Instead the image builds `FROM
docker.io/library/ubuntu:22.04` and installs the runner agent from its GitHub
release tarball (staged into the build context by `deploy.sh` / `build.sh` from
a machine that can reach `release-assets.githubusercontent.com`). It installs
the .NET runtime libraries the runner needs plus a baseline of `git`, `jq`,
`zstd`, `build-essential`, and `python3`. Language toolchains are provisioned at
job time by `setup-*` actions into the persisted tool cache.

## Web manager

FastAPI + Jinja2 + HTMX, server-rendered with HTMX polling — no bundler, no
Node build step. SQLite for the user table, audit log, and disk time series.
Sessions are signed cookies (`itsdangerous`); passwords are argon2.

Background loops: reconcile (merge each agent's `/state` with the GitHub runner
list, drive the degraded→reap state machine) every `reconcile_seconds`; disk
sample + watermark enforcement every `disk_sample_seconds`.

## Disk watermark guard

Acts on the used-percent of each host's first configured mount:

| level | effect |
|---|---|
| warn | UI banner only |
| prune | `podman system prune` on that host + clear idle-slot tool caches |
| freeze | block enabling/creating slots on that host until space is freed |

Plus `sp-runner-gc.timer` daily per host: prune dangling images and stopped
containers, and clear tool caches untouched for more than 14 days.
