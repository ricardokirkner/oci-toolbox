PYTHON ?= python3
PROFILE ?= DEFAULT
REGION ?= us-ashburn-1
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
REMOTE_SCRIPT_PATH ?= /tmp/harden_ubuntu_host.sh
REMOTE_AUDIT_SCRIPT_PATH ?= /tmp/audit_ubuntu_host.sh
PY_CACHE_PREFIX ?= /tmp/oci-toolbox-pyc

.PHONY: help check suggest suggest-capacity setup-provision provision-openclaw provision-always-free provision-payg verify inventory inventory-json reset-dry-run reset-execute harden-host audit-host

help:
	@printf '%s\n' \
	'Targets:' \
	'  make check' \
	'  make suggest PROFILE=DEFAULT' \
	'  make suggest-capacity PROFILE=DEFAULT' \
	'  make setup-provision PROFILE=DEFAULT' \
	'  make provision-openclaw PROFILE=DEFAULT COMPARTMENT_ID=<ocid> SUBNET_ID=<ocid>' \
	'  make provision-always-free PROFILE=DEFAULT COMPARTMENT_ID=<ocid> SUBNET_ID=<ocid>' \
	'  make provision-payg PROFILE=DEFAULT COMPARTMENT_ID=<ocid> SUBNET_ID=<ocid> REGION=<region>' \
	'  make verify PROFILE=DEFAULT COMPARTMENT_ID=<ocid> INSTANCE_ID=<ocid>' \
	'  make inventory PROFILE=DEFAULT' \
	'  make inventory-json PROFILE=DEFAULT' \
	'  make reset-dry-run PROFILE=DEFAULT' \
	'  make reset-execute PROFILE=DEFAULT CONFIRM=DELETE' \
	'  make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa' \
	'  make audit-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa AUDIT_EXPECT_PORTS="22 443"' \
	'  make harden-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa' \
	'  make harden-host HOST=<ip-or-dns> SSH_USER=ubuntu SSH_KEY_PATH=~/.ssh/id_rsa HARDEN_ALLOW_PORTS="80 443"'

check:
	PYTHONPYCACHEPREFIX=$(PY_CACHE_PREFIX) $(PYTHON) -m py_compile *.py
	bash -n audit_ubuntu_host.sh
	bash -n harden_ubuntu_host.sh

suggest:
	$(PYTHON) best_region_provisioner.py suggest --profile $(PROFILE)

suggest-capacity:
	$(PYTHON) best_region_provisioner.py suggest --profile $(PROFILE) --probe-capacity

setup-provision:
	$(PYTHON) best_region_provisioner.py setup-provision --profile $(PROFILE)

provision-openclaw: guard-compartment-id guard-subnet-id
	$(PYTHON) openclaw_provisioner.py \
		--profile $(PROFILE) \
		--compartment-id $(COMPARTMENT_ID) \
		--subnet-id $(SUBNET_ID)

provision-always-free: guard-compartment-id guard-subnet-id
	$(PYTHON) best_region_provisioner.py provision \
		--billing-mode always-free \
		--profile $(PROFILE) \
		--compartment-id $(COMPARTMENT_ID) \
		--region $(REGION) \
		--workers 1 \
		--shapes $(SHAPES) \
		--boot-volume-gb $(BOOT_VOLUME_GB) \
		--subnet-id $(SUBNET_ID)

provision-payg: guard-compartment-id guard-subnet-id
	$(PYTHON) best_region_provisioner.py provision \
		--billing-mode payg \
		--profile $(PROFILE) \
		--compartment-id $(COMPARTMENT_ID) \
		--region $(REGION) \
		--workers 1 \
		--shapes $(SHAPES) \
		--boot-volume-gb $(BOOT_VOLUME_GB) \
		--subnet-id $(SUBNET_ID)

verify: guard-compartment-id
	$(PYTHON) best_region_provisioner.py verify \
		--profile $(PROFILE) \
		--compartment-id $(COMPARTMENT_ID) \
		$(if $(INSTANCE_ID),--instance-id $(INSTANCE_ID),)

inventory:
	$(PYTHON) oci_account_inventory.py --profile $(PROFILE)

inventory-json:
	$(PYTHON) oci_account_inventory.py --profile $(PROFILE) --json

reset-dry-run:
	$(PYTHON) oci_account_reset.py --profile $(PROFILE)

reset-execute:
	@test "$(CONFIRM)" = "DELETE" || (echo "Set CONFIRM=DELETE to run destructive cleanup."; exit 1)
	$(PYTHON) oci_account_reset.py --profile $(PROFILE) --delete-child-compartments --execute --confirm DELETE

audit-host: guard-host
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) audit_ubuntu_host.sh $(SSH_USER)@$(HOST):$(REMOTE_AUDIT_SCRIPT_PATH)
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x $(REMOTE_AUDIT_SCRIPT_PATH) && sudo $(REMOTE_AUDIT_SCRIPT_PATH) $(foreach port,$(AUDIT_EXPECT_PORTS),--expect-open-port $(port))"

harden-host: guard-host
	scp -P $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) harden_ubuntu_host.sh $(SSH_USER)@$(HOST):$(REMOTE_SCRIPT_PATH)
	ssh -t -p $(SSH_PORT) $(if $(SSH_KEY_PATH),-i $(SSH_KEY_PATH),) $(SSH_USER)@$(HOST) "chmod +x $(REMOTE_SCRIPT_PATH) && sudo $(REMOTE_SCRIPT_PATH) --ssh-port $(SSH_PORT) $(foreach port,$(HARDEN_ALLOW_PORTS),--allow-port $(port))"

guard-compartment-id:
	@test -n "$(COMPARTMENT_ID)" || (echo "COMPARTMENT_ID is required."; exit 1)

guard-subnet-id:
	@test -n "$(SUBNET_ID)" || (echo "SUBNET_ID is required."; exit 1)

guard-host:
	@test -n "$(HOST)" || (echo "HOST is required."; exit 1)
