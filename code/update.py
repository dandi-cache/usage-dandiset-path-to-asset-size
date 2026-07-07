import argparse
import concurrent.futures
import functools
import json
import pathlib

import boto3
import botocore
import botocore.config
import botocore.exceptions

# The source cache is registered as an input subdataset under `sourcedata`. Its derivative is
# published as JSON Lines on the source's `derivatives` branch (one single-key
# `{content_id: {dandiset_id: path}}` object per line); the JSON single-object form is also
# accepted for backwards compatibility. Only the content ids and their dandiset ids are used
# here: the content ids define this cache's key set, and the dandiset ids select which S3
# manifests to read.
SOURCE_SUBDATASET_NAME = "content-id-to-usage-dandiset-path"
SOURCE_FILE_STEM = "content_id_to_usage_dandiset_path"

# Asset sizes are read directly from the public DANDI archive S3 bucket -- the same
# `assets.jsonld` manifests the grandparent content-id-to-dandiset-paths cache reads -- rather
# than from the DANDI REST API. Each Dandiset version publishes a manifest under
# `dandisets/<dandiset_id>/<version>/assets.jsonld` listing every asset with its `contentSize`
# and its `contentUrl`s (the second of which is the S3 URL that embeds the content id).
_BUCKET = "dandiarchive"
_REGION = "us-east-2"
_ASSETS_SUFFIX = "/assets.jsonld"


def _load_source_mapping(base_directory: pathlib.Path) -> dict:
    source_directory = base_directory / "sourcedata" / SOURCE_SUBDATASET_NAME / "derivatives"

    jsonl_file_path = source_directory / f"{SOURCE_FILE_STEM}.jsonl"
    if jsonl_file_path.exists():
        mapping: dict = {}
        with jsonl_file_path.open(mode="r") as file_stream:
            for line in file_stream:
                if line.strip():
                    mapping.update(json.loads(line))
        return mapping

    json_candidate_file_paths = [
        source_directory / f"{SOURCE_FILE_STEM}.json",
        source_directory / f"{SOURCE_FILE_STEM}.min.json",
    ]
    for file_path in json_candidate_file_paths:
        if file_path.exists():
            return json.loads(file_path.read_text())

    candidates = ", ".join([jsonl_file_path.name] + [p.name for p in json_candidate_file_paths])
    raise FileNotFoundError(f"Could not find the source mapping in {source_directory} (looked for: {candidates}).")


def _build_s3_client(max_pool_connections: int) -> "botocore.client.BaseClient":
    # `dandiarchive` is a public bucket, so requests are sent unsigned (anonymous). The
    # connection pool holds one connection per download worker to avoid redundant handshakes.
    config = botocore.config.Config(
        signature_version=botocore.UNSIGNED,
        max_pool_connections=max_pool_connections,
        retries={"mode": "standard"},
    )
    return boto3.client("s3", region_name=_REGION, config=config)


def _content_id_from_content_urls(content_urls: list[str]) -> str:
    # The second contentUrl is the S3 download URL: `.../blobs/<a>/<b>/<content_id>` for blob
    # assets, or `.../zarr/<content_id>` for zarr assets (matching the grandparent cache).
    s3_download_url = content_urls[1]
    return s3_download_url.split("/")[-1] if "blobs" in s3_download_url else s3_download_url.split("/")[-2]


def _iter_manifest_keys(s3_client: "botocore.client.BaseClient", dandiset_id: str):
    """Yield every `assets.jsonld` key across all versions of the given Dandiset."""
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_BUCKET, Prefix=f"dandisets/{dandiset_id}/"):
        for entry in page.get("Contents", []):
            if entry["Key"].endswith(_ASSETS_SUFFIX):
                yield entry["Key"]


def _sizes_from_manifest(s3_client: "botocore.client.BaseClient", key: str) -> dict[str, int]:
    try:
        response = s3_client.get_object(Bucket=_BUCKET, Key=key)
    except botocore.exceptions.ClientError as error:
        error_code = error.response.get("Error", {}).get("Code", "")
        # Embargoed Dandisets list their manifests but deny anonymous reads (AccessDenied); a
        # manifest can also be deleted between listing and fetching (NoSuchKey). Both are
        # expected upstream states, not pipeline failures, so skip the manifest.
        if error_code in ("AccessDenied", "NoSuchKey"):
            print(f"Skipping inaccessible manifest `{key}` ({error_code}).", flush=True)
            return {}
        raise

    body = response["Body"].read()
    all_asset_metadata = json.loads(body) if body.strip() else []

    sizes: dict[str, int] = {}
    for asset_metadata in all_asset_metadata:
        content_urls = asset_metadata.get("contentUrl")
        content_size = asset_metadata.get("contentSize")
        if not content_urls or content_size is None:
            continue
        sizes[_content_id_from_content_urls(content_urls)] = content_size
    return sizes


def _run(base_directory: pathlib.Path, limit: int | None, max_workers: int) -> None:
    # Resolve the size in bytes of every content id in the source mapping and emit a single-key
    # `{content_id: size}` record for each.
    #
    # Every content id from the source mapping gets exactly one record so this cache always
    # matches the source's size; the size is `null` whenever it cannot be resolved (no matching
    # asset was found in the S3 manifests). This avoids having to separately track which ids
    # errored or were already processed.
    #
    # Sizes are read from the `assets.jsonld` manifests of only the Dandisets referenced by the
    # source mapping, keyed by content id (a blob/zarr's `contentSize` is identical wherever it
    # appears, so any Dandiset that uses it yields the same size).
    #
    # `limit` caps the number of Dandisets processed in a single run (primarily a testing/CI
    # knob); the default processes every Dandiset referenced by the source mapping.

    source_mapping = _load_source_mapping(base_directory)

    dandiset_ids = sorted({next(iter(dandiset_path)) for dandiset_path in source_mapping.values()})
    if limit is not None:
        dandiset_ids = dandiset_ids[:limit]

    s3_client = _build_s3_client(max_pool_connections=max_workers)

    manifest_keys: list[str] = []
    for dandiset_id in dandiset_ids:
        manifest_keys.extend(_iter_manifest_keys(s3_client, dandiset_id))

    content_id_to_size: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        sizes_from_manifest = functools.partial(_sizes_from_manifest, s3_client)
        for manifest_sizes in executor.map(sizes_from_manifest, manifest_keys):
            content_id_to_size.update(manifest_sizes)

    derivatives_directory = base_directory / "derivatives"
    derivatives_directory.mkdir(parents=True, exist_ok=True)

    output_file_path = derivatives_directory / "usage_dandiset_path_to_asset_size.jsonl"
    with output_file_path.open(mode="w") as file_stream:
        file_stream.writelines(
            f"{json.dumps({content_id: content_id_to_size.get(content_id)})}\n" for content_id in source_mapping
        )


if __name__ == "__main__":
    default_base_directory = pathlib.Path(__file__).parent.parent

    parser = argparse.ArgumentParser(description="Update the usage-dandiset-path-to-asset-size DANDI cache.")
    parser.add_argument(
        "--base-directory",
        type=pathlib.Path,
        default=default_base_directory,
        help=(
            "The directory containing the `sourcedata` and `derivatives` directories. "
            "Set to the mounted dataset path when run inside the pipeline container; "
            "defaults to the repository root."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on the number of dandisets to process in this run.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Number of concurrent S3 workers used to list and fetch the asset manifests.",
    )
    args = parser.parse_args()

    _run(base_directory=args.base_directory, limit=args.limit, max_workers=args.max_workers)
