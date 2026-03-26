#!/usr/bin/env python3
"""
OCI A1 provisioner split into two parts:

1. suggest: recommend likely home regions for a new Always Free account
2. provision: launch into an explicitly chosen region on an existing account
"""

from __future__ import annotations

import argparse
import multiprocessing
import queue
import re
import sys
import time
from types import SimpleNamespace
from typing import Dict, List, Sequence, Tuple

import oci

from oci_toolbox_common import (
    DEFAULT_COMPARTMENT_ID,
    DEFAULT_SUBNET_IDS,
    MIN_BOOT_VOLUME_GB,
    REGION_COORDINATES,
    calculate_always_free_headroom,
    filter_regions_with_subnets,
    filter_shapes_for_headroom,
    get_home_region,
    get_region_subscriptions,
    haversine_km,
    load_config,
    load_ssh_key,
    parse_shapes,
    resolve_location,
    resolve_subnet_id,
    worker,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Suggest a new OCI home region or provision an OCI A1 instance."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    suggest = subparsers.add_parser(
        "suggest",
        help="Recommend likely home regions for a new Always Free account",
    )
    suggest.add_argument(
        "--latitude",
        type=float,
        help="Latitude override for region ranking",
    )
    suggest.add_argument(
        "--longitude",
        type=float,
        help="Longitude override for region ranking",
    )
    suggest.add_argument(
        "--local-timezone",
        default="America/Montevideo",
        help="IANA timezone used when latitude/longitude are not provided",
    )
    suggest.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of suggested regions to print",
    )
    suggest.add_argument(
        "--probe-capacity",
        action="store_true",
        help="Use the current OCI profile to probe live A1 capacity in subscribed regions",
    )
    suggest.add_argument(
        "--profile",
        default="DEFAULT",
        help="OCI CLI profile name used when --probe-capacity is enabled",
    )
    suggest.add_argument(
        "--shape",
        default="1x6",
        help="A1 shape to probe for live capacity, in OCPUxMemoryGB format",
    )

    provision = subparsers.add_parser(
        "provision",
        help="Provision an OCI A1 instance in a specified region",
    )
    provision.add_argument(
        "--billing-mode",
        choices=("always-free", "payg"),
        default="always-free",
        help="always-free enforces the tenancy home region; payg allows any subscribed region",
    )
    provision.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    provision.add_argument(
        "--compartment-id",
        required=True,
        help="Compartment OCID used for images and instance creation",
    )
    provision.add_argument(
        "--ssh-key-path",
        default="~/.ssh/id_rsa.pub",
        help="Public SSH key injected into the instance",
    )
    provision.add_argument(
        "--region",
        required=True,
        help="Explicit target region for the launch",
    )
    provision.add_argument(
        "--subnet-id",
        help="Subnet OCID override for the specified region",
    )
    provision.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Serial capacity-hunting worker count. Only 1 is supported.",
    )
    provision.add_argument(
        "--shapes",
        default="1x6,2x12,3x18,4x24",
        help="Shape attempts in OCPUxMemoryGB format, comma separated",
    )
    provision.add_argument(
        "--name-prefix",
        default="oci-a1",
        help="Display name prefix for the created instance",
    )
    provision.add_argument(
        "--boot-volume-gb",
        type=int,
        default=MIN_BOOT_VOLUME_GB,
        help="Boot volume size in GB",
    )
    provision.add_argument(
        "--latitude",
        type=float,
        help="Latitude override for reporting",
    )
    provision.add_argument(
        "--longitude",
        type=float,
        help="Longitude override for reporting",
    )
    provision.add_argument(
        "--local-timezone",
        default="America/Montevideo",
        help="IANA timezone used when latitude/longitude are not provided",
    )
    provision.add_argument(
        "--bootstrap",
        action="store_true",
        default=True,
        help="Install Docker, Git, Python and helper tooling via cloud-init",
    )
    provision.add_argument(
        "--no-bootstrap",
        action="store_false",
        dest="bootstrap",
        help="Disable cloud-init bootstrapping",
    )
    provision.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve region choice, subnet selection, and mode-specific checks, then exit",
    )

    setup = subparsers.add_parser(
        "setup-provision",
        help="Interactively select or create compartment, VCN, and public subnet, then provision",
    )
    setup.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    setup.add_argument(
        "--billing-mode",
        choices=("always-free", "payg"),
        default="always-free",
        help="always-free enforces the tenancy home region; payg allows any subscribed region",
    )
    setup.add_argument(
        "--region",
        help="Target region. Defaults to the home region in always-free mode.",
    )
    setup.add_argument(
        "--ssh-key-path",
        default="~/.ssh/id_rsa.pub",
        help="Public SSH key injected into the instance",
    )
    setup.add_argument(
        "--shapes",
        default="1x6,2x12,3x18,4x24",
        help="Shape attempts in OCPUxMemoryGB format, comma separated",
    )
    setup.add_argument(
        "--boot-volume-gb",
        type=int,
        default=MIN_BOOT_VOLUME_GB,
        help="Boot volume size in GB",
    )
    setup.add_argument(
        "--name-prefix",
        default="oci-a1",
        help="Display name prefix for the created instance",
    )
    setup.add_argument(
        "--local-timezone",
        default="America/Montevideo",
        help="IANA timezone used when latitude/longitude are not provided",
    )
    setup.add_argument(
        "--bootstrap",
        action="store_true",
        default=True,
        help="Install Docker, Git, Python and helper tooling via cloud-init",
    )
    setup.add_argument(
        "--no-bootstrap",
        action="store_false",
        dest="bootstrap",
        help="Disable cloud-init bootstrapping",
    )

    verify = subparsers.add_parser(
        "verify",
        help="Check whether an instance meets the documented Always Free conditions",
    )
    verify.add_argument("--profile", default="DEFAULT", help="OCI CLI profile name")
    verify.add_argument(
        "--compartment-id",
        required=True,
        help="Compartment OCID used for tenancy usage checks",
    )
    verify.add_argument(
        "--instance-id",
        help="Optional instance OCID to inspect. If omitted, only tenancy-wide usage is shown.",
    )

    return parser


