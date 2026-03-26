# OCI Toolbox

Small OCI CLI helpers for:

- suggesting a good home region for a new tenancy
- provisioning `VM.Standard.A1.Flex` instances with Always Free-friendly defaults
- hardening a provisioned Ubuntu host
- auditing what exists in the account
- cleaning up common IaaS resources

## Files

- `best_region_provisioner.py`: main CLI with `suggest`, `provision`, `setup-provision`, and `verify`
- `best_region_always_free_provisioner.py`: focused Always Free provisioner
- `openclaw_provisioner.py`: OpenClaw flow that first offers existing OCI hosts, then falls back to provisioning a new Always Free host
- `bootstrap_openclaw_host.sh`: reusable remote bootstrap script that installs OpenClaw under a dedicated `openclaw` service user
- `review_openclaw_host.sh`: review-and-repair script for OpenClaw host setup
- `sync_openclaw_config_host.sh`: installs a tracked OpenClaw config repo on the host and repoints the service to it
- `audit_ubuntu_host.sh`: read-only audit script for a provisioned Ubuntu OCI VM
- `harden_ubuntu_host.sh`: conservative Ubuntu host-hardening script for provisioned OCI VMs
- `oci_account_inventory.py`: inventory report across subscribed regions
- `oci_account_reset.py`: dry-run-first cleanup helper
- `oci_toolbox_common.py`: shared OCI helpers used by the scripts

## Requirements

- Python 3.10+
- `oci` Python SDK installed in the environment used to run the scripts
- OCI config and credentials in `~/.oci/config`
- an SSH public key available for provisioning flows

Install the SDK if needed:

```bash
python3 -m pip install oci
```

You can also create a local `.env` file for the `make` commands. The Makefile
loads `.env` automatically, so values like `HOST`, `SSH_USER`, `SSH_KEY_PATH`,
`PROFILE`, `COMPARTMENT_ID`, and `SUBNET_ID` do not need to be repeated on each
command. Start from `.env.example` if you want a template.

## Quick Start

Suggest likely regions for a new account:

```bash
python3 best_region_provisioner.py suggest
```

Probe subscribed regions for live A1 capacity:

```bash
python3 best_region_provisioner.py suggest --probe-capacity --profile DEFAULT
```

Run the guided provisioning flow:

```bash
python3 best_region_provisioner.py setup-provision --profile DEFAULT
```

Provision directly with Always Free defaults:

```bash
python3 best_region_provisioner.py provision \
  --billing-mode always-free \
  --profile DEFAULT \
  --compartment-id <compartment-ocid> \
  --region us-ashburn-1 \
  --subnet-id <subnet-ocid>
```

Provision or bootstrap an OpenClaw host:

```bash
python3 openclaw_provisioner.py \
  --profile DEFAULT
```

In an interactive terminal, it first lists running OCI instances with public IPs
and asks which host to bootstrap. If you choose `Provision a new OCI host`, it
falls back to the normal Always Free provisioning flow.

Use `--openclaw-version` to pin a channel or version, or `--no-bootstrap` if you
only want the OCI provisioning step for a newly created host. OpenClaw is
installed under a dedicated `openclaw` service user by default, and the
bootstrap flow also installs Linuxbrew for that user so Homebrew-managed skill
dependencies are available both in the `openclaw` shell and in its
`systemd --user` environment.

Review an existing host's OpenClaw setup and repair common issues:

```bash
make review-openclaw-host \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa
```

Sync a tracked local OpenClaw config repo to the host:

```bash
make sync-openclaw-config \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa \
  OPENCLAW_CONFIG_REPO=/path/to/openclaw-config-repo
```

The expected repo layout is simple: keep `openclaw.json` at the repo root, and
store any included files or directories underneath that same root. The sync
command copies the repo to the host, points the `openclaw` service user at the
tracked `openclaw.json`, validates the config, and restarts the gateway if that
user service is already active.

Verify whether a launch is aligned with the documented Always Free envelope:

```bash
python3 best_region_provisioner.py verify \
  --profile DEFAULT \
  --compartment-id <compartment-ocid> \
  --instance-id <instance-ocid>
```

