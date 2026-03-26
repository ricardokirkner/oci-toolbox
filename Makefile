PYTHON ?= python3
PROFILE ?= DEFAULT
REGION ?=
COMPARTMENT_ID ?=
SUBNET_ID ?=
INSTANCE_ID ?=
SHAPES ?= 1x6,2x12,3x18,4x24
BOOT_VOLUME_GB ?= 50
HOST ?=
SSH_USER ?= ubuntu
SSH_KEY_PATH ?=
SSH_PORT ?= 22
AUDIT_EXPECT_PORTS ?=
HARDEN_ALLOW_PORTS ?=
OPENCLAW_SERVICE_USER ?= openclaw
OPENCLAW_VERSION ?= latest
OPENCLAW_PREFIX ?=
OPENCLAW_CONFIG_REPO ?=
OPENCLAW_CONFIG_FILE ?= openclaw.json
OPENCLAW_CONFIG_REMOTE_ROOT ?=
REMOTE_SCRIPT_PATH ?= /tmp/harden_ubuntu_host.sh
REMOTE_AUDIT_SCRIPT_PATH ?= /tmp/audit_ubuntu_host.sh
REMOTE_OPENCLAW_BOOTSTRAP_SCRIPT_PATH ?= /tmp/bootstrap_openclaw_host.sh
REMOTE_OPENCLAW_REVIEW_SCRIPT_PATH ?= /tmp/review_openclaw_host.sh
REMOTE_OPENCLAW_CONFIG_SYNC_SCRIPT_PATH ?= /tmp/sync_openclaw_config_host.sh
REMOTE_OPENCLAW_CONFIG_STAGING_PATH ?= /tmp/openclaw-config-staging
PY_CACHE_PREFIX ?= /tmp/oci-toolbox-pyc

ifneq ($(wildcard .env),)
include .env
endif

export PROFILE REGION COMPARTMENT_ID SUBNET_ID INSTANCE_ID SHAPES BOOT_VOLUME_GB
export HOST SSH_USER SSH_KEY_PATH SSH_PORT AUDIT_EXPECT_PORTS HARDEN_ALLOW_PORTS
export OPENCLAW_SERVICE_USER OPENCLAW_VERSION OPENCLAW_PREFIX
export OPENCLAW_CONFIG_REPO OPENCLAW_CONFIG_FILE OPENCLAW_CONFIG_REMOTE_ROOT

.PHONY: help check suggest suggest-capacity setup-provision provision-openclaw provision-always-free provision-payg verify inventory inventory-json reset-dry-run reset-execute harden-host audit-host review-openclaw-host sync-openclaw-config

help:
	@printf '%s\n' \
	'Targets:' \
	'  values can come from a local .env file, with command-line vars still taking precedence' \
	'  make check' \
	'  make suggest PROFILE=DEFAULT' \
	'  make suggest-capacity PROFILE=DEFAULT' \
	'  make setup-provision PROFILE=DEFAULT' \
	'  make provision-openclaw PROFILE=DEFAULT [COMPARTMENT_ID=<ocid>] [SUBNET_ID=<ocid>]' \
	'    first offers existing OCI hosts with public IPs, then can provision a new host' \
	'  make provision-always-free PROFILE=DEFAULT [COMPARTMENT_ID=<ocid>] [SUBNET_ID=<ocid>] [REGION=<region>]' \
	'  make provision-payg PROFILE=DEFAULT [COMPARTMENT_ID=<ocid>] [SUBNET_ID=<ocid>] [REGION=<region>]' \
	'  make verify PROFILE=DEFAULT [COMPARTMENT_ID=<ocid>] [INSTANCE_ID=<ocid>]' \
	'  Omit OCI IDs to select existing resources or create new ones interactively.' \
	'  make inventory PROFILE=DEFAULT' \
	'  make inventory-json PROFILE=DEFAULT' \
	'  make reset-dry-run PROFILE=DEFAULT' \
	'  make reset-execute PROFILE=DEFAULT CONFIRM=DELETE' \
	'  make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa' \
	'  make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa AUDIT_EXPECT_PORTS="22 443"' \
	'  make harden-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa' \
	'  make harden-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa HARDEN_ALLOW_PORTS="80 443"' \
	'  make review-openclaw-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa' \
	'  make sync-openclaw-config HOST=<ip-or-dns> OPENCLAW_CONFIG_REPO=/path/to/repo'

check:
	PYTHONPYCACHEPREFIX=$(PY_CACHE_PREFIX) $(PYTHON) -m py_compile *.py
	bash -n audit_ubuntu_host.sh
	bash -n bootstrap_openclaw_host.sh
	bash -n harden_ubuntu_host.sh
	bash -n review_openclaw_host.sh
	bash -n sync_openclaw_config_host.sh

suggest:
	$(PYTHON) best_region_provisioner.py suggest --profile $(PROFILE)

suggest-capacity:
	$(PYTHON) best_region_provisioner.py suggest --profile $(PROFILE) --probe-capacity

setup-provision:
	$(PYTHON) best_region_provisioner.py setup-provision --profile $(PROFILE)

provision-openclaw:
	$(PYTHON) openclaw_provisioner.py \
		--profile $(PROFILE) \
		$(if $(COMPARTMENT_ID),--compartment-id $(COMPARTMENT_ID),) \
		$(if $(SUBNET_ID),--subnet-id $(SUBNET_ID),)

