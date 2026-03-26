#!/usr/bin/env python3
"""
Shared helpers for the OCI toolbox scripts.
"""

from __future__ import annotations

import base64
import math
import multiprocessing
import os
import random
import time
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

import oci


DEFAULT_COMPARTMENT_ID = (
    "<REDACTED-TENANCY-OCID>"
)

DEFAULT_SUBNET_IDS = {
    "us-ashburn-1": (
        "<REDACTED-SUBNET-OCID>"
    ),
}

DEFAULT_LOCATION = {
    "label": "<REDACTED>",
    "latitude": <REDACTED>,
    "longitude": <REDACTED>,
}

TIMEZONE_LOCATIONS = {
    "America/Montevideo": DEFAULT_LOCATION,
}

REGION_COORDINATES = {
    "sa-santiago-1": (-33.4489, -70.6693),
    "sa-saopaulo-1": (-23.5505, -46.6333),
    "sa-vinhedo-1": (-23.0305, -46.9759),
    "sa-bogota-1": (4.7110, -74.0721),
    "mx-queretaro-1": (20.5888, -100.3899),
    "us-ashburn-1": (39.0438, -77.4874),
    "us-phoenix-1": (33.4484, -112.0740),
    "us-sanjose-1": (37.3382, -121.8863),
    "ca-montreal-1": (45.5019, -73.5674),
    "ca-toronto-1": (43.6532, -79.3832),
    "eu-frankfurt-1": (50.1109, 8.6821),
    "eu-madrid-1": (40.4168, -3.7038),
}

UBUNTU_VERSIONS = ("24.04", "22.04")
DEFAULT_SHAPES = ((1, 6), (2, 12), (3, 18), (4, 24))
BASE_RETRY_SECONDS = 10
MAX_BACKOFF_SECONDS = 120
IMAGE_CACHE_SECONDS = 900
ALWAYS_FREE_MAX_OCPUS = 4
ALWAYS_FREE_MAX_MEMORY_GB = 24
ALWAYS_FREE_MAX_VOLUME_GB = 200
MIN_BOOT_VOLUME_GB = 50
TOOLBOX_STATE_DIR = "/var/lib/oci-toolbox"
BOOTSTRAP_NOTES_PATH = f"{TOOLBOX_STATE_DIR}/bootstrap-notes.txt"


def list_all(func: Callable[..., Any], **kwargs: Any) -> List[Any]:
    return oci.pagination.list_call_get_all_results(func, **kwargs).data


def parse_shapes(shape_text: str) -> List[Tuple[int, int]]:
    shapes: List[Tuple[int, int]] = []
    for chunk in shape_text.split(","):
        item = chunk.strip().lower()
        if not item:
            continue
        if "x" not in item:
            raise ValueError(f"Invalid shape definition: {chunk!r}")
        ocpu_text, memory_text = item.split("x", 1)
        ocpu = int(ocpu_text)
        memory = int(memory_text)
        if ocpu <= 0 or memory <= 0:
            raise ValueError(f"Shape values must be positive: {chunk!r}")
        shapes.append((ocpu, memory))
    if not shapes:
        raise ValueError("At least one shape must be configured")
    return shapes


def load_ssh_key(path: str) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise FileNotFoundError(f"SSH key not found: {expanded}")
    with open(expanded, encoding="utf-8") as handle:
        return handle.read().strip()


def load_config(profile: str) -> Dict[str, str]:
    return oci.config.from_file(profile_name=profile)


def get_region_subscriptions(
    identity_client: oci.identity.IdentityClient, tenancy_id: str
) -> Sequence[oci.identity.models.RegionSubscription]:
    return identity_client.list_region_subscriptions(tenancy_id).data


def get_home_region(subscriptions: Sequence[oci.identity.models.RegionSubscription]) -> str:
    for subscription in subscriptions:
        if subscription.is_home_region:
            return subscription.region_name
    raise RuntimeError("Could not determine the tenancy home region")


def haversine_km(
    latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float
) -> float:
    radius_km = 6371.0
    lat1 = math.radians(latitude_1)
    lon1 = math.radians(longitude_1)
    lat2 = math.radians(latitude_2)
    lon2 = math.radians(longitude_2)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(hav))


def resolve_location(args: Any) -> Dict[str, float | str]:
    if args.latitude is not None or args.longitude is not None:
        if args.latitude is None or args.longitude is None:
            raise ValueError("Both --latitude and --longitude are required together")
        return {
            "label": f"custom ({args.latitude:.4f}, {args.longitude:.4f})",
            "latitude": args.latitude,
            "longitude": args.longitude,
        }

    if args.local_timezone in TIMEZONE_LOCATIONS:
        return TIMEZONE_LOCATIONS[args.local_timezone]

    return DEFAULT_LOCATION