def rank_public_regions(
    latitude: float,
    longitude: float,
) -> List[Tuple[str, float]]:
    ranked: List[Tuple[str, float]] = []
    for region_name, (region_lat, region_lon) in REGION_COORDINATES.items():
        ranked.append(
            (region_name, haversine_km(latitude, longitude, region_lat, region_lon))
        )
    ranked.sort(key=lambda item: item[1])
    return ranked


def handle_suggest(args: argparse.Namespace) -> int:
    location = resolve_location(args)
    ranked = rank_public_regions(
        float(location["latitude"]),
        float(location["longitude"]),
    )
    limit = max(args.limit, 1)
    capacity_by_region: Dict[str, Dict[str, object]] = {}

    if args.probe_capacity:
        shape = parse_shapes(args.shape)
        if len(shape) != 1:
            raise ValueError("--shape must describe exactly one shape for capacity probing")
        capacity_by_region = probe_capacity_by_region(args.profile, shape[0])

    ordered = order_suggestions(ranked, capacity_by_region) if args.probe_capacity else ranked

    print(f"Location hint: {location['label']}")
    print("Recommended home-region candidates for a new Always Free account:")
    for index, (region_name, distance_km) in enumerate(ordered[:limit], start=1):
        capacity = capacity_by_region.get(region_name)
        if capacity is None:
            print(f"{index}. {region_name} ({distance_km:.0f} km)")
            continue

        print(
            f"{index}. {region_name} ({distance_km:.0f} km) "
            f"status={capacity['status']} available_count={capacity['available_count']}"
        )

    if args.probe_capacity:
        print(
            "Capacity probe note: live A1 capacity is only available for regions subscribed on "
            "the current profile and can change between probe time and account signup."
        )
    else:
        print(
            "Note: this is a proximity-based recommendation only. It cannot predict Oracle signup "
            "availability or real-time A1 capacity in those regions."
        )
    return 0


