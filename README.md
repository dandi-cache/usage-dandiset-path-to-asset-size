# DANDI Cache: `usage-dandiset-path-to-asset-size`

A cache mapping every asset in every [DANDI](https://dandiarchive.org/) dandiset to its size in bytes, keyed by the dandiset and the asset's intra-dandiset path.

Each record is a single JSON object:

```json
{"dandiset_id": "000003", "version": "0.230629.1955", "asset_id": "...", "path": "sub-YutaMouse20/sub-YutaMouse20_ses-20170505.nwb", "size": 1234567}
```

The data is read live from the DANDI archive via the [DANDI Python client](https://dandi.readthedocs.io/), preferring each dandiset's most recently published version (falling back to the draft for unpublished dandisets).

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



## How it works

This cache demonstrates how generated results of the code branch and records every update with full provenance.

It uses three branches:

- **`main`** holds only the code of the update logic, the runtime container definition, and the CI workflows (including building and distributing the container images).
- [**`derivatives`**](https://github.com/dandi-cache/cache-template/tree/derivatives) is a persistent [DataLad](https://www.datalad.org/) dataset on its own branch. Each update is recorded there with `datalad containers-run`, so every revision carries full provenance of the exact command, the input subdataset commit, the output diff, and the runtime container image digest.
- **`dist`** is the lightweight publication artifact consumed by downstream users and preferred for one-time downloads.

The processing runs inside a published container image (`ghcr.io/dandi-cache/usage-dandiset-path-to-asset-size:latest`) that holds only the pinned runtime environment.

The orchestration lives in [`code/update_pipeline.sh`](code/update_pipeline.sh); the actual cache logic lives in [`code/update.py`](code/update.py).

The repository is described as a [BIDS study dataset](https://bids-specification.readthedocs.io/en/stable/common-principles.html#study-dataset) via [`dataset_description.json`](dataset_description.json) (`DatasetType: "study"`). Future enhancements may improve the provenance tracking through this mechanism in line with BEP028.



### Local development

The container image is the authoritative runtime, but you can recreate the environment locally with [uv](https://docs.astral.sh/uv/) for debugging:

```bash
uv run --project envs python code/update.py
```