def rank_regions_by_distance(
    subscribed_regions: Iterable[str], latitude: float, longitude: float
) -> List[str]:
    known: List[Tuple[float, str]] = []
    unknown: List[str] = []

    for region in subscribed_regions:
        coordinates = REGION_COORDINATES.get(region)
        if coordinates is None:
            unknown.append(region)
            continue
        known.append((haversine_km(latitude, longitude, coordinates[0], coordinates[1]), region))

    known.sort(key=lambda item: item[0])
    return [region for _, region in known] + sorted(unknown)


def build_cloud_init() -> str:
    return f"""#cloud-config
package_update: true
package_upgrade: true
write_files:
  - path: {BOOTSTRAP_NOTES_PATH}
    permissions: "0644"
    content: |
      Provisioned by oci-toolbox
      Architecture: ARM64 (OCI VM.Standard.A1.Flex)
      This host is prepared for ARM-compatible Docker/Python workloads.
runcmd:
  - mkdir -p {TOOLBOX_STATE_DIR}
  - export DEBIAN_FRONTEND=noninteractive
  - apt-get update
  - apt-get install -y docker.io git python3 python3-pip curl unzip ca-certificates jq
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
  - printf '%s\n' 'Docker:' \"$(docker --version)\" >> {BOOTSTRAP_NOTES_PATH}
  - printf '%s\n' 'Python:' \"$(python3 --version)\" >> {BOOTSTRAP_NOTES_PATH}
"""


def get_latest_ubuntu_image(
    compute_client: oci.core.ComputeClient, compartment_id: str
) -> str:
    newest_image = None

    for version in UBUNTU_VERSIONS:
        images = list_all(
            compute_client.list_images,
            compartment_id=compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version=version,
            shape="VM.Standard.A1.Flex",
        )
        if not images:
            continue

        images.sort(key=lambda image: image.time_created, reverse=True)
        candidate = images[0]
        if newest_image is None or candidate.time_created > newest_image.time_created:
            newest_image = candidate

    if newest_image is None:
        raise RuntimeError("No compatible Ubuntu A1 image was found")

    return newest_image.id


def subnet_env_name(region_name: str) -> str:
    return f"OCI_SUBNET_ID_{region_name.upper().replace('-', '_')}"


def resolve_subnet_id(
    region_name: str,
    subnet_override: str | None,
    region_subnet_ids: Dict[str, str],
) -> str | None:
    if subnet_override:
        return subnet_override

    env_value = os.environ.get(subnet_env_name(region_name))
    if env_value:
        return env_value

    return region_subnet_ids.get(region_name)


def build_launch_details(
    compartment_id: str,
    display_name: str,
    availability_domain: str,
    image_id: str,
    subnet_id: str,
    ssh_key: str,
    ocpu: int,
    memory_gb: int,
    boot_volume_gb: int,
    bootstrap: bool,
) -> oci.core.models.LaunchInstanceDetails:
    metadata = {"ssh_authorized_keys": ssh_key}
    if bootstrap:
        metadata["user_data"] = base64.b64encode(build_cloud_init().encode("utf-8")).decode(
            "ascii"
        )

    return oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        display_name=display_name,
        availability_domain=availability_domain,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpu, memory_in_gbs=memory_gb
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image_id,
            boot_volume_size_in_gbs=boot_volume_gb,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata=metadata,
        freeform_tags={
            "managed-by": "oci-toolbox",
            "role": "generic-arm-host",
        },
    )


def init_region_state(
    base_config: Dict[str, str],
    compartment_id: str,
    region_name: str,
) -> Dict[str, object]:
    region_config = base_config.copy()
    region_config["region"] = region_name

    compute = oci.core.ComputeClient(region_config)
    identity = oci.identity.IdentityClient(region_config)
    ads = [ad.name for ad in identity.list_availability_domains(compartment_id).data]

    return {
        "compute": compute,
        "ads": ads,
        "image_id": get_latest_ubuntu_image(compute, compartment_id),
        "image_refreshed_at": time.time(),
    }


def maybe_refresh_image(
    state: Dict[str, object],
    compartment_id: str,
) -> None:
    if time.time() - float(state["image_refreshed_at"]) < IMAGE_CACHE_SECONDS:
        return

    compute = state["compute"]
    state["image_id"] = get_latest_ubuntu_image(compute, compartment_id)
    state["image_refreshed_at"] = time.time()


