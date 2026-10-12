# DANDI Cache: `usage-dandiset-path-to-asset-size`

A cache mapping each DANDI content id to its asset size in bytes.

Each record is a single-key JSON object mapping a content id to its size in bytes:

```json
{"00003b22-9c54-4d15-afae-42ea9816c146": 1234567}
```

The content ids are exactly those of the [`dandi-cache/content-id-to-usage-dandiset-path`](https://github.com/dandi-cache/content-id-to-usage-dandiset-path) cache, which is consumed as an input subdataset. For each content id, the size is read straight from the public DANDI archive S3 bucket — the `contentSize` field of the `assets.jsonld` manifests under `s3://dandiarchive/dandisets/<dandiset_id>/<version>/` — the same manifests the grandparent [`dandi-cache/content-id-to-dandiset-paths`](https://github.com/dandi-cache/content-id-to-dandiset-paths) cache reads. No DANDI REST API calls are made. The cache is accumulative: only resolved sizes are recorded (never `null`), and content ids that cannot be resolved yet (e.g. embargoed dandisets) are simply absent and retried on later runs.

Updated frequently.

Primarily for use by developers.



## One-time use

`dist` publishes each output as a [Zarr](https://zarr.dev/) store: the cache itself is `derivatives/usage_dandiset_path_to_asset_size.zarr/`.
A store is one array of [JSON Lines](https://jsonlines.org/) text cut into chunk files, each holding whole lines and gzip-compressed.
It can be read with the standard library alone, or opened remotely with any Zarr library.

### Python API (recommended)

The chunk files are gzip-compressed [JSON Lines](https://jsonlines.org/): list them, then read them in order as you would any JSONL files.

```python
import gzip, json, urllib.request

chunks = "https://api.github.com/repos/dandi-cache/usage-dandiset-path-to-asset-size/contents/derivatives/usage_dandiset_path_to_asset_size.zarr/stream/c?ref=dist"
files = sorted((entry["download_url"] for entry in json.load(urllib.request.urlopen(chunks))), key=lambda url: int(url.rsplit("/", 1)[1]))
usage_dandiset_path_to_asset_size = {key: value for url in files for line in gzip.decompress(urllib.request.urlopen(url).read()).decode().splitlines() if line for key, value in json.loads(line).items()}
```

### Remote Zarr store

Anything that opens Zarr can open the store over HTTP, reading only the chunks it needs.
The group's `zarr.json` carries the array's metadata, so opening it takes one request:

```bash
pip install "zarr>=3" fsspec aiohttp
```

```python
import json

import zarr

url = "https://raw.githubusercontent.com/dandi-cache/usage-dandiset-path-to-asset-size/refs/heads/dist/derivatives/usage_dandiset_path_to_asset_size.zarr"
stream = zarr.open_group(url, mode="r")["stream"]
print(stream.shape, stream.chunks)  # Bytes in all, and bytes per chunk.

# The first chunk, fetched and decompressed on its own: whole lines, each `{"<key>": <value>}`.
first_chunk = stream[: stream.chunks[0]].tobytes().decode("utf-8")
for line in first_chunk.splitlines():
    if line.strip():
        print(json.loads(line))
        break
```

Reading `stream[:]` returns the whole text; to read it in pieces, step through it by `stream.chunks[0]`, as each chunk is fetched whole.

### Save to file

A raw URL cannot be listed, so to fetch every chunk at once clone the `dist` branch, which holds nothing else:

```bash
git clone --branch dist --depth 1 https://github.com/dandi-cache/usage-dandiset-path-to-asset-size.git
```



## Repeated use

If you plan on using this cache regularly, clone the `dist` branch of this repository:

```bash
git clone --branch dist https://github.com/dandi-cache/usage-dandiset-path-to-asset-size.git
```

Then set up a CRON on your system to pull the latest version of the cache at your desired frequency.

For example, through `crontab -e`, add:

```bash
0 0 * * * git -C /path/to/usage-dandiset-path-to-asset-size pull
```

This will minimize data overhead by only loading the most recent changes.

### Local development

The container image is the authoritative runtime, but you can recreate the environment locally with [uv](https://docs.astral.sh/uv/) for debugging:

```bash
uv run --project envs python code/update.py
```

Each output is a store in the clone, with every chunk gzip-compressed:

```python
import gzip, json, pathlib

chunks = sorted(pathlib.Path("usage-dandiset-path-to-asset-size/derivatives/usage_dandiset_path_to_asset_size.zarr/stream/c").iterdir(), key=lambda path: int(path.name))
usage_dandiset_path_to_asset_size = {key: value for chunk in chunks for line in gzip.decompress(chunk.read_bytes()).decode().splitlines() if line for key, value in json.loads(line).items()}
```
