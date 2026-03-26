#!/usr/bin/env python3
"""
Provision an OCI A1 instance bootstrapped for OpenClaw.

This flow keeps the OCI-side logic intentionally narrow:
- Always Free-compatible home-region launches only
- Ubuntu ARM images on VM.Standard.A1.Flex
- OpenClaw CLI bootstrapped via cloud-init for the ubuntu user
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import queue
import sys
from typing import List

import oci

from oci_toolbox_common import (
    DEFAULT_COMPARTMENT_ID,
    DEFAULT_SUBNET_IDS,
    MIN_BOOT_VOLUME_GB,
    build_openclaw_cloud_init,
    calculate_always_free_headroom,
    describe_region_choice,
    filter_regions_with_subnets,
    filter_shapes_for_headroom,
    get_home_region,
    get_region_subscriptions,
    load_config,
    load_ssh_key,
    parse_shapes,
    rank_regions_by_distance,
    resolve_location,
    resolve_subnet_id,
    worker,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Launch an Always Free-friendly OCI A1 instance and preinstall the "
            "OpenClaw CLI for the ubuntu user."
        )
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--compartment-id",
        default=os.environ.get("OCI_COMPARTMENT_ID", DEFAULT_COMPARTMENT_ID),
        help="Compartment OCID used for images and instance creation",
    )
    parser.add_argument(
        "--ssh-key-path",
        default=os.path.expanduser("~/.ssh/id_rsa.pub"),
        help="Public SSH key injected into the instance",
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
        "--latitude",
        type=float,
        help="Latitude override for region ranking",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        help="Longitude override for region ranking",
    )
    parser.add_argument(
        "--local-timezone",
        default=os.environ.get("TZ", "America/Montevideo"),
        help="IANA timezone used when latitude/longitude are not provided",
    )
    parser.add_argument(
        "--openclaw-prefix",
        default="/home/ubuntu/.openclaw",
        help="Install prefix for the OpenClaw CLI on the instance",
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


def main() -> int:
    try:
        args = parse_args()
        if args.workers != 1:
            raise ValueError("Only --workers 1 is supported.")

        shapes = parse_shapes(args.shapes)
        ssh_key = load_ssh_key(args.ssh_key_path)
        base_config = load_config(args.profile)
        location = resolve_location(args)

        if not args.compartment_id:
            args.compartment_id = base_config.get("tenancy")

        identity = oci.identity.IdentityClient(base_config)
        subscriptions = get_region_subscriptions(identity, base_config["tenancy"])
        subscribed_regions = [item.region_name for item in subscriptions]
        home_region = get_home_region(subscriptions)
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
                f"({args.openclaw_version}) into {args.openclaw_prefix}"
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