def order_suggestions(
    ranked: Sequence[Tuple[str, float]],
    capacity_by_region: Dict[str, Dict[str, object]],
) -> List[Tuple[str, float]]:
    def sort_key(item: Tuple[str, float]) -> Tuple[int, int, float, str]:
        region_name, distance_km = item
        capacity = capacity_by_region.get(region_name)
        if capacity is None:
            return (1, 0, distance_km, region_name)

        available_count = int(capacity["available_count"])
        if available_count > 0:
            return (0, -available_count, distance_km, region_name)

        return (2, 0, distance_km, region_name)

    return sorted(ranked, key=sort_key)


def probe_capacity_by_region(
    profile: str,
    shape: Tuple[int, int],
) -> Dict[str, Dict[str, object]]:
    ocpu, memory_gb = shape
    base_config = load_config(profile)
    root_compartment_id = base_config["tenancy"]
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, root_compartment_id)
    results: Dict[str, Dict[str, object]] = {}

    for subscription in subscriptions:
        region_name = subscription.region_name
        region_config = base_config.copy()
        region_config["region"] = region_name
        region_identity = oci.identity.IdentityClient(region_config)
        compute = oci.core.ComputeClient(region_config)

        try:
            availability_domains = region_identity.list_availability_domains(
                root_compartment_id
            ).data
        except Exception as exc:
            results[region_name] = {
                "status": f"query-failed:{type(exc).__name__}",
                "available_count": 0,
            }
            continue

        total_available = 0
        statuses: List[str] = []
        for availability_domain in availability_domains:
            report = compute.create_compute_capacity_report(
                oci.core.models.CreateComputeCapacityReportDetails(
                    compartment_id=root_compartment_id,
                    availability_domain=availability_domain.name,
                    shape_availabilities=[
                        oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                            instance_shape="VM.Standard.A1.Flex",
                            instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                                ocpus=ocpu,
                                memory_in_gbs=memory_gb,
                            ),
                        )
                    ],
                )
            ).data

            for shape_availability in report.shape_availabilities:
                total_available += int(shape_availability.available_count or 0)
                statuses.append(shape_availability.availability_status or "UNKNOWN")

        summarized_status = "AVAILABLE" if total_available > 0 else summarize_statuses(statuses)
        results[region_name] = {
            "status": summarized_status,
            "available_count": total_available,
        }

    return results


def summarize_statuses(statuses: Sequence[str]) -> str:
    normalized = {status.upper() for status in statuses if status}
    if "OUT_OF_HOST_CAPACITY" in normalized:
        return "OUT_OF_HOST_CAPACITY"
    if "HARDWARE_NOT_SUPPORTED" in normalized:
        return "HARDWARE_NOT_SUPPORTED"
    if normalized:
        return ",".join(sorted(normalized))
    return "UNKNOWN"


def validate_target_region(
    billing_mode: str,
    target_region: str,
    subscribed_regions: Sequence[str],
    home_region: str,
) -> None:
    if target_region not in subscribed_regions:
        raise RuntimeError(
            f"Region {target_region} is not subscribed in this tenancy profile"
        )

    if billing_mode == "always-free" and target_region != home_region:
        raise RuntimeError(
            f"always-free mode requires the tenancy home region {home_region}; got {target_region}"
        )


def print_provision_summary(
    args: argparse.Namespace,
    location_label: str,
    home_region: str,
    subscribed_regions: Sequence[str],
) -> None:
    print(f"Location hint: {location_label}")
    print(f"Home region: {home_region}")
    print(f"Billing mode: {args.billing_mode}")
    print(f"Target region: {args.region}")
    print(f"Subscribed regions: {', '.join(subscribed_regions)}")
    print(f"Boot volume size: {args.boot_volume_gb} GB")


def prompt_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if value:
        return value
    if default is not None:
        return default
    raise RuntimeError(f"{prompt} is required")