def worker(
    worker_id: int,
    profile: str,
    compartment_id: str,
    candidate_regions: Sequence[str],
    shapes: Sequence[Tuple[int, int]],
    ssh_key: str,
    subnet_override: str | None,
    region_subnet_ids: Dict[str, str],
    name_prefix: str,
    boot_volume_gb: int,
    bootstrap: bool,
    stop_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
) -> None:
    base_config = load_config(profile)
    region_state: Dict[str, Dict[str, object]] = {}
    backoff = BASE_RETRY_SECONDS

    while not stop_event.is_set():
        time.sleep(random.uniform(1.0, 3.0))

        for region_name in candidate_regions:
            if stop_event.is_set():
                return

            subnet_id = resolve_subnet_id(region_name, subnet_override, region_subnet_ids)
            if not subnet_id:
                print(
                    f"[worker {worker_id}] region={region_name} skipped: missing subnet mapping",
                    flush=True,
                )
                continue

            if region_name not in region_state:
                try:
                    region_state[region_name] = init_region_state(
                        base_config, compartment_id, region_name
                    )
                except Exception as exc:
                    print(
                        f"[worker {worker_id}] region={region_name} init failed: {exc}",
                        flush=True,
                    )
                    time.sleep(BASE_RETRY_SECONDS)
                    continue

            state = region_state[region_name]
            maybe_refresh_image(state, compartment_id)
            ads = list(state["ads"])
            random.shuffle(ads)
            compute = state["compute"]
            image_id = str(state["image_id"])

            shape_order = list(shapes)
            if worker_id % 2 == 1:
                random.shuffle(shape_order)

            for availability_domain in ads:
                for ocpu, memory_gb in shape_order:
                    if stop_event.is_set():
                        return

                    display_name = f"{name_prefix}-{worker_id}-{int(time.time())}"
                    print(
                        (
                            f"[worker {worker_id}] region={region_name} ad={availability_domain} "
                            f"trying {ocpu} OCPU / {memory_gb} GB"
                        ),
                        flush=True,
                    )

                    try:
                        response = compute.launch_instance(
                            build_launch_details(
                                compartment_id=compartment_id,
                                display_name=display_name,
                                availability_domain=availability_domain,
                                image_id=image_id,
                                subnet_id=subnet_id,
                                ssh_key=ssh_key,
                                ocpu=ocpu,
                                memory_gb=memory_gb,
                                boot_volume_gb=boot_volume_gb,
                                bootstrap=bootstrap,
                            )
                        )
                        instance = response.data
                        result_queue.put(
                            {
                                "instance_id": instance.id,
                                "display_name": display_name,
                                "region": region_name,
                                "availability_domain": availability_domain,
                                "shape": f"{ocpu} OCPU / {memory_gb} GB",
                            }
                        )
                        stop_event.set()
                        return

                    except oci.exceptions.ServiceError as exc:
                        print(
                            (
                                f"[worker {worker_id}] region={region_name} ad={availability_domain} "
                                f"{exc.status} {exc.code}: {exc.message}"
                            ),
                            flush=True,
                        )

                        if exc.status == 500:
                            time.sleep(BASE_RETRY_SECONDS + random.uniform(0, 5))
                        elif exc.status == 429 or exc.code == "LimitExceeded":
                            time.sleep(backoff + random.uniform(0, 5))
                            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                        elif 400 <= exc.status < 500:
                            return
                        else:
                            time.sleep(20)

                    except Exception as exc:
                        print(
                            (
                                f"[worker {worker_id}] region={region_name} ad={availability_domain} "
                                f"unexpected error: {exc}"
                            ),
                            flush=True,
                        )
                        time.sleep(BASE_RETRY_SECONDS)


def filter_regions_with_subnets(
    candidate_regions: Sequence[str],
    subnet_override: str | None,
    region_subnet_ids: Dict[str, str],
) -> List[str]:
    if subnet_override:
        return list(candidate_regions)

    return [
        region_name
        for region_name in candidate_regions
        if resolve_subnet_id(region_name, subnet_override, region_subnet_ids)
    ]


def describe_region_choice(
    home_region: str,
    ranked_regions: Sequence[str],
    location: Dict[str, float | str],
) -> None:
    label = location["label"]
    print(f"Location hint: {label}")
    print(f"Home region: {home_region}")
    print(f"Distance-ranked subscribed regions: {', '.join(ranked_regions)}")
    print("Always Free mode is enforced, so launch attempts are restricted to the home region.")
    if ranked_regions and ranked_regions[0] != home_region:
        print(
            f"Nearest subscribed region by distance would be {ranked_regions[0]}, "
            f"but it is not used because it would not be Always Free."
        )


