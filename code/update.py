import argparse
import json
import pathlib

from dandi.dandiapi import DandiAPIClient


def _run(base_directory: pathlib.Path, limit: int | None) -> None:
    # Map every asset in every dandiset to its size in bytes, keyed by the dandiset and the
    # asset's intra-dandiset path. The data is read live from the DANDI archive via the DANDI
    # Python client, so there is no `sourcedata` input; the full cache is recomputed on each
    # run (sizes and paths of existing dandisets can change as they are edited).
    #
    # `limit` caps the number of dandisets processed in a single run. It is primarily a
    # testing/CI knob; the default (None, or the pipeline's 2000) is larger than the number of
    # dandisets in the archive, so a normal run processes every dandiset.

    derivatives_directory = base_directory / "derivatives"
    derivatives_directory.mkdir(parents=True, exist_ok=True)

    output_file_path = derivatives_directory / "usage_dandiset_path_to_asset_size.jsonl"

    with DandiAPIClient() as client, output_file_path.open(mode="w") as file_stream:
        for index, dandiset in enumerate(client.get_dandisets()):
            if limit is not None and index >= limit:
                break

            # Prefer the most recent published version; fall back to the draft when a dandiset
            # has never been published.
            most_recent_published_version = dandiset.most_recent_published_version
            if most_recent_published_version is not None:
                dandiset = dandiset.for_version(most_recent_published_version)

            dandiset_id = dandiset.identifier
            version_id = dandiset.version_id

            try:
                assets = list(dandiset.get_assets())
            except Exception as exception:  # noqa: BLE001
                # Skip dandisets that fail to enumerate (e.g. transient API errors or
                # invalid states) rather than aborting the entire run.
                print(f"Skipping {dandiset_id}/{version_id}: {exception}")
                continue

            for asset in assets:
                record = {
                    "dandiset_id": dandiset_id,
                    "version": version_id,
                    "asset_id": asset.identifier,
                    "path": asset.path,
                    "size": asset.size,
                }
                file_stream.write(f"{json.dumps(record)}\n")


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
