# DANDI Cache: `usage-dandiset-path-to-asset-size`

A cache mapping each DANDI content id to its asset size in bytes.

Each record is a single-key JSON object mapping a content id to its size in bytes:

```json
{"00003b22-9c54-4d15-afae-42ea9816c146": 1234567}
```

The content ids are exactly those of the [`dandi-cache/content-id-to-usage-dandiset-path`](https://github.com/dandi-cache/content-id-to-usage-dandiset-path) cache, which is consumed as an input subdataset. For each content id, the size is read straight from the public DANDI archive S3 bucket — the `contentSize` field of the `assets.jsonld` manifests under `s3://dandiarchive/dandisets/<dandiset_id>/<version>/` — the same manifests the grandparent [`dandi-cache/content-id-to-dandiset-paths`](https://github.com/dandi-cache/content-id-to-dandiset-paths) cache reads. No DANDI REST API calls are made. A content id whose asset is not found in the manifests is recorded with size `null`.

Updated frequently.

Primarily for use by developers.



## One-time use

If you only plan to use this cache infrequently or from disparate locations, you can directly download the latest version of the cache as a compressed [JSON Lines](https://jsonlines.org/) file from the `dist` branch:

### Python API (recommended)

```python
import gzip
import json

import requests

url = "https://raw.githubusercontent.com/dandi-cache/usage-dandiset-path-to-asset-size/refs/heads/dist/derivatives/usage_dandiset_path_to_asset_size.jsonl.gz"
response = requests.get(url)
lines = gzip.decompress(data=response.content).decode("utf-8").splitlines()
usage_dandiset_path_to_asset_size = [json.loads(line) for line in lines]
```

### Save to file

```bash
curl https://raw.githubusercontent.com/dandi-cache/usage-dandiset-path-to-asset-size/refs/heads/dist/derivatives/usage_dandiset_path_to_asset_size.jsonl.gz -o usage_dandiset_path_to_asset_size.jsonl.gz
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