def get_active_compartment_ids(
    identity: oci.identity.IdentityClient,
    root_compartment_id: str,
) -> List[str]:
    compartments = list_all(
        identity.list_compartments,
        compartment_id=root_compartment_id,
        compartment_id_in_subtree=True,
        access_level="ANY",
    )
    compartment_ids = [root_compartment_id]
    compartment_ids.extend(
        compartment.id
        for compartment in compartments
        if compartment.lifecycle_state == "ACTIVE"
    )
    return compartment_ids


def list_all_boot_volumes(
    blockstorage: oci.core.BlockstorageClient,
    compartment_id: str,
    availability_domains: Sequence[str],
) -> List[oci.core.models.BootVolume]:
    volumes: List[oci.core.models.BootVolume] = []
    for availability_domain in availability_domains:
        volumes.extend(
            list_all(
                blockstorage.list_boot_volumes,
                compartment_id=compartment_id,
                availability_domain=availability_domain,
            )
        )
    return volumes


def list_all_block_volumes(
    blockstorage: oci.core.BlockstorageClient,
    compartment_id: str,
    availability_domains: Sequence[str],
) -> List[oci.core.models.Volume]:
    volumes: List[oci.core.models.Volume] = []
    for availability_domain in availability_domains:
        volumes.extend(
            list_all(
                blockstorage.list_volumes,
                compartment_id=compartment_id,
                availability_domain=availability_domain,
            )
        )
    return volumes


def calculate_always_free_headroom(
    base_config: Dict[str, str],
    compartment_id: str,
    home_region: str,
) -> Dict[str, int]:
    home_config = base_config.copy()
    home_config["region"] = home_region

    compute = oci.core.ComputeClient(home_config)
    identity = oci.identity.IdentityClient(home_config)
    blockstorage = oci.core.BlockstorageClient(home_config)

    root_compartment_id = base_config["tenancy"]
    compartment_ids = get_active_compartment_ids(identity, root_compartment_id)
    availability_domains = [
        ad.name for ad in identity.list_availability_domains(root_compartment_id).data
    ]

    instances: List[oci.core.models.Instance] = []
    for current_compartment_id in compartment_ids:
        instances.extend(list_all(compute.list_instances, compartment_id=current_compartment_id))
    active_a1_instances = [
        instance
        for instance in instances
        if instance.shape == "VM.Standard.A1.Flex"
        and instance.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]

    used_ocpus = int(
        sum((instance.shape_config.ocpus or 0) for instance in active_a1_instances)
    )
    used_memory_gb = int(
        sum((instance.shape_config.memory_in_gbs or 0) for instance in active_a1_instances)
    )

    boot_volumes: List[oci.core.models.BootVolume] = []
    block_volumes: List[oci.core.models.Volume] = []
    for current_compartment_id in compartment_ids:
        boot_volumes.extend(
            list_all_boot_volumes(blockstorage, current_compartment_id, availability_domains)
        )
        block_volumes.extend(
            list_all_block_volumes(blockstorage, current_compartment_id, availability_domains)
        )
    active_boot_volumes = [
        volume
        for volume in boot_volumes
        if volume.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]
    active_block_volumes = [
        volume
        for volume in block_volumes
        if volume.lifecycle_state not in {"TERMINATED", "TERMINATING"}
    ]

    used_volume_gb = int(
        sum(int(volume.size_in_gbs or 0) for volume in active_boot_volumes)
        + sum(int(volume.size_in_gbs or 0) for volume in active_block_volumes)
    )

    return {
        "used_ocpus": used_ocpus,
        "used_memory_gb": used_memory_gb,
        "used_volume_gb": used_volume_gb,
        "remaining_ocpus": max(ALWAYS_FREE_MAX_OCPUS - used_ocpus, 0),
        "remaining_memory_gb": max(ALWAYS_FREE_MAX_MEMORY_GB - used_memory_gb, 0),
        "remaining_volume_gb": max(ALWAYS_FREE_MAX_VOLUME_GB - used_volume_gb, 0),
    }


def filter_shapes_for_headroom(
    requested_shapes: Sequence[Tuple[int, int]],
    remaining_ocpus: int,
    remaining_memory_gb: int,
) -> List[Tuple[int, int]]:
    return [
        (ocpu, memory_gb)
        for ocpu, memory_gb in requested_shapes
        if ocpu <= remaining_ocpus and memory_gb <= remaining_memory_gb
    ]