def prompt_choice(prompt: str, options: Sequence[Tuple[str, str]], default_index: int = 1) -> str:
    print(prompt)
    for index, (_, label) in enumerate(options, start=1):
        print(f"{index}. {label}")
    raw = input(f"Select option [{default_index}]: ").strip()
    if not raw:
        chosen = default_index
    else:
        chosen = int(raw)
    if chosen < 1 or chosen > len(options):
        raise RuntimeError("Invalid selection")
    return options[chosen - 1][0]


def sanitize_dns_label(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", value.lower())
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"{fallback}{cleaned}"
    cleaned = cleaned[:15]
    if not cleaned:
        return fallback[:15]
    return cleaned


def wait_for_lifecycle(getter, state: str, timeout_seconds: int = 120) -> object:
    deadline = time.time() + timeout_seconds
    last = None
    while time.time() < deadline:
        try:
            last = getter().data
        except oci.exceptions.ServiceError as exc:
            # Newly created OCI resources, especially IAM compartments, can
            # briefly 404 before the read path catches up to the create path.
            if exc.status in {404, 409} or exc.code in {
                "NotAuthorizedOrNotFound",
                "IncorrectState",
            }:
                time.sleep(2)
                continue
            raise
        if getattr(last, "lifecycle_state", None) == state:
            return last
        time.sleep(2)
    if last is None:
        raise RuntimeError(f"Timed out waiting for resource to reach {state}")
    return last


def list_compartments_with_root(
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
) -> List[Tuple[str, str]]:
    compartments = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        compartment_id=root_compartment_id,
        compartment_id_in_subtree=True,
        access_level="ANY",
    ).data
    items = [("ROOT", root_compartment_id)]
    items.extend(
        (compartment.name or compartment.id, compartment.id)
        for compartment in compartments
        if compartment.lifecycle_state == "ACTIVE"
    )
    return items


def choose_or_create_compartment(
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
) -> Tuple[str, str]:
    compartments = list_compartments_with_root(identity, root_compartment_id)
    options = [(compartment_id, f"{name} ({compartment_id})") for name, compartment_id in compartments]
    options.append(("__create__", "Create new child compartment"))
    selected = prompt_choice("Compartment:", options)
    if selected != "__create__":
        for name, compartment_id in compartments:
            if compartment_id == selected:
                return name, compartment_id

    name = prompt_text("New compartment name", "alwaysfree")
    description = prompt_text("New compartment description", "Always Free lab")
    created = identity.create_compartment(
        oci.identity.models.CreateCompartmentDetails(
            compartment_id=root_compartment_id,
            name=name,
            description=description,
        )
    ).data
    compartment = wait_for_lifecycle(
        lambda: identity.get_compartment(created.id),
        "ACTIVE",
    )
    return compartment.name, compartment.id


def list_vcns(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
) -> List[oci.core.models.Vcn]:
    return [
        vcn
        for vcn in oci.pagination.list_call_get_all_results(
            network.list_vcns,
            compartment_id=compartment_id,
        ).data
        if vcn.lifecycle_state == "AVAILABLE"
    ]


def choose_or_create_vcn(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
) -> oci.core.models.Vcn:
    vcns = list_vcns(network, compartment_id)
    options = [(vcn.id, f"{vcn.display_name or vcn.id} ({vcn.id})") for vcn in vcns]
    options.append(("__create__", "Create new VCN"))
    selected = prompt_choice("VCN:", options)
    if selected != "__create__":
        return network.get_vcn(selected).data

    display_name = prompt_text("New VCN display name", "always-free-vcn")
    cidr_block = prompt_text("VCN CIDR block", "10.0.0.0/16")
    dns_label = prompt_text("VCN DNS label", sanitize_dns_label(display_name, "toolboxnet"))
    created = network.create_vcn(
        oci.core.models.CreateVcnDetails(
            compartment_id=compartment_id,
            display_name=display_name,
            cidr_blocks=[cidr_block],
            dns_label=dns_label,
        )
    ).data
    return wait_for_lifecycle(lambda: network.get_vcn(created.id), "AVAILABLE")


