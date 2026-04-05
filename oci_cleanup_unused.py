#!/usr/bin/env python3
"""
Find and delete orphaned OCI resources no longer attached to any active instance.

Targets: unattached boot volumes, unattached block volumes, unassigned reserved
public IPs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, Sequence, Tuple

import oci

from oci_toolbox_common import (
    get_region_subscriptions,
    list_all,
    list_compartments_with_root,
    load_config,
)

from oci_account_reset import CleanupContext, describe_compartment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and delete orphaned OCI resources (volumes, public IPs)."
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit cleanup to the specified region. Repeatable.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete orphaned resources. Without this flag, dry-run only.",
    )
    return parser.parse_args()


def find_orphaned_boot_volumes(
    ctx: CleanupContext,
    compute: oci.core.ComputeClient,
    blockstorage: oci.core.BlockstorageClient,
    availability_domains: Sequence[str],
    compartment_id: str,
    compartment_label: str,
) -> None:
    for availability_domain in availability_domains:
        boot_volumes = [
            volume
            for volume in list_all(
                blockstorage.list_boot_volumes,
                compartment_id=compartment_id,
                availability_domain=availability_domain,
            )
            if volume.lifecycle_state not in {"TERMINATED", "TERMINATING"}
        ]
        for volume in boot_volumes:
            attachments = [
                attachment
                for attachment in list_all(
                    compute.list_boot_volume_attachments,
                    availability_domain=availability_domain,
                    compartment_id=compartment_id,
                    boot_volume_id=volume.id,
                )
                if attachment.lifecycle_state not in {"TERMINATED", "DETACHED"}
            ]
            if attachments:
                continue
            size = getattr(volume, "size_in_gbs", "?")
            ctx.run(
                f"Delete orphaned boot volume {volume.display_name or volume.id} "
                f"({size} GB) in {compartment_label}",
                lambda volume_id=volume.id: blockstorage.delete_boot_volume(volume_id),
            )


def find_orphaned_block_volumes(
    ctx: CleanupContext,
    compute: oci.core.ComputeClient,
    blockstorage: oci.core.BlockstorageClient,
    availability_domains: Sequence[str],
    compartment_id: str,
    compartment_label: str,
) -> None:
    for availability_domain in availability_domains:
        block_volumes = [
            volume
            for volume in list_all(
                blockstorage.list_volumes,
                compartment_id=compartment_id,
                availability_domain=availability_domain,
            )
            if volume.lifecycle_state not in {"TERMINATED", "TERMINATING"}
        ]
        for volume in block_volumes:
            attachments = [
                attachment
                for attachment in list_all(
                    compute.list_volume_attachments,
                    compartment_id=compartment_id,
                    volume_id=volume.id,
                )
                if attachment.lifecycle_state not in {"TERMINATED", "DETACHED"}
            ]
            if attachments:
                continue
            size = getattr(volume, "size_in_gbs", "?")
            ctx.run(
                f"Delete orphaned block volume {volume.display_name or volume.id} "
                f"({size} GB) in {compartment_label}",
                lambda volume_id=volume.id: blockstorage.delete_volume(volume_id),
            )


def find_unassigned_public_ips(
    ctx: CleanupContext,
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    compartment_label: str,
) -> None:
    public_ips = [
        public_ip
        for public_ip in list_all(
            network.list_public_ips,
            scope="REGION",
            compartment_id=compartment_id,
        )
        if getattr(public_ip, "lifecycle_state", "AVAILABLE") not in {"TERMINATED", "DELETING"}
        and getattr(public_ip, "lifetime", "") == "RESERVED"
        and not getattr(public_ip, "assigned_entity_id", None)
    ]
    for public_ip in public_ips:
        ctx.run(
            f"Delete unassigned reserved public IP "
            f"{public_ip.display_name or public_ip.id} "
            f"({getattr(public_ip, 'ip_address', '?')}) in {compartment_label}",
            lambda public_ip_id=public_ip.id: network.delete_public_ip(public_ip_id),
        )


def cleanup_region(
    ctx: CleanupContext,
    base_config: Dict[str, str],
    root_compartment_id: str,
    region_name: str,
    compartment_rows: Sequence[Tuple[str, str]],
) -> None:
    region_config = base_config.copy()
    region_config["region"] = region_name

    identity = oci.identity.IdentityClient(region_config)
    compute = oci.core.ComputeClient(region_config)
    network = oci.core.VirtualNetworkClient(region_config)
    blockstorage = oci.core.BlockstorageClient(region_config)

    print(f"\nRegion {region_name}")

    availability_domains = [
        ad.name for ad in identity.list_availability_domains(root_compartment_id).data
    ]

    for compartment_name, compartment_id in compartment_rows:
        label = describe_compartment(compartment_name, compartment_id)
        find_orphaned_boot_volumes(
            ctx, compute, blockstorage, availability_domains,
            compartment_id, label,
        )
        find_orphaned_block_volumes(
            ctx, compute, blockstorage, availability_domains,
            compartment_id, label,
        )
        find_unassigned_public_ips(ctx, network, compartment_id, label)


def main() -> int:
    args = parse_args()
    base_config = load_config(args.profile)
    root_compartment_id = base_config["tenancy"]
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, root_compartment_id)
    subscribed_regions = [item.region_name for item in subscriptions]
    target_regions = args.regions or subscribed_regions
    compartment_rows = list_compartments_with_root(identity, root_compartment_id)

    ctx = CleanupContext(execute=args.execute)

    print("OCI unused resource cleanup")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Target regions: {', '.join(target_regions)}")
    print(f"Compartments in scope: {len(compartment_rows)}")
    if not args.execute:
        print("No changes will be made. Re-run with --execute to delete orphaned resources.")

    for region_name in target_regions:
        cleanup_region(ctx, base_config, root_compartment_id, region_name, compartment_rows)

    if not ctx.actions:
        print("\nNo orphaned resources found.")
    else:
        print(f"\nPlanned actions: {len(ctx.actions)}")

    if ctx.failures:
        print(f"Failures: {len(ctx.failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
