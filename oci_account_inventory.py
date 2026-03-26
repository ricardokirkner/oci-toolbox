#!/usr/bin/env python3
"""
Report OCI resources across subscribed regions and compartments.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Sequence, Tuple

import oci

from oci_toolbox_common import get_home_region, get_region_subscriptions, list_all, load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory OCI resources across subscribed regions and compartments."
    )
    parser.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Limit inventory to the specified region. Repeatable.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human summary.",
    )
    return parser.parse_args()
def get_active_compartments(
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
) -> List[Tuple[str, str]]:
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
    rows = [("ROOT", root_compartment_id)]
    rows.extend((compartment.name or compartment.id, compartment.id) for compartment in compartments)
    return rows


def simplify_instance(instance: oci.core.models.Instance) -> Dict[str, Any]:
    shape_config = instance.shape_config
    return {
        "id": instance.id,
        "display_name": instance.display_name,
        "compartment_id": instance.compartment_id,
        "lifecycle_state": instance.lifecycle_state,
        "shape": instance.shape,
        "ocpus": getattr(shape_config, "ocpus", None) if shape_config else None,
        "memory_gb": getattr(shape_config, "memory_in_gbs", None) if shape_config else None,
        "availability_domain": instance.availability_domain,
        "image_id": instance.image_id,
    }


def simplify_vcn(vcn: oci.core.models.Vcn) -> Dict[str, Any]:
    return {
        "id": vcn.id,
        "display_name": vcn.display_name,
        "compartment_id": vcn.compartment_id,
        "lifecycle_state": vcn.lifecycle_state,
        "cidr_blocks": list(vcn.cidr_blocks or []),
        "dns_label": vcn.dns_label,
    }


def simplify_subnet(subnet: oci.core.models.Subnet) -> Dict[str, Any]:
    return {
        "id": subnet.id,
        "display_name": subnet.display_name,
        "compartment_id": subnet.compartment_id,
        "lifecycle_state": subnet.lifecycle_state,
        "vcn_id": subnet.vcn_id,
        "cidr_block": subnet.cidr_block,
        "prohibit_public_ip_on_vnic": subnet.prohibit_public_ip_on_vnic,
        "route_table_id": subnet.route_table_id,
        "security_list_ids": list(subnet.security_list_ids or []),
    }


def simplify_internet_gateway(gateway: oci.core.models.InternetGateway) -> Dict[str, Any]:
    return {
        "id": gateway.id,
        "display_name": gateway.display_name,
        "compartment_id": gateway.compartment_id,
        "lifecycle_state": gateway.lifecycle_state,
        "vcn_id": gateway.vcn_id,
        "is_enabled": gateway.is_enabled,
    }


def simplify_route_table(route_table: oci.core.models.RouteTable) -> Dict[str, Any]:
    return {
        "id": route_table.id,
        "display_name": route_table.display_name,
        "compartment_id": route_table.compartment_id,
        "lifecycle_state": route_table.lifecycle_state,
        "vcn_id": route_table.vcn_id,
        "route_rules": [
            {
                "destination": rule.destination,
                "destination_type": rule.destination_type,
                "network_entity_id": rule.network_entity_id,
            }
            for rule in (route_table.route_rules or [])
        ],
    }


def simplify_security_list(security_list: oci.core.models.SecurityList) -> Dict[str, Any]:
    return {
        "id": security_list.id,
        "display_name": security_list.display_name,
        "compartment_id": security_list.compartment_id,
        "lifecycle_state": security_list.lifecycle_state,
        "vcn_id": security_list.vcn_id,
        "ingress_rule_count": len(security_list.ingress_security_rules or []),
        "egress_rule_count": len(security_list.egress_security_rules or []),
    }


def simplify_nsg(nsg: oci.core.models.NetworkSecurityGroup) -> Dict[str, Any]:
    return {
        "id": nsg.id,
        "display_name": nsg.display_name,
        "compartment_id": nsg.compartment_id,
        "lifecycle_state": nsg.lifecycle_state,
        "vcn_id": nsg.vcn_id,
    }


def simplify_volume(volume: oci.core.models.Volume) -> Dict[str, Any]:
    return {
        "id": volume.id,
        "display_name": volume.display_name,
        "compartment_id": volume.compartment_id,
        "lifecycle_state": volume.lifecycle_state,
        "availability_domain": volume.availability_domain,
        "size_in_gbs": volume.size_in_gbs,
    }


def simplify_boot_volume(volume: oci.core.models.BootVolume) -> Dict[str, Any]:
    return {
        "id": volume.id,
        "display_name": volume.display_name,
        "compartment_id": volume.compartment_id,
        "lifecycle_state": volume.lifecycle_state,
        "availability_domain": volume.availability_domain,
        "size_in_gbs": volume.size_in_gbs,
        "image_id": volume.image_id,
    }


def simplify_public_ip(public_ip: oci.core.models.PublicIp) -> Dict[str, Any]:
    return {
        "id": public_ip.id,
        "display_name": public_ip.display_name,
        "compartment_id": public_ip.compartment_id,
        "lifecycle_state": public_ip.lifecycle_state,
        "ip_address": public_ip.ip_address,
        "lifetime": public_ip.lifetime,
        "scope": public_ip.scope,
        "assigned_entity_id": public_ip.assigned_entity_id,
    }


def collect_region_inventory(
    region_config: Dict[str, str],
    root_compartment_id: str,
    compartment_rows: Sequence[Tuple[str, str]],
) -> Dict[str, Any]:
    identity = oci.identity.IdentityClient(region_config)
    compute = oci.core.ComputeClient(region_config)
    network = oci.core.VirtualNetworkClient(region_config)
    blockstorage = oci.core.BlockstorageClient(region_config)

    availability_domains = [
        ad.name for ad in identity.list_availability_domains(root_compartment_id).data
    ]

    result: Dict[str, Any] = {
        "compartments": {},
        "totals": {
            "instances": 0,
            "vcns": 0,
            "subnets": 0,
            "internet_gateways": 0,
            "route_tables": 0,
            "security_lists": 0,
            "network_security_groups": 0,
            "block_volumes": 0,
            "boot_volumes": 0,
            "reserved_public_ips": 0,
        },
    }

    for compartment_name, compartment_id in compartment_rows:
        instances = [
            simplify_instance(instance)
            for instance in list_all(compute.list_instances, compartment_id=compartment_id)
            if instance.lifecycle_state not in {"TERMINATED"}
        ]
        vcns = [
            simplify_vcn(vcn)
            for vcn in list_all(network.list_vcns, compartment_id=compartment_id)
            if vcn.lifecycle_state not in {"TERMINATED"}
        ]
        subnets = [
            simplify_subnet(subnet)
            for subnet in list_all(network.list_subnets, compartment_id=compartment_id)
            if subnet.lifecycle_state not in {"TERMINATED"}
        ]
        internet_gateways = [
            simplify_internet_gateway(gateway)
            for gateway in list_all(network.list_internet_gateways, compartment_id=compartment_id)
            if gateway.lifecycle_state not in {"TERMINATED"}
        ]
        route_tables = [
            simplify_route_table(route_table)
            for route_table in list_all(network.list_route_tables, compartment_id=compartment_id)
            if route_table.lifecycle_state not in {"TERMINATED"}
        ]
        security_lists = [
            simplify_security_list(security_list)
            for security_list in list_all(network.list_security_lists, compartment_id=compartment_id)
            if security_list.lifecycle_state not in {"TERMINATED"}
        ]
        nsgs = [
            simplify_nsg(nsg)
            for nsg in list_all(network.list_network_security_groups, compartment_id=compartment_id)
            if nsg.lifecycle_state not in {"TERMINATED"}
        ]

        block_volumes: List[Dict[str, Any]] = []
        boot_volumes: List[Dict[str, Any]] = []
        for availability_domain in availability_domains:
            block_volumes.extend(
                simplify_volume(volume)
                for volume in list_all(
                    blockstorage.list_volumes,
                    compartment_id=compartment_id,
                    availability_domain=availability_domain,
                )
                if volume.lifecycle_state not in {"TERMINATED"}
            )
            boot_volumes.extend(
                simplify_boot_volume(volume)
                for volume in list_all(
                    blockstorage.list_boot_volumes,
                    compartment_id=compartment_id,
                    availability_domain=availability_domain,
                )
                if volume.lifecycle_state not in {"TERMINATED"}
            )

        reserved_public_ips = [
            simplify_public_ip(public_ip)
            for public_ip in list_all(
                network.list_public_ips,
                scope="REGION",
                compartment_id=compartment_id,
            )
            if getattr(public_ip, "lifecycle_state", "AVAILABLE") not in {"TERMINATED", "DELETING"}
            and getattr(public_ip, "lifetime", "") == "RESERVED"
        ]

        compartment_result = {
            "instances": instances,
            "vcns": vcns,
            "subnets": subnets,
            "internet_gateways": internet_gateways,
            "route_tables": route_tables,
            "security_lists": security_lists,
            "network_security_groups": nsgs,
            "block_volumes": block_volumes,
            "boot_volumes": boot_volumes,
            "reserved_public_ips": reserved_public_ips,
        }
        result["compartments"][compartment_name] = compartment_result

        result["totals"]["instances"] += len(instances)
        result["totals"]["vcns"] += len(vcns)
        result["totals"]["subnets"] += len(subnets)
        result["totals"]["internet_gateways"] += len(internet_gateways)
        result["totals"]["route_tables"] += len(route_tables)
        result["totals"]["security_lists"] += len(security_lists)
        result["totals"]["network_security_groups"] += len(nsgs)
        result["totals"]["block_volumes"] += len(block_volumes)
        result["totals"]["boot_volumes"] += len(boot_volumes)
        result["totals"]["reserved_public_ips"] += len(reserved_public_ips)

    return result


def print_human_summary(inventory: Dict[str, Any]) -> None:
    print(f"Home region: {inventory['home_region']}")
    print(f"Subscribed regions: {', '.join(inventory['subscribed_regions'])}")
    for region_name, region_data in inventory["regions"].items():
        totals = region_data["totals"]
        print(f"\nRegion {region_name}")
        print(
            "Totals: "
            f"instances={totals['instances']} "
            f"vcns={totals['vcns']} "
            f"subnets={totals['subnets']} "
            f"internet_gateways={totals['internet_gateways']} "
            f"route_tables={totals['route_tables']} "
            f"security_lists={totals['security_lists']} "
            f"nsgs={totals['network_security_groups']} "
            f"block_volumes={totals['block_volumes']} "
            f"boot_volumes={totals['boot_volumes']} "
            f"reserved_public_ips={totals['reserved_public_ips']}"
        )

        for compartment_name, compartment_data in region_data["compartments"].items():
            non_empty = {
                key: value for key, value in compartment_data.items() if value
            }
            if not non_empty:
                continue
            print(f"  Compartment {compartment_name}")
            for resource_type, items in non_empty.items():
                print(f"    {resource_type}: {len(items)}")
                for item in items[:5]:
                    name = item.get("display_name") or item.get("id")
                    print(f"      - {name} ({item.get('id')})")
                if len(items) > 5:
                    print(f"      ... {len(items) - 5} more")


def main() -> int:
    args = parse_args()
    base_config = load_config(args.profile)
    root_compartment_id = base_config["tenancy"]
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, root_compartment_id)
    home_region = get_home_region(subscriptions)
    subscribed_regions = [item.region_name for item in subscriptions]
    target_regions = args.regions or subscribed_regions
    compartment_rows = get_active_compartments(identity, root_compartment_id)

    inventory: Dict[str, Any] = {
        "home_region": home_region,
        "subscribed_regions": subscribed_regions,
        "regions": {},
    }

    for region_name in target_regions:
        region_config = base_config.copy()
        region_config["region"] = region_name
        inventory["regions"][region_name] = collect_region_inventory(
            region_config,
            root_compartment_id,
            compartment_rows,
        )

    if args.json:
        json.dump(inventory, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        print_human_summary(inventory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