def ensure_internet_gateway(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    vcn_id: str,
) -> oci.core.models.InternetGateway:
    gateways = [
        gateway
        for gateway in oci.pagination.list_call_get_all_results(
            network.list_internet_gateways,
            compartment_id=compartment_id,
            vcn_id=vcn_id,
        ).data
        if gateway.lifecycle_state == "AVAILABLE" and gateway.is_enabled
    ]
    if gateways:
        return gateways[0]

    return network.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment_id,
            display_name="public-igw",
            is_enabled=True,
            vcn_id=vcn_id,
        )
    ).data


def create_public_route_table(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    vcn_id: str,
    internet_gateway_id: str,
) -> oci.core.models.RouteTable:
    return network.create_route_table(
        oci.core.models.CreateRouteTableDetails(
            compartment_id=compartment_id,
            display_name="public-route-table",
            vcn_id=vcn_id,
            route_rules=[
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    destination_type="CIDR_BLOCK",
                    network_entity_id=internet_gateway_id,
                )
            ],
        )
    ).data


def create_public_security_list(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    vcn_id: str,
    ssh_cidr: str,
) -> oci.core.models.SecurityList:
    return network.create_security_list(
        oci.core.models.CreateSecurityListDetails(
            compartment_id=compartment_id,
            display_name="public-security-list",
            vcn_id=vcn_id,
            ingress_security_rules=[
                oci.core.models.IngressSecurityRule(
                    protocol="6",
                    source=ssh_cidr,
                    source_type="CIDR_BLOCK",
                    tcp_options=oci.core.models.TcpOptions(
                        destination_port_range=oci.core.models.PortRange(min=22, max=22)
                    ),
                    description="Allow SSH",
                )
            ],
            egress_security_rules=[
                oci.core.models.EgressSecurityRule(
                    protocol="all",
                    destination="0.0.0.0/0",
                    destination_type="CIDR_BLOCK",
                    description="Allow all egress",
                )
            ],
        )
    ).data


def list_subnets(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    vcn_id: str,
) -> List[oci.core.models.Subnet]:
    return [
        subnet
        for subnet in oci.pagination.list_call_get_all_results(
            network.list_subnets,
            compartment_id=compartment_id,
            vcn_id=vcn_id,
        ).data
        if subnet.lifecycle_state == "AVAILABLE"
    ]


def choose_or_create_subnet(
    network: oci.core.VirtualNetworkClient,
    compartment_id: str,
    vcn: oci.core.models.Vcn,
) -> oci.core.models.Subnet:
    subnets = list_subnets(network, compartment_id, vcn.id)
    options = [(subnet.id, f"{subnet.display_name or subnet.id} ({subnet.id})") for subnet in subnets]
    options.append(("__create__", "Create new public subnet"))
    selected = prompt_choice("Subnet:", options)
    if selected != "__create__":
        subnet = network.get_subnet(selected).data
        if subnet.prohibit_public_ip_on_vnic:
            raise RuntimeError(
                "Selected subnet prohibits public IPs on VNICs. Choose another subnet or create a new public subnet."
            )
        return subnet

    display_name = prompt_text("New subnet display name", "public-subnet")
    cidr_block = prompt_text("Subnet CIDR block", "10.0.1.0/24")
    dns_label = prompt_text("Subnet DNS label", sanitize_dns_label(display_name, "publicsubnet"))
    ssh_cidr = prompt_text("SSH ingress CIDR", "0.0.0.0/0")
    internet_gateway = ensure_internet_gateway(network, compartment_id, vcn.id)
    route_table = create_public_route_table(network, compartment_id, vcn.id, internet_gateway.id)
    security_list = create_public_security_list(network, compartment_id, vcn.id, ssh_cidr)
    created = network.create_subnet(
        oci.core.models.CreateSubnetDetails(
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            display_name=display_name,
            cidr_block=cidr_block,
            dns_label=dns_label,
            prohibit_public_ip_on_vnic=False,
            route_table_id=route_table.id,
            security_list_ids=[security_list.id],
        )
    ).data
    return wait_for_lifecycle(lambda: network.get_subnet(created.id), "AVAILABLE")


