#!/usr/bin/env python3
"""
Terminate a single OCI compute instance by ID or interactive selection.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Sequence, Tuple

import oci

from oci_toolbox_common import (
    can_prompt,
    get_region_subscriptions,
    list_all,
    list_compartments_with_root,
    load_config,
    prompt_choice,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Terminate a single OCI compute instance."
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit instance listing to the specified region. Repeatable.",
    )
    parser.add_argument(
        "--instance-id",
        default=os.environ.get("INSTANCE_ID"),
        help="Instance OCID to terminate directly.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually terminate. Without this flag, dry-run only.",
    )
    return parser.parse_args()


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
        if attachment.lifecycle_state in {"TERMINATED", "DETACHING"}:
            continue
        try:
            vnic = network.get_vnic(attachment.vnic_id).data
        except oci.exceptions.ServiceError:
            continue
        if getattr(vnic, "public_ip", None):
            return str(vnic.public_ip)
    return None


def collect_instances(
    base_config: Dict[str, str],
    compartment_rows: Sequence[Tuple[str, str]],
    target_regions: Sequence[str],
) -> List[Dict[str, Any]]:
    instances: List[Dict[str, Any]] = []

    for region_name in target_regions:
        region_config = base_config.copy()
        region_config["region"] = region_name
        compute = oci.core.ComputeClient(region_config)
        network = oci.core.VirtualNetworkClient(region_config)

        for compartment_name, compartment_id in compartment_rows:
            for instance in list_all(compute.list_instances, compartment_id=compartment_id):
                if instance.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                    continue
                public_ip = get_instance_public_ip(compute, network, instance)
                shape_config = instance.shape_config
                instances.append(
                    {
                        "instance_id": instance.id,
                        "display_name": instance.display_name or instance.id,
                        "compartment_id": compartment_id,
                        "compartment_name": compartment_name,
                        "region": region_name,
                        "shape": instance.shape,
                        "ocpus": getattr(shape_config, "ocpus", None) if shape_config else None,
                        "memory_gb": getattr(shape_config, "memory_in_gbs", None) if shape_config else None,
                        "lifecycle_state": instance.lifecycle_state,
                        "public_ip": public_ip,
                    }
                )

    instances.sort(
        key=lambda item: (
            item["display_name"].lower(),
            item["region"],
            item["compartment_name"].lower(),
        )
    )
    return instances


def choose_instance(instances: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not instances:
        raise RuntimeError("No instances found.")
    if not can_prompt():
        raise RuntimeError("No --instance-id provided and stdin is not a terminal.")

    options = [
        (
            instance["instance_id"],
            (
                f"{instance['display_name']} ({instance['lifecycle_state']}) "
                f"region={instance['region']} compartment={instance['compartment_name']} "
                f"shape={instance['shape']}"
                + (f" ip={instance['public_ip']}" if instance["public_ip"] else "")
            ),
        )
        for instance in instances
    ]
    selected = prompt_choice("Instance to terminate:", options)
    for instance in instances:
        if instance["instance_id"] == selected:
            return instance
    raise RuntimeError("Selected instance was not found")


def fetch_instance_by_id(
    base_config: Dict[str, str],
    instance_id: str,
    compartment_rows: Sequence[Tuple[str, str]],
    target_regions: Sequence[str],
) -> Dict[str, Any]:
    for region_name in target_regions:
        region_config = base_config.copy()
        region_config["region"] = region_name
        compute = oci.core.ComputeClient(region_config)
        try:
            instance = compute.get_instance(instance_id).data
        except oci.exceptions.ServiceError:
            continue

        if instance.lifecycle_state in {"TERMINATED", "TERMINATING"}:
            raise RuntimeError(
                f"Instance {instance_id} is already {instance.lifecycle_state}."
            )

        network = oci.core.VirtualNetworkClient(region_config)
        public_ip = get_instance_public_ip(compute, network, instance)
        shape_config = instance.shape_config

        compartment_name = instance.compartment_id
        for name, cid in compartment_rows:
            if cid == instance.compartment_id:
                compartment_name = name
                break

        return {
            "instance_id": instance.id,
            "display_name": instance.display_name or instance.id,
            "compartment_id": instance.compartment_id,
            "compartment_name": compartment_name,
            "region": region_name,
            "shape": instance.shape,
            "ocpus": getattr(shape_config, "ocpus", None) if shape_config else None,
            "memory_gb": getattr(shape_config, "memory_in_gbs", None) if shape_config else None,
            "lifecycle_state": instance.lifecycle_state,
            "public_ip": public_ip,
        }

    raise RuntimeError(f"Instance {instance_id} not found in target regions.")


def wait_for_termination(
    compute: oci.core.ComputeClient,
    instance_id: str,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            instance = compute.get_instance(instance_id).data
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                return
            raise
        if instance.lifecycle_state == "TERMINATED":
            return
        time.sleep(5)
    print("Warning: timed out waiting for instance to reach TERMINATED state.", file=sys.stderr)


def main() -> int:
    args = parse_args()
    base_config = load_config(args.profile)
    root_compartment_id = base_config["tenancy"]
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, root_compartment_id)
    subscribed_regions = [item.region_name for item in subscriptions]
    target_regions = args.regions or subscribed_regions
    compartment_rows = list_compartments_with_root(identity, root_compartment_id)

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")

    if args.instance_id:
        selected = fetch_instance_by_id(
            base_config, args.instance_id, compartment_rows, target_regions
        )
    else:
        instances = collect_instances(base_config, compartment_rows, target_regions)
        selected = choose_instance(instances)

    print(f"\nInstance: {selected['display_name']}")
    print(f"  ID: {selected['instance_id']}")
    print(f"  Region: {selected['region']}")
    print(f"  Compartment: {selected['compartment_name']}")
    print(f"  Shape: {selected['shape']}")
    if selected["ocpus"]:
        print(f"  OCPUs: {selected['ocpus']}, Memory: {selected['memory_gb']} GB")
    if selected["public_ip"]:
        print(f"  Public IP: {selected['public_ip']}")
    print(f"  State: {selected['lifecycle_state']}")

    if not args.execute:
        print("\nDry run: instance would be terminated.")
        print("Re-run with --execute to terminate.")
        return 0

    region_config = base_config.copy()
    region_config["region"] = selected["region"]
    compute = oci.core.ComputeClient(region_config)

    print(f"\nTerminating instance {selected['display_name']}...")
    compute.terminate_instance(selected["instance_id"], preserve_boot_volume=False)
    wait_for_termination(compute, selected["instance_id"])
    print("Instance terminated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
