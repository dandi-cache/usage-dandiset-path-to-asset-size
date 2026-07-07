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


def _load_previous_cache(cache_file_path: pathlib.Path) -> dict[str, int]:
    """Read the previous run's cache back into memory (empty on a bootstrap run)."""
    previous_cache: dict[str, int] = {}
    if not cache_file_path.exists():
        return previous_cache

    with cache_file_path.open() as file_stream:
        for line in file_stream:
            if stripped_line := line.strip():
                # Drop null sizes (written by earlier revisions of this cache) so those
                # content ids are treated as unresolved and retried.
                previous_cache.update(
                    (content_id, size) for content_id, size in json.loads(stripped_line).items() if size is not None
                )
    return previous_cache


def _run(base_directory: pathlib.Path, limit: int | None, max_workers: int) -> None:
    # Resolve the size in bytes of the content ids in the source mapping and emit a single-key
    # `{content_id: size}` record for each resolved id. Only resolved sizes are ever written:
    # a record is never `null`. Content ids that cannot be resolved yet (embargoed dandiset,
    # not present in any manifest) are simply absent and retried on the next run.
    #
    # The cache is accumulative and incremental: the pipeline runs on a clone of the
    # persistent `derivatives` branch, so the previous run's cache is already present. Sizes
    # recorded there are kept (a blob's size never changes), and each run works only on the
    # dandisets that still have unresolved content ids, so limited runs make steady progress
    # through the backlog instead of redoing the same prefix.
    #
    # Sizes are read from the `assets.jsonld` manifests of the targeted dandisets, keyed by
    # content id (a blob/zarr's `contentSize` is identical wherever it appears, so any
    # dandiset that uses it yields the same size).
    #
    # `limit` caps the number of dandisets processed in a single run; the default processes
    # every dandiset that still has unresolved content ids.

    source_mapping = _load_source_mapping(base_directory)

    derivatives_directory = base_directory / "derivatives"
    derivatives_directory.mkdir(parents=True, exist_ok=True)
    output_file_path = derivatives_directory / "usage_dandiset_path_to_asset_size.jsonl"

    content_id_to_size = _load_previous_cache(output_file_path)

    # Target only the dandisets that still have unresolved content ids.
    unresolved_dandiset_ids = sorted(
        {
            next(iter(dandiset_path))
            for content_id, dandiset_path in source_mapping.items()
            if content_id not in content_id_to_size
        }
    )
    if limit is not None:
        unresolved_dandiset_ids = unresolved_dandiset_ids[:limit]
    print(f"Processing {len(unresolved_dandiset_ids)} dandisets with unresolved content ids.", flush=True)

    s3_client = _build_s3_client(max_pool_connections=max_workers)

    manifest_keys: list[str] = []
    for dandiset_id in unresolved_dandiset_ids:
        manifest_keys.extend(_iter_manifest_keys(s3_client, dandiset_id))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        sizes_from_manifest = functools.partial(_sizes_from_manifest, s3_client)
        for manifest_sizes in executor.map(sizes_from_manifest, manifest_keys):
            content_id_to_size.update(manifest_sizes)

    # Restrict to the source's content ids (the manifests also cover ids the source does not
    # track) and drop ids the source no longer tracks.
    records = {
        content_id: content_id_to_size[content_id]
        for content_id in source_mapping
        if content_id_to_size.get(content_id) is not None
    }
    print(f"Resolved {len(records)} of {len(source_mapping)} content ids.", flush=True)

    with output_file_path.open(mode="w") as file_stream:
        file_stream.writelines(f"{json.dumps({content_id: records[content_id]})}\n" for content_id in sorted(records))


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