def find_instance_across_regions(
    base_config: Dict[str, str],
    subscribed_regions: Sequence[str],
    instance_id: str,
) -> Tuple[str, oci.core.models.Instance]:
    for region_name in subscribed_regions:
        region_config = base_config.copy()
        region_config["region"] = region_name
        compute = oci.core.ComputeClient(region_config)
        try:
            instance = compute.get_instance(instance_id).data
            return region_name, instance
        except oci.exceptions.ServiceError as exc:
            if exc.status == 404:
                continue
            raise

    raise RuntimeError(f"Instance {instance_id} was not found in any subscribed region")


def get_instance_boot_volume_size_gb(
    base_config: Dict[str, str],
    region_name: str,
    instance: oci.core.models.Instance,
) -> int | None:
    region_config = base_config.copy()
    region_config["region"] = region_name
    compute = oci.core.ComputeClient(region_config)
    blockstorage = oci.core.BlockstorageClient(region_config)

    attachments = oci.pagination.list_call_get_all_results(
        compute.list_boot_volume_attachments,
        availability_domain=instance.availability_domain,
        compartment_id=instance.compartment_id,
        instance_id=instance.id,
    ).data

    active_attachments = [
        attachment
        for attachment in attachments
        if attachment.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]
    if not active_attachments:
        return None

    boot_volume = blockstorage.get_boot_volume(active_attachments[0].boot_volume_id).data
    return int(boot_volume.size_in_gbs or 0)


def get_instance_image_details(
    base_config: Dict[str, str],
    region_name: str,
    image_id: str,
) -> oci.core.models.Image:
    region_config = base_config.copy()
    region_config["region"] = region_name
    compute = oci.core.ComputeClient(region_config)
    return compute.get_image(image_id).data


def is_always_free_compatible_image(image: oci.core.models.Image) -> bool:
    compatible_operating_systems = {
        "Canonical Ubuntu",
        "Oracle Linux",
    }
    return image.operating_system in compatible_operating_systems


def handle_verify(args: argparse.Namespace) -> int:
    base_config = load_config(args.profile)
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, base_config["tenancy"])
    subscribed_regions = [item.region_name for item in subscriptions]
    home_region = get_home_region(subscriptions)
    tenancy_compartment_id = base_config["tenancy"]
    headroom = calculate_always_free_headroom(
        base_config=base_config,
        compartment_id=tenancy_compartment_id,
        home_region=home_region,
    )

    total_ocpus = headroom["used_ocpus"]
    total_memory_gb = headroom["used_memory_gb"]
    total_volume_gb = headroom["used_volume_gb"]

    print(f"Home region: {home_region}")
    print(f"Subscribed regions: {', '.join(subscribed_regions)}")
    print(
        "Tenancy A1 usage: "
        f"{total_ocpus}/{4} OCPUs, "
        f"{total_memory_gb}/{24} GB RAM, "
        f"{total_volume_gb}/{200} GB boot+block volume"
    )
    print(
        "Tenancy free-envelope status: "
        + (
            "inside documented Always Free limits"
            if total_ocpus <= 4 and total_memory_gb <= 24 and total_volume_gb <= 200
            else "outside documented Always Free limits"
        )
    )

    if not args.instance_id:
        return 0

    region_name, instance = find_instance_across_regions(
        base_config=base_config,
        subscribed_regions=subscribed_regions,
        instance_id=args.instance_id,
    )
    image = get_instance_image_details(base_config, region_name, instance.image_id)
    boot_volume_gb = get_instance_boot_volume_size_gb(base_config, region_name, instance)

    shape_config = instance.shape_config
    instance_ocpus = int(shape_config.ocpus or 0) if shape_config else 0
    instance_memory_gb = int(shape_config.memory_in_gbs or 0) if shape_config else 0
    region_ok = region_name == home_region
    shape_ok = instance.shape == "VM.Standard.A1.Flex"
    image_ok = is_always_free_compatible_image(image)
    boot_volume_ok = boot_volume_gb is None or boot_volume_gb <= 200

    print(f"Instance region: {region_name}")
    print(f"Instance lifecycle state: {instance.lifecycle_state}")
    print(f"Instance shape: {instance.shape}")
    print(f"Instance shape config: {instance_ocpus} OCPUs / {instance_memory_gb} GB")
    print(f"Image OS: {image.operating_system} {image.operating_system_version}")
    print(
        "Boot volume size: "
        + (f"{boot_volume_gb} GB" if boot_volume_gb is not None else "unknown")
    )

    print("Instance eligibility checks:")
    print(f"- Home region match: {'yes' if region_ok else 'no'}")
    print(f"- A1 shape: {'yes' if shape_ok else 'no'}")
    print(f"- Compatible image: {'yes' if image_ok else 'no'}")
    print(f"- Boot volume <= 200 GB: {'yes' if boot_volume_ok else 'no'}")

    likely_eligible = region_ok and shape_ok and image_ok and boot_volume_ok
    print(
        "Instance eligibility verdict: "
        + (
            "meets the documented Always Free conditions"
            if likely_eligible
            else "does not meet the documented Always Free conditions"
        )
    )
    print(
        "Tenancy billing verdict: "
        + (
            "currently inside the documented free envelope"
            if total_ocpus <= 4 and total_memory_gb <= 24 and total_volume_gb <= 200
            else "currently outside the documented free envelope"
        )
    )
    return 0