provision-always-free:
	$(PYTHON) best_region_provisioner.py provision \
		--billing-mode always-free \
		--profile $(PROFILE) \
		$(if $(COMPARTMENT_ID),--compartment-id $(COMPARTMENT_ID),) \
		$(if $(REGION),--region $(REGION),) \
		--workers 1 \
		--shapes $(SHAPES) \
		--boot-volume-gb $(BOOT_VOLUME_GB) \
		$(if $(SUBNET_ID),--subnet-id $(SUBNET_ID),)

provision-payg:
	$(PYTHON) best_region_provisioner.py provision \
		--billing-mode payg \
		--profile $(PROFILE) \
		$(if $(COMPARTMENT_ID),--compartment-id $(COMPARTMENT_ID),) \
		$(if $(REGION),--region $(REGION),) \
		--workers 1 \
		--shapes $(SHAPES) \
		--boot-volume-gb $(BOOT_VOLUME_GB) \
		$(if $(SUBNET_ID),--subnet-id $(SUBNET_ID),)

verify:
	$(PYTHON) best_region_provisioner.py verify \
		--profile $(PROFILE) \
		$(if $(COMPARTMENT_ID),--compartment-id $(COMPARTMENT_ID),) \
		$(if $(INSTANCE_ID),--instance-id $(INSTANCE_ID),)

inventory:
	$(PYTHON) oci_account_inventory.py --profile $(PROFILE)

inventory-json:
	$(PYTHON) oci_account_inventory.py --profile $(PROFILE) --json

reset-dry-run:
	$(PYTHON) oci_account_reset.py --profile $(PROFILE)

reset-execute:
	$(PYTHON) oci_account_reset.py --profile $(PROFILE) --delete-child-compartments --execute $(if $(CONFIRM),--confirm $(CONFIRM),)

audit-host: guard-host
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) audit_ubuntu_host.sh $(SSH_USER)@$(HOST):$(REMOTE_AUDIT_SCRIPT_PATH)
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x $(REMOTE_AUDIT_SCRIPT_PATH) && sudo $(REMOTE_AUDIT_SCRIPT_PATH) $(foreach port,$(AUDIT_EXPECT_PORTS),--expect-open-port $(port))"

harden-host: guard-host
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) harden_ubuntu_host.sh $(SSH_USER)@$(HOST):$(REMOTE_SCRIPT_PATH)
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x $(REMOTE_SCRIPT_PATH) && sudo $(REMOTE_SCRIPT_PATH) --ssh-port $(SSH_PORT) $(foreach port,$(HARDEN_ALLOW_PORTS),--allow-port $(port))"

review-openclaw-host: guard-host
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) bootstrap_openclaw_host.sh $(SSH_USER)@$(HOST):$(REMOTE_OPENCLAW_BOOTSTRAP_SCRIPT_PATH)
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) review_openclaw_host.sh $(SSH_USER)@$(HOST):$(REMOTE_OPENCLAW_REVIEW_SCRIPT_PATH)
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x $(REMOTE_OPENCLAW_BOOTSTRAP_SCRIPT_PATH) $(REMOTE_OPENCLAW_REVIEW_SCRIPT_PATH) && sudo $(REMOTE_OPENCLAW_REVIEW_SCRIPT_PATH) --bootstrap-script-path $(REMOTE_OPENCLAW_BOOTSTRAP_SCRIPT_PATH) --openclaw-user $(OPENCLAW_SERVICE_USER) --openclaw-version $(OPENCLAW_VERSION) $(if $(OPENCLAW_PREFIX),--openclaw-prefix $(OPENCLAW_PREFIX),)"

sync-openclaw-config: guard-host guard-openclaw-config-repo
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) sync_openclaw_config_host.sh $(SSH_USER)@$(HOST):$(REMOTE_OPENCLAW_CONFIG_SYNC_SCRIPT_PATH)
	ssh -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "rm -rf '$(REMOTE_OPENCLAW_CONFIG_STAGING_PATH)' && mkdir -p '$(REMOTE_OPENCLAW_CONFIG_STAGING_PATH)'"
	tar --exclude='.git' --exclude='.DS_Store' -C "$(OPENCLAW_CONFIG_REPO)" -cf - . | ssh -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "tar -xf - -C '$(REMOTE_OPENCLAW_CONFIG_STAGING_PATH)'"
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x '$(REMOTE_OPENCLAW_CONFIG_SYNC_SCRIPT_PATH)' && sudo '$(REMOTE_OPENCLAW_CONFIG_SYNC_SCRIPT_PATH)' --openclaw-user '$(OPENCLAW_SERVICE_USER)' --config-staging-path '$(REMOTE_OPENCLAW_CONFIG_STAGING_PATH)' --config-file '$(OPENCLAW_CONFIG_FILE)' $(if $(OPENCLAW_CONFIG_REMOTE_ROOT),--config-root '$(OPENCLAW_CONFIG_REMOTE_ROOT)',)"

guard-host:
	@test -n "$(HOST)" || (echo "HOST is required."; exit 1)

guard-openclaw-config-repo:
	@test -n "$(OPENCLAW_CONFIG_REPO)" || (echo "OPENCLAW_CONFIG_REPO is required."; exit 1)
	@test -d "$(OPENCLAW_CONFIG_REPO)" || (echo "OPENCLAW_CONFIG_REPO does not exist: $(OPENCLAW_CONFIG_REPO)"; exit 1)
	@test -f "$(OPENCLAW_CONFIG_REPO)/$(OPENCLAW_CONFIG_FILE)" || (echo "OPENCLAW_CONFIG_FILE was not found: $(OPENCLAW_CONFIG_REPO)/$(OPENCLAW_CONFIG_FILE)"; exit 1)
