#!/usr/bin/env python3
"""
Best-effort OCI account cleanup helper.

This script targets common user-created IaaS resources so the tenancy is close
to a just-created state:
- compute instances
- boot and block volumes
- reserved public IPs
- VCN networking resources created inside compartments
- optional child compartment deletion

It does not delete the root compartment, billing settings, IAM users/groups/
policies, quotas, budgets, or region subscriptions.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

import oci

from oci_toolbox_common import (
    get_home_region,
    get_region_subscriptions,
    list_all,
    load_config,
    prompt_delete_confirmation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or destroy common OCI resources across subscribed regions."
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit cleanup to the specified region. Repeatable.",
    )
    parser.add_argument(
        "--delete-child-compartments",
        action="store_true",
        help="Delete active child compartments after resource cleanup",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform deletions. Without this flag, the script is dry-run only.",
    )
    parser.add_argument(
        "--confirm",
        help='Required with --execute. Must equal "DELETE".',
    )
    return parser.parse_args()
def describe_compartment(name: str, compartment_id: str) -> str:
    return f"{name} ({compartment_id})"


def compartment_depth(compartment_by_id: Dict[str, oci.identity.models.Compartment], compartment_id: str) -> int:
    depth = 0
    current = compartment_by_id.get(compartment_id)
    while current and current.compartment_id in compartment_by_id:
        depth += 1
        current = compartment_by_id.get(current.compartment_id)
    return depth


class CleanupContext:
    def __init__(self, execute: bool) -> None:
        self.execute = execute
        self.actions: List[str] = []
        self.failures: List[str] = []

    def plan(self, message: str) -> None:
        print(message)
        self.actions.append(message)

    def run(self, message: str, action: Callable[[], object]) -> None:
        self.plan(message)
        if not self.execute:
            return
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            failure = f"{message} -> FAILED: {type(exc).__name__}: {exc}"
            print(failure, file=sys.stderr)
            self.failures.append(failure)


def get_active_compartments(
    identity: oci.identity.IdentityClient, root_compartment_id: str
) -> Tuple[List[oci.identity.models.Compartment], Dict[str, oci.identity.models.Compartment]]:
    compartments = [
        compartment
        for compartment in list_all(
            identity.list_compartments,
            compartment_id=root_compartment_id,
            compartment_id_in_subtree=True,
            access_level="ANY",
        )
        if compartment.lifecycle_state == "ACTIVE"
    ]
    by_id = {compartment.id: compartment for compartment in compartments}
    return compartments, by_id


def terminate_instances(
    ctx: CleanupContext,
    compute: oci.core.ComputeClient,
    compartment_id: str,
    compartment_label: str,
) -> None:
    instances = [
        instance
        for instance in list_all(compute.list_instances, compartment_id=compartment_id)
        if instance.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]

    for instance in instances:
        ctx.run(
            f"Terminate instance {instance.display_name or instance.id} in {compartment_label}",
            lambda instance_id=instance.id: compute.terminate_instance(
                instance_id,
                preserve_boot_volume=False,
            ),
        )


def wait_for_instances_terminated(
    compute: oci.core.ComputeClient,
    compartment_id: str,
    timeout_seconds: int = 180,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        active = [
            instance
            for instance in list_all(compute.list_instances, compartment_id=compartment_id)
            if instance.lifecycle_state not in {"TERMINATED"}
        ]
        if not active:
            return
        time.sleep(5)


def delete_block_and_boot_volumes(
    ctx: CleanupContext,
    compute: oci.core.ComputeClient,
    blockstorage: oci.core.BlockstorageClient,
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
    compartment_id: str,
    compartment_label: str,
) -> None:
    availability_domains = [
        ad.name for ad in identity.list_availability_domains(root_compartment_id).data
    ]

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
            ctx.run(
                f"Delete boot volume {volume.display_name or volume.id} in {compartment_label}",
                lambda volume_id=volume.id: blockstorage.delete_boot_volume(volume_id),
            )

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
            ctx.run(
                f"Delete block volume {volume.display_name or volume.id} in {compartment_label}",
                lambda volume_id=volume.id: blockstorage.delete_volume(volume_id),
            )


def delete_public_ips(
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
    ]
    for public_ip in public_ips:
        ctx.run(
            f"Delete reserved public IP {public_ip.display_name or public_ip.id} in {compartment_label}",
            lambda public_ip_id=public_ip.id: network.delete_public_ip(public_ip_id),
        )


def delete_vcns(
    ctx: CleanupContext,
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    compartment_label: str,
) -> None:
    vcns = [
        vcn
        for vcn in list_all(network.list_vcns, compartment_id=compartment_id)
        if vcn.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]

    for vcn in vcns:
        route_tables = [
            route_table
            for route_table in list_all(network.list_route_tables, compartment_id=compartment_id, vcn_id=vcn.id)
            if route_table.lifecycle_state not in {"TERMINATED", "TERMINATING"}
        ]
        for route_table in route_tables:
            if not getattr(route_table, "route_rules", None):
                continue
            ctx.run(
                f"Clear route rules from route table {route_table.display_name or route_table.id} in {compartment_label}",
                lambda route_table_id=route_table.id: network.update_route_table(
                    route_table_id,
                    oci.core.models.UpdateRouteTableDetails(route_rules=[]),
                ),
            )

        subnets = [
            subnet
            for subnet in list_all(network.list_subnets, compartment_id=compartment_id, vcn_id=vcn.id)
            if subnet.lifecycle_state not in {"TERMINATED", "TERMINATING"}
        ]
        for subnet in subnets:
            ctx.run(
                f"Delete subnet {subnet.display_name or subnet.id} in {compartment_label}",
                lambda subnet_id=subnet.id: network.delete_subnet(subnet_id),
            )

        if ctx.execute:
            deadline = time.time() + 120
            while time.time() < deadline:
                remaining_subnets = [
                    subnet
                    for subnet in list_all(
                        network.list_subnets,
                        compartment_id=compartment_id,
                        vcn_id=vcn.id,
                    )
                    if subnet.lifecycle_state not in {"TERMINATED"}
                ]
                if not remaining_subnets:
                    break
                time.sleep(3)

        for gateway in list_all(network.list_nat_gateways, compartment_id=compartment_id, vcn_id=vcn.id):
            if gateway.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete NAT gateway {gateway.display_name or gateway.id} in {compartment_label}",
                lambda gateway_id=gateway.id: network.delete_nat_gateway(gateway_id),
            )

        for gateway in list_all(network.list_service_gateways, compartment_id=compartment_id, vcn_id=vcn.id):
            if gateway.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete service gateway {gateway.display_name or gateway.id} in {compartment_label}",
                lambda gateway_id=gateway.id: network.delete_service_gateway(gateway_id),
            )

        for gateway in list_all(network.list_local_peering_gateways, compartment_id=compartment_id, vcn_id=vcn.id):
            if gateway.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete local peering gateway {gateway.display_name or gateway.id} in {compartment_label}",
                lambda gateway_id=gateway.id: network.delete_local_peering_gateway(gateway_id),
            )

        for attachment in list_all(network.list_drg_attachments, compartment_id=compartment_id, vcn_id=vcn.id):
            if attachment.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete DRG attachment {attachment.display_name or attachment.id} in {compartment_label}",
                lambda attachment_id=attachment.id: network.delete_drg_attachment(attachment_id),
            )

        for nsg in list_all(network.list_network_security_groups, compartment_id=compartment_id, vcn_id=vcn.id):
            if nsg.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete network security group {nsg.display_name or nsg.id} in {compartment_label}",
                lambda nsg_id=nsg.id: network.delete_network_security_group(nsg_id),
            )

        for route_table in route_tables:
            if route_table.id == vcn.default_route_table_id:
                continue
            if route_table.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete route table {route_table.display_name or route_table.id} in {compartment_label}",
                lambda route_table_id=route_table.id: network.delete_route_table(route_table_id),
            )

        for security_list in list_all(network.list_security_lists, compartment_id=compartment_id, vcn_id=vcn.id):
            if security_list.id == vcn.default_security_list_id:
                continue
            if security_list.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete security list {security_list.display_name or security_list.id} in {compartment_label}",
                lambda security_list_id=security_list.id: network.delete_security_list(security_list_id),
            )

        for gateway in list_all(network.list_internet_gateways, compartment_id=compartment_id, vcn_id=vcn.id):
            if gateway.lifecycle_state in {"TERMINATED", "TERMINATING"}:
                continue
            ctx.run(
                f"Delete internet gateway {gateway.display_name or gateway.id} in {compartment_label}",
                lambda gateway_id=gateway.id: network.delete_internet_gateway(gateway_id),
            )

        ctx.run(
            f"Delete VCN {vcn.display_name or vcn.id} in {compartment_label}",
            lambda vcn_id=vcn.id: network.delete_vcn(vcn_id),
        )


def delete_child_compartments(
    ctx: CleanupContext,
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
    compartments: Sequence[oci.identity.models.Compartment],
    compartment_by_id: Dict[str, oci.identity.models.Compartment],
) -> None:
    ordered = sorted(
        compartments,
        key=lambda compartment: compartment_depth(compartment_by_id, compartment.id),
        reverse=True,
    )
    for compartment in ordered:
        if compartment.id == root_compartment_id:
            continue
        ctx.run(
            f"Delete child compartment {describe_compartment(compartment.name, compartment.id)}",
            lambda compartment_id=compartment.id: identity.delete_compartment(compartment_id),
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

    for compartment_name, compartment_id in compartment_rows:
        label = describe_compartment(compartment_name, compartment_id)
        terminate_instances(ctx, compute, compartment_id, label)

    if ctx.execute:
        for _, compartment_id in compartment_rows:
            wait_for_instances_terminated(compute, compartment_id)

    for compartment_name, compartment_id in compartment_rows:
        label = describe_compartment(compartment_name, compartment_id)
        delete_block_and_boot_volumes(
            ctx,
            compute,
            blockstorage,
            identity,
            root_compartment_id,
            compartment_id,
            label,
        )
        delete_public_ips(ctx, network, compartment_id, label)
        delete_vcns(ctx, network, compartment_id, label)


def main() -> int:
    args = parse_args()
    if args.execute and args.confirm is None:
        try:
            args.confirm = prompt_delete_confirmation(
                'Type DELETE to confirm destructive cleanup'
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.execute and args.confirm != "DELETE":
        print('Refusing to execute without confirmation value DELETE', file=sys.stderr)
        return 2

    base_config = load_config(args.profile)
    root_compartment_id = base_config["tenancy"]
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, root_compartment_id)
    home_region = get_home_region(subscriptions)
    subscribed_regions = [item.region_name for item in subscriptions]
    target_regions = args.regions or subscribed_regions

    compartments, compartment_by_id = get_active_compartments(identity, root_compartment_id)
    compartment_rows: List[Tuple[str, str]] = [("ROOT", root_compartment_id)]
    compartment_rows.extend((compartment.name, compartment.id) for compartment in compartments)

    ctx = CleanupContext(execute=args.execute)

    print("OCI account reset helper")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Home region: {home_region}")
    print(f"Subscribed regions: {', '.join(subscribed_regions)}")
    print(f"Target regions: {', '.join(target_regions)}")
    print(f"Compartments in scope: {len(compartment_rows)}")
    if not args.execute:
        print('No changes will be made. Re-run with --execute --confirm DELETE to destroy resources.')

    for region_name in target_regions:
        cleanup_region(ctx, base_config, root_compartment_id, region_name, compartment_rows)

    if args.delete_child_compartments:
        delete_child_compartments(
            ctx,
            identity,
            root_compartment_id,
            compartments,
            compartment_by_id,
        )
    else:
        print("\nSkipping child compartment deletion. Use --delete-child-compartments to include them.")

    print(f"\nPlanned actions: {len(ctx.actions)}")
    print(f"Failures: {len(ctx.failures)}")
    if ctx.failures:
        print("Some resources could not be deleted. Review the failures above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
