# S3-compatible provider registry
# Maps provider -> region -> (label, endpoint_url)
# endpoint_url is None when the SDK uses the default (e.g. AWS S3).

S3_PROVIDERS = {
    "Wasabi": {
        "endpoint_template": "https://s3.{region}.wasabisys.com",
        "regions": {
            "us-east-1": "US East 1 (N. Virginia)",
            "us-east-2": "US East 2 (N. Virginia)",
            "us-central-1": "US Central 1 (Texas)",
            "us-west-1": "US West 1 (Oregon)",
            "us-west-2": "US West 2 (San Jose)",
            "ca-central-1": "Canada (Toronto)",
            "eu-central-1": "EU Central 1 (Amsterdam)",
            "eu-central-2": "EU Central 2 (Frankfurt)",
            "eu-west-1": "EU West 1 (London)",
            "eu-west-2": "EU West 2 (Paris)",
            "eu-west-3": "EU West 3 (London)",
            "eu-south-1": "EU South 1 (Milan)",
            "ap-northeast-1": "AP Northeast 1 (Tokyo)",
            "ap-northeast-2": "AP Northeast 2 (Osaka)",
            "ap-southeast-1": "AP Southeast 1 (Singapore)",
            "ap-southeast-2": "AP Southeast 2 (Sydney)",
        },
    },
    "Backblaze B2": {
        "endpoint_template": "https://s3.{region}.backblazeb2.com",
        "regions": {
            "us-west-001": "US West (Sacramento)",
            "us-west-002": "US West (Phoenix)",
            "us-west-004": "US West (Oregon)",
            "us-east-005": "US East (New York)",
            "eu-central-003": "EU Central (Amsterdam)",
        },
    },
    "DigitalOcean Spaces": {
        "endpoint_template": "https://{region}.digitaloceanspaces.com",
        "regions": {
            "nyc1": "New York 1",
            "nyc2": "New York 2",
            "nyc3": "New York 3",
            "sfo2": "San Francisco 2",
            "sfo3": "San Francisco 3",
            "ams3": "Amsterdam 3",
            "sgp1": "Singapore 1",
            "lon1": "London 1",
            "fra1": "Frankfurt 1",
            "tor1": "Toronto 1",
            "blr1": "Bangalore 1",
            "syd1": "Sydney 1",
        },
    },
    "Cloudflare R2": {
        # Endpoint requires account ID: https://<account_id>.r2.cloudflarestorage.com
        # Region is always "auto" for R2
        "endpoint_template": None,
        "regions": {},
    },
    "Vultr Object Storage": {
        "endpoint_template": "https://{region}.vultrobjects.com",
        "regions": {
            "ewr1": "New Jersey",
            "sjc1": "Silicon Valley",
            "ams1": "Amsterdam",
            "sgp1": "Singapore",
            "blr1": "Bangalore",
            "del1": "New Delhi",
        },
    },
    "Linode Object Storage": {
        "endpoint_template": "https://{region}.linodeobjects.com",
        "regions": {
            "us-east-1": "US East (Newark)",
            "us-southeast-1": "US Southeast (Atlanta)",
            "us-ord-1": "US Central (Chicago)",
            "us-iad-1": "US East (Washington)",
            "us-lax-1": "US West (Los Angeles)",
            "us-mia-1": "US Southeast (Miami)",
            "eu-central-1": "EU Central (Frankfurt)",
            "nl-ams-1": "EU West (Amsterdam)",
            "gb-lon-1": "EU West (London)",
            "es-mad-1": "EU South (Madrid)",
            "ap-south-1": "AP South (Mumbai)",
            "in-maa-1": "AP South (Chennai)",
            "id-cgk-1": "AP Southeast (Jakarta)",
            "ap-west-1": "AP West (Singapore)",
            "jp-osa-1": "AP Northeast (Osaka)",
            "au-mel-1": "AP Southeast (Melbourne)",
            "br-gru-1": "SA East (São Paulo)",
        },
    },
    "Scaleway": {
        "endpoint_template": "https://s3.{region}.scw.cloud",
        "regions": {
            "fr-par": "Paris (France)",
            "nl-ams": "Amsterdam (Netherlands)",
            "pl-waw": "Warsaw (Poland)",
        },
    },
    "IDrive e2": {
        "endpoint_template": "https://s3.{region}.idrivee2.com",
        "regions": {
            "us-west-1": "Oregon",
            "us-west-2": "Los Angeles",
            "us-west-3": "San Jose",
            "us-southwest-1": "Phoenix",
            "us-midwest-1": "Chicago",
            "us-central-1": "Dallas",
            "us-east-1": "Virginia",
            "us-southeast-1": "Miami",
            "ca-east-1": "Montreal",
            "eu-west-1": "Ireland",
            "eu-west-2": "London",
            "eu-west-3": "London 2",
            "eu-west-4": "Paris",
            "eu-central-1": "Frankfurt 2",
            "eu-central-2": "Frankfurt",
            "ap-southeast-1": "Singapore",
        },
    },
    "Other": {
        "endpoint_template": None,  # user-provided (MinIO, Self Hosted, etc.)
        "regions": {},
    },
}


def get_provider_regions(provider):
    """Return region list for a provider.

    Returns:
        list[dict]: [{"region": "us-east-1", "label": "us-east-1 - US East (N. Virginia)", "endpoint": "https://..."}]
    """
    provider_info = S3_PROVIDERS.get(provider)
    if not provider_info:
        return []

    template = provider_info.get("endpoint_template")
    result = []
    for region_id, label in provider_info["regions"].items():
        endpoint = template.format(region=region_id) if template else ""
        result.append({
            "region": region_id,
            "label": f"{region_id} - {label}",
            "endpoint": endpoint,
        })
    return result
