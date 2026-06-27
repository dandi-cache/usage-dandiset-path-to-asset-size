import argparse
import gzip
import json
import pathlib

from dandi.dandiapi import DandiAPIClient

# The source cache is registered as an input subdataset under `sourcedata`. Its published
# derivative is a single JSON object mapping each content id (a DANDI blob/zarr UUID) to a
# single `{dandiset_id: asset_path}` pair.
SOURCE_SUBDATASET_NAME = "content-id-to-usage-dandiset-path"
SOURCE_FILE_STEM = "content_id_to_usage_dandiset_path"


def _load_source_mapping(base_directory: pathlib.Path) -> dict:
    source_directory = base_directory / "sourcedata" / SOURCE_SUBDATASET_NAME / "derivatives"
    candidate_file_paths = [
        source_directory / f"{SOURCE_FILE_STEM}.json",
        source_directory / f"{SOURCE_FILE_STEM}.min.json",
        source_directory / f"{SOURCE_FILE_STEM}.json.gz",
        source_directory / f"{SOURCE_FILE_STEM}.min.json.gz",
    ]
    for file_path in candidate_file_paths:
        if file_path.exists():
            raw = file_path.read_bytes()
            if file_path.suffix == ".gz":
                raw = gzip.decompress(raw)
            return json.loads(raw)

    candidates = ", ".join(file_path.name for file_path in candidate_file_paths)
    raise FileNotFoundError(f"Could not find the source mapping in {source_directory} (looked for: {candidates}).")


def _run(base_directory: pathlib.Path, limit: int | None) -> None:
    # For each content id in the source mapping, resolve the size in bytes of the asset it
    # refers to and emit a simplified `{content_id, size}` record.
    #
    # The content ids are grouped by dandiset so each dandiset's assets are enumerated only
    # once and matched to the requested paths, rather than issuing one lookup per content id.
    #
    # `limit` caps the number of dandisets processed in a single run (primarily a testing/CI
    # knob); the default processes every dandiset referenced by the source mapping.

    source_mapping = _load_source_mapping(base_directory)

    content_ids_by_dandiset: dict[str, dict[str, str]] = {}  # dandiset_id -> {asset_path: content_id}
    for content_id, dandiset_path in source_mapping.items():
        ((dandiset_id, asset_path),) = dandiset_path.items()
        content_ids_by_dandiset.setdefault(dandiset_id, {})[asset_path] = content_id

    content_id_to_size: dict[str, int] = {}
    with DandiAPIClient() as client:
        for index, (dandiset_id, paths_to_content_ids) in enumerate(sorted(content_ids_by_dandiset.items())):
            if limit is not None and index >= limit:
                break

            try:
                dandiset = client.get_dandiset(dandiset_id)
                most_recent_published_version = dandiset.most_recent_published_version
                if most_recent_published_version is not None:
                    dandiset = dandiset.for_version(most_recent_published_version)

                for asset in dandiset.get_assets():
                    content_id = paths_to_content_ids.get(asset.path)
                    if content_id is not None:
                        content_id_to_size[content_id] = asset.size
            except Exception as exception:  # noqa: BLE001
                # Skip dandisets that fail to enumerate (e.g. transient API errors, removed
                # or embargoed dandisets) rather than aborting the entire run.
                print(f"Skipping dandiset {dandiset_id}: {exception}")
                continue

    derivatives_directory = base_directory / "derivatives"
    derivatives_directory.mkdir(parents=True, exist_ok=True)

    output_file_path = derivatives_directory / "usage_dandiset_path_to_asset_size.jsonl"
    with output_file_path.open(mode="w") as file_stream:
        file_stream.writelines(
            f"{json.dumps({'content_id': content_id, 'size': size})}\n"
            for content_id, size in content_id_to_size.items()
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
    args = parser.parse_args()

    _run(base_directory=args.base_directory, limit=args.limit)
