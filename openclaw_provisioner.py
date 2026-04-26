#!/usr/bin/env python3
"""
Provision an OCI A1 instance bootstrapped for OpenClaw.

This flow keeps the OCI-side logic intentionally narrow:
- Always Free-compatible home-region launches only
- Ubuntu ARM images on VM.Standard.A1.Flex
- OpenClaw CLI bootstrapped into a dedicated non-root service user
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import queue
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import oci

from oci_toolbox_common import (
    DEFAULT_SUBNET_IDS,
    MIN_BOOT_VOLUME_GB,
    build_openclaw_cloud_init,
    can_prompt,
    calculate_always_free_headroom,
    choose_or_create_compartment,
    choose_or_create_subnet,
    choose_or_create_vcn,
    describe_region_choice,
    filter_regions_with_subnets,
    filter_shapes_for_headroom,
    get_home_region,
    get_region_subscriptions,
    list_all,
    list_compartments_with_root,
    load_config,
    load_ssh_key,
    parse_shapes,
    prompt_choice,
    prompt_existing_path,
    prompt_text,
    rank_regions_by_distance,
    resolve_location,
    resolve_subnet_id,
    worker,
)

REMOTE_OPENCLAW_BOOTSTRAP_PATH = "/tmp/bootstrap_openclaw_host.sh"
DEFAULT_OPENCLAW_USER = "openclaw"
DEFAULT_OPENCLAW_PREFIX = f"/home/{DEFAULT_OPENCLAW_USER}/.openclaw"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap OpenClaw onto an existing OCI host or provision a new "
            "Always Free-friendly OCI A1 instance."
        )
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--compartment-id",
        default=os.environ.get("OCI_COMPARTMENT_ID"),
        help="Compartment OCID used for images and instance creation",
    )
    parser.add_argument(
        "--ssh-key-path",
        default=os.path.expanduser("~/.ssh/id_rsa.pub"),
        help="Public SSH key injected into the instance",
    )
    parser.add_argument(
        "--ssh-private-key-path",
        help="Private SSH key used when bootstrapping an existing host over SSH",
    )
    parser.add_argument(
        "--ssh-user",
        default="ubuntu",
        help="SSH user for existing-host bootstrap flows",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=22,
        help="SSH port for existing-host bootstrap flows",
    )
    parser.add_argument(
        "--subnet-id",
        default=os.environ.get("OCI_SUBNET_ID"),
        help="Subnet OCID override for the tenancy home region",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Serial capacity-hunting worker count. Only 1 is supported.",
    )
    parser.add_argument(
        "--shapes",
        default="2x12,1x6,3x18,4x24",
        help="Shape attempts in OCPUxMemoryGB format, comma separated",
    )
    parser.add_argument(
        "--name-prefix",
        default="openclaw",
        help="Display name prefix for the created instance",
    )
    parser.add_argument(
        "--boot-volume-gb",
        type=int,
        default=MIN_BOOT_VOLUME_GB,
        help="Boot volume size in GB",
    )
    parser.add_argument(
        "--openclaw-prefix",
        default=DEFAULT_OPENCLAW_PREFIX,
        help="Install prefix for the OpenClaw CLI on the instance",
    )
    parser.add_argument(
        "--openclaw-user",
        default=DEFAULT_OPENCLAW_USER,
        help="Dedicated service user that owns and runs OpenClaw",
    )
    parser.add_argument(
        "--openclaw-version",
        default="latest",
        help="OpenClaw release channel or version passed to install-cli.sh",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        default=True,
        help="Install the OpenClaw CLI via cloud-init",
    )
    parser.add_argument(
        "--no-bootstrap",
        action="store_false",
        dest="bootstrap",
        help="Disable cloud-init bootstrapping",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve home region, subnet selection, and quota headroom, then exit",
    )
    return parser.parse_args()


def default_private_key_path(public_key_path: str) -> str:
    expanded = os.path.expanduser(public_key_path)
    if expanded.endswith(".pub"):
        return expanded[:-4]
    return os.path.expanduser("~/.ssh/id_rsa")


def resolve_openclaw_prefix(args: argparse.Namespace) -> str:
    if (
        args.openclaw_user != DEFAULT_OPENCLAW_USER
        and args.openclaw_prefix == DEFAULT_OPENCLAW_PREFIX
    ):
        return f"/home/{args.openclaw_user}/.openclaw"
    return args.openclaw_prefix


def get_instance_public_ip(
    compute: oci.core.ComputeClient,
    network: oci.core.VirtualNetworkClient,
    instance: oci.core.models.Instance,
) -> str | None:
    attachments = list_all(
        compute.list_vnic_attachments,
        compartment_id=instance.compartment_id,
        instance_id=instance.id,
    )
    for attachment in attachments:
        vnic = network.get_vnic(attachment.vnic_id).data
        if getattr(vnic, "public_ip", None):
            return str(vnic.public_ip)
    return None


def collect_existing_hosts(
    base_config: Dict[str, str],
    compartment_rows: Sequence[Tuple[str, str]],
    subscribed_regions: Sequence[str],
) -> List[Dict[str, Any]]:
    hosts: List[Dict[str, Any]] = []

    for region_name in subscribed_regions:
        region_config = base_config.copy()
        region_config["region"] = region_name
        compute = oci.core.ComputeClient(region_config)
        network = oci.core.VirtualNetworkClient(region_config)

        for compartment_name, compartment_id in compartment_rows:
            instances = [
                instance
                for instance in list_all(compute.list_instances, compartment_id=compartment_id)
                if instance.lifecycle_state == "RUNNING"
            ]
            for instance in instances:
                public_ip = get_instance_public_ip(compute, network, instance)
                if not public_ip:
                    continue
                hosts.append(
                    {
                        "instance_id": instance.id,
                        "display_name": instance.display_name or instance.id,
                        "compartment_id": compartment_id,
                        "compartment_name": compartment_name,
                        "region": region_name,
                        "shape": instance.shape,
                        "public_ip": public_ip,
                    }
                )

    hosts.sort(
        key=lambda host: (
            host["display_name"].lower(),
            host["region"],
            host["compartment_name"].lower(),
        )
    )
    return hosts


def choose_existing_host(
    hosts: Sequence[Dict[str, Any]],
) -> Dict[str, Any] | None:
    if not hosts:
        return None

    options = [
        (
            host["instance_id"],
            (
                f"{host['display_name']} ({host['public_ip']}) "
                f"region={host['region']} compartment={host['compartment_name']} "
                f"shape={host['shape']}"
            ),
        )
        for host in hosts
    ]
    options.append(("__new__", "Provision a new OCI host"))
    selected = prompt_choice("OpenClaw target host:", options)
    if selected == "__new__":
        return None
    for host in hosts:
        if host["instance_id"] == selected:
            return host
    raise RuntimeError("Selected host was not found")


def run_command(command: Sequence[str]) -> None:
    subprocess.run(command, check=True)


def bootstrap_existing_host(args: argparse.Namespace, host: Dict[str, Any]) -> int:
    args.ssh_user = prompt_text("SSH user", args.ssh_user)
    private_key_path = prompt_existing_path(
        "SSH private key path",
        args.ssh_private_key_path or default_private_key_path(args.ssh_key_path),
        prompt_even_if_exists=True,
    )
    bootstrap_script_path = Path(__file__).resolve().with_name("bootstrap_openclaw_host.sh")
    remote_target = f"{args.ssh_user}@{host['public_ip']}:{REMOTE_OPENCLAW_BOOTSTRAP_PATH}"
    ssh_target = f"{args.ssh_user}@{host['public_ip']}"

    print(
        "Bootstrapping existing host: "
        f"{host['display_name']} ({host['public_ip']}) in {host['region']}"
    )

    if args.dry_run:
        print(
            f"Dry run: existing host {host['display_name']} "
            f"public_ip={host['public_ip']} ssh_user={args.ssh_user}"
        )
        return 0

    run_command(
        [
            "scp",
            "-P",
            str(args.ssh_port),
            "-i",
            os.path.expanduser(private_key_path),
            str(bootstrap_script_path),
            remote_target,
        ]
    )
    remote_command = (
        f"chmod +x {shlex.quote(REMOTE_OPENCLAW_BOOTSTRAP_PATH)} && "
        f"sudo {shlex.quote(REMOTE_OPENCLAW_BOOTSTRAP_PATH)} "
        f"--openclaw-user {shlex.quote(args.openclaw_user)} "
        f"--openclaw-prefix {shlex.quote(args.openclaw_prefix)} "
        f"--openclaw-version {shlex.quote(args.openclaw_version)}"
    )
    run_command(
        [
            "ssh",
            "-t",
            "-p",
            str(args.ssh_port),
            "-i",
            os.path.expanduser(private_key_path),
            ssh_target,
            remote_command,
        ]
    )

    print("SUCCESS OPENCLAW BOOTSTRAPPED")
    print(f"Host: {host['display_name']}")
    print(f"Public IP: {host['public_ip']}")
    print(f"Region: {host['region']}")
    print("OpenClaw next steps:")
    print(f"- ssh to the instance as {args.ssh_user}")
    print(f"- switch user: sudo -iu {args.openclaw_user}")
    print("- run: openclaw onboard --install-daemon")
    print("- run: openclaw gateway status")
    return 0


def main() -> int:
    try:
        args = parse_args()
        if args.workers != 1:
            raise ValueError("Only --workers 1 is supported.")
        args.openclaw_prefix = resolve_openclaw_prefix(args)

        base_config = load_config(args.profile)

        identity = oci.identity.IdentityClient(base_config)
        subscriptions = get_region_subscriptions(identity, base_config["tenancy"])
        subscribed_regions = [item.region_name for item in subscriptions]
        home_region = get_home_region(subscriptions)

        if args.bootstrap and can_prompt():
            compartment_rows = list_compartments_with_root(identity, base_config["tenancy"])
            existing_hosts = collect_existing_hosts(
                base_config=base_config,
                compartment_rows=compartment_rows,
                subscribed_regions=subscribed_regions,
            )
            if existing_hosts:
                selected_host = choose_existing_host(existing_hosts)
                if selected_host is not None:
                    return bootstrap_existing_host(args, selected_host)
            else:
                print("No running OCI hosts with public IPs were found. Provisioning a new host.")

        if not args.compartment_id:
            compartment_name, args.compartment_id = choose_or_create_compartment(
                identity, base_config["tenancy"]
            )
            print(f"Using compartment: {compartment_name} ({args.compartment_id})")
        args.ssh_key_path = prompt_existing_path("SSH public key path", args.ssh_key_path)

        shapes = parse_shapes(args.shapes)
        ssh_key = load_ssh_key(args.ssh_key_path)
        location = resolve_location()
        ranked_regions = rank_regions_by_distance(
            subscribed_regions,
            float(location["latitude"]),
            float(location["longitude"]),
        )

        headroom = calculate_always_free_headroom(
            base_config=base_config,
            compartment_id=args.compartment_id,
            home_region=home_region,
        )
        safe_shapes = filter_shapes_for_headroom(
            requested_shapes=shapes,
            remaining_ocpus=headroom["remaining_ocpus"],
            remaining_memory_gb=headroom["remaining_memory_gb"],
        )

        if args.boot_volume_gb > headroom["remaining_volume_gb"]:
            raise RuntimeError(
                "Refusing to launch: requested boot volume exceeds remaining Always Free "
                "storage headroom."
            )

        if not safe_shapes:
            raise RuntimeError(
                "Refusing to launch: no requested A1 shape fits inside the remaining "
                "Always Free compute headroom."
            )

        candidate_regions = filter_regions_with_subnets(
            [home_region], args.subnet_id, DEFAULT_SUBNET_IDS
        )
        if not candidate_regions and not args.subnet_id:
            region_config = base_config.copy()
            region_config["region"] = home_region
            network = oci.core.VirtualNetworkClient(region_config)
            vcn = choose_or_create_vcn(network, args.compartment_id)
            print(f"Using VCN: {vcn.display_name} ({vcn.id})")
            subnet = choose_or_create_subnet(network, args.compartment_id, vcn)
            print(f"Using subnet: {subnet.display_name} ({subnet.id})")
            args.subnet_id = subnet.id
            candidate_regions = filter_regions_with_subnets(
                [home_region], args.subnet_id, DEFAULT_SUBNET_IDS
            )
        if not candidate_regions:
            raise RuntimeError(
                "No usable regions remain after subnet filtering. Provide --subnet-id or "
                "set OCI_SUBNET_ID_<REGION> for the home region."
            )

        describe_region_choice(
            home_region=home_region,
            ranked_regions=ranked_regions,
            location=location,
        )
        print(
            "Current Always Free headroom: "
            f"{headroom['remaining_ocpus']} OCPUs, "
            f"{headroom['remaining_memory_gb']} GB RAM, "
            f"{headroom['remaining_volume_gb']} GB block storage"
        )
        print(
            "Safe launch shapes after headroom check: "
            + ", ".join(f"{ocpu}x{memory_gb}" for ocpu, memory_gb in safe_shapes)
        )
        if args.bootstrap:
            print(
                "Bootstrap profile: OpenClaw CLI "
                f"({args.openclaw_version}) into {args.openclaw_prefix} "
                f"owned by {args.openclaw_user}"
            )

        if args.dry_run:
            subnet_id = resolve_subnet_id(home_region, args.subnet_id, DEFAULT_SUBNET_IDS)
            print(f"Dry run: region={home_region} subnet={subnet_id}")
            return 0

        cloud_init_data = None
        if args.bootstrap:
            cloud_init_data = build_openclaw_cloud_init(
                openclaw_prefix=args.openclaw_prefix,
                openclaw_version=args.openclaw_version,
                openclaw_user=args.openclaw_user,
                admin_ssh_user="ubuntu",
            )

        stop_event = multiprocessing.Event()
        result_queue: multiprocessing.Queue = multiprocessing.Queue()
        processes: List[multiprocessing.Process] = []

        print("Starting OpenClaw OCI provisioner...")

        for worker_id in range(args.workers):
            process = multiprocessing.Process(
                target=worker,
                args=(
                    worker_id,
                    args.profile,
                    args.compartment_id,
                    candidate_regions,
                    safe_shapes,
                    ssh_key,
                    args.subnet_id,
                    DEFAULT_SUBNET_IDS,
                    args.name_prefix,
                    args.boot_volume_gb,
                    args.bootstrap,
                    stop_event,
                    result_queue,
                    cloud_init_data,
                    "openclaw-gateway-host",
                ),
            )
            process.start()
            processes.append(process)

        result = None
        while result is None:
            try:
                result = result_queue.get(timeout=5)
            except queue.Empty:
                if not any(process.is_alive() for process in processes):
                    raise RuntimeError(
                        "All workers exited without creating an instance. "
                        "Check the subnet mapping, region subscription, "
                        "A1 availability, and quota limits."
                    )
        stop_event.set()

        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

        print("SUCCESS INSTANCE CREATED")
        print(f"Instance ID: {result['instance_id']}")
        print(f"Name: {result['display_name']}")
        print(f"Region: {result['region']}")
        print(f"Availability Domain: {result['availability_domain']}")
        print(f"Shape: {result['shape']}")
        print("Architecture: ARM64")
        if args.bootstrap:
            print("OpenClaw next steps:")
            print("- wait for cloud-init to finish")
            print("- ssh to the instance as ubuntu")
            print(f"- switch user: sudo -iu {args.openclaw_user}")
            print("- run: openclaw onboard --install-daemon")
            print("- run: openclaw gateway status")
        return 0

    except KeyboardInterrupt:
        print("Interrupted")
        return 130
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