Review current resources:

```bash
python3 oci_account_inventory.py --profile DEFAULT
```

Dry-run account cleanup:

```bash
python3 oci_account_reset.py --profile DEFAULT
```

Audit a provisioned Ubuntu instance over SSH:

```bash
make audit-host \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa
```

If the host is expected to expose more than SSH, declare those ports so the
audit treats them as intentional:

```bash
make audit-host \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa \
  AUDIT_EXPECT_PORTS="22 443"
```

Harden a provisioned Ubuntu instance over SSH:

```bash
make harden-host \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa
```

If the host should serve web traffic, add ports explicitly:

```bash
make harden-host \
  HOST=<public-ip-or-dns> \
  SSH_USER=ubuntu \
  SSH_KEY_PATH=~/.ssh/id_rsa \
  HARDEN_ALLOW_PORTS="80 443"
```

Or copy the script manually and run it on the host:

```bash
scp -i ~/.ssh/id_rsa harden_ubuntu_host.sh ubuntu@<public-ip>:/tmp/
ssh -i ~/.ssh/id_rsa ubuntu@<public-ip>
chmod +x /tmp/harden_ubuntu_host.sh
sudo /tmp/harden_ubuntu_host.sh --ssh-port 22
```

## Suggested Always Free Defaults

For A1 capacity hunting, start small:

- shape order: `1x6,2x12,3x18,4x24`
- boot volume: `50 GB`
- worker count: `1`
- billing mode: `always-free`

These settings keep the requested shape small while staying aligned with Oracle's documented Always Free A1 envelope.

## Host Hardening

`harden_ubuntu_host.sh` is intentionally conservative. It currently:

- applies package updates and optional dist-upgrade
- enables unattended security upgrades
- installs and configures `fail2ban`
- enables `ufw` with deny-incoming / allow-outgoing defaults
- keeps SSH open on the configured port
- optionally opens additional TCP ports such as `80` and `443`
- disables SSH password and root login
- applies a small set of low-risk network sysctl hardening settings
- installs and enables `auditd` by default
- disables `rpcbind` by default

It is meant for Ubuntu cloud hosts accessed by SSH key. Review it before using it on anything with unusual SSH, firewall, or networking requirements.

## Host Audit

`audit_ubuntu_host.sh` prints:

- an overall status: `PASS`, `WARN`, or `FAIL`
- positive findings
- warnings worth reviewing
- failures that should be fixed

By default it assumes only the SSH daemon port should be reachable. Use
`AUDIT_EXPECT_PORTS` with `make audit-host` if the host should intentionally
expose additional ports.

## Safety Notes

- `always-free` mode only makes sense in the tenancy home region.
- The scripts can check current tenancy usage, but they cannot guarantee zero billing if the account has already consumed monthly free usage earlier in the billing cycle.
- `oci_account_reset.py` is destructive when run with `--execute`. Start with dry-run output and inspect the inventory first.
- `setup-provision` can create compartments, VCNs, gateways, route tables, and subnets. Review the prompts carefully before confirming.
- `harden_ubuntu_host.sh` resets and reenables `ufw`. If your host needs nonstandard inbound access, pass the required ports when you run it.

## Make Targets

The `Makefile` wraps the common commands:

```bash
make help
make check
make suggest
make setup-provision PROFILE=DEFAULT
make provision-openclaw PROFILE=DEFAULT
make provision-always-free PROFILE=DEFAULT
make verify PROFILE=DEFAULT INSTANCE_ID=<instance-ocid>
make inventory
make reset-dry-run
make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa
make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa AUDIT_EXPECT_PORTS="22 443"
make harden-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa HARDEN_ALLOW_PORTS="80 443"
make review-openclaw-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa
make sync-openclaw-config HOST=<ip-or-dns> OPENCLAW_CONFIG_REPO=/path/to/openclaw-config-repo
```

If you omit `COMPARTMENT_ID`, `SUBNET_ID`, or `REGION` on the provisioning
commands, the CLI can list existing OCI resources and let you select one or
create a new one interactively. For `make` commands, those values can also come
from a local `.env` file, and command-line assignments still take precedence
over `.env`.
