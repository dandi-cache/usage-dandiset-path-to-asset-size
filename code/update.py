"""The size in bytes of every content ID the usage cache tracks.

Sizes come from the archive's own `assets.jsonld` manifests, keyed by content ID: a blob's
`contentSize` is the same wherever it appears, so any Dandiset that uses it yields the same
answer.

Two things shape the loop, both kept from what this cache has published:

- It is accumulative. A size already recorded is never recomputed, because a blob's size does not
  change. Only content IDs with no size yet are pursued.
- It works by Dandiset, not by content ID. Each run targets the Dandisets that still hold an
  unresolved content ID and reads their manifests, so a bounded run makes steady progress through
  the backlog instead of redoing the same prefix. One manifest resolves many content IDs at once.

A content ID that cannot be resolved yet, because its Dandiset is embargoed or it appears in no
manifest, is left absent rather than recorded as null, and retried next run. Older revisions of
this cache did write nulls, so they are dropped on read.

Everything shared -- the argument parsing, the logging, the batch cap, the input reading, the
output paths, testing mode, the JSON Lines writing, the unsigned S3 client, the manifest listing
and reader, and the content-ID parse -- comes from `dandi_cache_utils`.
"""

import dandi_cache_utils as dandi_cache

#: One connection per worker; a smaller pool makes the surplus workers redo the TLS handshake.
WORKERS = 16


def main() -> None:
    dataset, arguments = dandi_cache.open_dataset()
    client = dandi_cache.s3.anonymous_client(max_pool_connections=WORKERS)

    usage_dandiset_path = dataset.read_input()

    # Earlier revisions wrote `null` for an unresolved size. Dropping those here is what has them
    # treated as unresolved and retried, rather than read back as a recorded answer.
    sizes = {content_id: size for content_id, size in dataset.read_output_lookup().items() if size is not None}

    unresolved_dandiset_ids = sorted(
        {
            next(iter(dandiset_path))
            for content_id, dandiset_path in usage_dandiset_path.items()
            if content_id not in sizes and dandiset_path
        }
    )
    limit = dandi_cache.effective_limit(testing=dataset.testing, limit=arguments.limit)
    if limit is not None:
        unresolved_dandiset_ids = unresolved_dandiset_ids[:limit]
    dandi_cache.logger.info("Processing %d Dandisets with unresolved content IDs.", len(unresolved_dandiset_ids))

    def sizes_from_manifest(key: str, /) -> dict[str, int]:
        assets = dandi_cache.s3.dandiset_assets(client, key)
        if assets is None:
            return {}
        resolved = {}
        for asset in assets:
            content_urls = asset.get("contentUrl")
            content_size = asset.get("contentSize")
            if content_urls and content_size is not None:
                resolved[dandi_cache.s3.content_id_from_content_urls(content_urls)] = content_size
        return resolved

    manifest_keys = [
        key
        for dandiset_id in unresolved_dandiset_ids
        for key in dandi_cache.s3.asset_manifest_keys(client, dandiset_id=dandiset_id)
    ]
    for resolved in dandi_cache.s3.concurrent_map(sizes_from_manifest, manifest_keys, max_workers=WORKERS):
        sizes.update(resolved)

    def build() -> list[dict]:
        # Restricted to the content IDs the source tracks: the manifests also cover assets it does
        # not, and an ID it has stopped tracking is dropped rather than carried forever.
        resolved = {
            content_id: sizes[content_id] for content_id in usage_dandiset_path if sizes.get(content_id) is not None
        }
        dandi_cache.logger.info("Resolved %d of %d content IDs.", len(resolved), len(usage_dandiset_path))
        return [{content_id: resolved[content_id]} for content_id in sorted(resolved)]

    dandi_cache.run_full_rebuild(dataset, build=build)


if __name__ == "__main__":
    main()