def handle_provision(args: argparse.Namespace) -> int:
    if args.workers != 1:
        raise RuntimeError(
            "This provisioner now runs serially to avoid multiple concurrent launches. "
            "Use --workers 1."
        )

    shapes = parse_shapes(args.shapes)
    ssh_key = load_ssh_key(args.ssh_key_path)
    base_config = load_config(args.profile)
    location = resolve_location(args)

    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, base_config["tenancy"])
    subscribed_regions = [item.region_name for item in subscriptions]
    home_region = get_home_region(subscriptions)
    tenancy_compartment_id = base_config["tenancy"]

    validate_target_region(
        billing_mode=args.billing_mode,
        target_region=args.region,
        subscribed_regions=subscribed_regions,
        home_region=home_region,
    )

    candidate_regions = filter_regions_with_subnets(
        [args.region], args.subnet_id, DEFAULT_SUBNET_IDS
    )
    if not candidate_regions:
        raise RuntimeError(
            "No usable subnet mapping exists for the requested region. Provide --subnet-id or "
            "set OCI_SUBNET_ID_<REGION> for that region."
        )

    print_provision_summary(
        args=args,
        location_label=str(location["label"]),
        home_region=home_region,
        subscribed_regions=subscribed_regions,
    )

    launch_shapes = list(shapes)
    if args.billing_mode == "always-free":
        headroom = calculate_always_free_headroom(
            base_config=base_config,
            compartment_id=tenancy_compartment_id,
            home_region=home_region,
        )
        if args.boot_volume_gb > headroom["remaining_volume_gb"]:
            raise RuntimeError(
                "Refusing to launch: requested boot volume exceeds remaining Always Free "
                "storage headroom."
            )

        launch_shapes = filter_shapes_for_headroom(
            requested_shapes=shapes,
            remaining_ocpus=headroom["remaining_ocpus"],
            remaining_memory_gb=headroom["remaining_memory_gb"],
        )
        if not launch_shapes:
            raise RuntimeError(
                "Refusing to launch: no requested A1 shape fits inside the remaining "
                "Always Free compute headroom."
            )

        print(
            "Current Always Free headroom: "
            f"{headroom['remaining_ocpus']} OCPUs, "
            f"{headroom['remaining_memory_gb']} GB RAM, "
            f"{headroom['remaining_volume_gb']} GB block storage"
        )
        print(
            "Safe launch shapes after headroom check: "
            + ", ".join(f"{ocpu}x{memory_gb}" for ocpu, memory_gb in launch_shapes)
        )
    else:
        print(
            "Cost note: payg mode can incur charges, especially outside the home region."
        )

    if args.dry_run:
        subnet_id = resolve_subnet_id(args.region, args.subnet_id, DEFAULT_SUBNET_IDS)
        print(f"Dry run: region={args.region} subnet={subnet_id}")
        return 0

    stop_event = multiprocessing.Event()
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    processes: List[multiprocessing.Process] = []

    print("Starting OCI A1 provisioner in serial mode...")
    for worker_id in range(args.workers):
        process = multiprocessing.Process(
            target=worker,
            args=(
                worker_id,
                args.profile,
                args.compartment_id,
                candidate_regions,
                launch_shapes,
                ssh_key,
                args.subnet_id,
                DEFAULT_SUBNET_IDS,
                args.name_prefix,
                args.boot_volume_gb,
                args.bootstrap,
                stop_event,
                result_queue,
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
                    "Check region subscription, subnet mapping, A1 availability, and quotas."
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
    print(f"Billing mode: {args.billing_mode}")
    return 0


def handle_setup_provision(args: argparse.Namespace) -> int:
    base_config = load_config(args.profile)
    identity = oci.identity.IdentityClient(base_config)
    subscriptions = get_region_subscriptions(identity, base_config["tenancy"])
    subscribed_regions = [item.region_name for item in subscriptions]
    home_region = get_home_region(subscriptions)

    default_region = home_region if args.billing_mode == "always-free" else subscribed_regions[0]
    target_region = prompt_text("Target region", args.region or default_region)
    validate_target_region(args.billing_mode, target_region, subscribed_regions, home_region)

    region_config = base_config.copy()
    region_config["region"] = target_region
    network = oci.core.VirtualNetworkClient(region_config)

    print(f"Billing mode: {args.billing_mode}")
    print(f"Home region: {home_region}")
    print(f"Target region: {target_region}")

    compartment_name, compartment_id = choose_or_create_compartment(
        identity, base_config["tenancy"]
    )
    print(f"Using compartment: {compartment_name} ({compartment_id})")

    vcn = choose_or_create_vcn(network, compartment_id)
    print(f"Using VCN: {vcn.display_name} ({vcn.id})")

    subnet = choose_or_create_subnet(network, compartment_id, vcn)
    print(f"Using subnet: {subnet.display_name} ({subnet.id})")

    ssh_key_path = prompt_text("SSH public key path", args.ssh_key_path)
    shapes = prompt_text("A1 shape attempts", args.shapes)
    boot_volume_gb = int(prompt_text("Boot volume size (GB)", str(args.boot_volume_gb)))
    name_prefix = prompt_text("Instance name prefix", args.name_prefix)
    bootstrap = (
        prompt_text(
            "Bootstrap host with Docker/Python? (y/n)",
            "y" if args.bootstrap else "n",
        ).lower()
        in {"y", "yes"}
    )

    print("Provision summary:")
    print(f"- billing mode: {args.billing_mode}")
    print(f"- region: {target_region}")
    print(f"- compartment: {compartment_name}")
    print(f"- vcn: {vcn.display_name}")
    print(f"- subnet: {subnet.display_name}")
    print(f"- shapes: {shapes}")
    print(f"- boot volume: {boot_volume_gb} GB")
    if prompt_text("Proceed with launch? (y/n)", "y").lower() not in {"y", "yes"}:
        print("Aborted before launch.")
        return 0

    provision_args = SimpleNamespace(
        billing_mode=args.billing_mode,
        profile=args.profile,
        compartment_id=compartment_id,
        ssh_key_path=ssh_key_path,
        region=target_region,
        subnet_id=subnet.id,
        workers=1,
        shapes=shapes,
        name_prefix=name_prefix,
        boot_volume_gb=boot_volume_gb,
        latitude=None,
        longitude=None,
        local_timezone=args.local_timezone,
        bootstrap=bootstrap,
        dry_run=False,
    )
    return handle_provision(provision_args)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "suggest":
            return handle_suggest(args)
        if args.command == "provision":
            return handle_provision(args)
        if args.command == "verify":
            return handle_verify(args)
        if args.command == "setup-provision":
            return handle_setup_provision(args)
        parser.error(f"Unsupported command: {args.command}")
        return 2
    except KeyboardInterrupt:
        print("Interrupted")
        return 130
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
