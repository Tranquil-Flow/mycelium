# A13 macOS selected-model acquisition handoff

This is the integration contract for the bounded C3 acquisition component. It is
source acquisition only: it does not quantize, load tensors, start a worker,
qualify a route, or manufacture provisioning/serving readiness.

## Public interface

```python
from mycelium_a13_macos.acquisition import SelectedModelAcquisition

acquisition = SelectedModelAcquisition(private_state_root / "acquisition")
acquisition.start({
    "download_acknowledged": True,
    "max_download_bytes": 999_604_126,
    "storage_limit_bytes": 999_604_126,
})
status = acquisition.status()
# status has only state, downloaded_bytes, total_bytes, error_code, and model.

# Available only after terminal state `prepared`; otherwise raises a stable
# AcquisitionError code. This is mycelium_a13_macos.serving.PreparedSource.
prepared = acquisition.prepared_source()

acquisition.cancel()
acquisition.close()
```

`start` accepts exactly the three consent keys shown above, starts at most one
background attempt, and performs no network operation before that explicit call.
A blocking `_run_blocking(consent)` seam exists for deterministic tests.

The production transport resolves only public
`Qwen/Qwen2.5-0.5B-Instruct` at pinned commit
`7ae557604adf67be50417f59c2c2f167def9a775`, over authenticated Web-PKI HTTPS.
It obtains the repository file inventory and sizes from the pinned Hugging Face
revision API, verifies LFS SHA-256 or Git blob SHA-1 metadata for every acquired
file, then independently re-verifies the complete snapshot and calls the real
`compile_local_model_manifest` through `prepare_local_source`. Partial files are
private and resumable. Status and errors never expose URLs, query strings,
response bodies, local paths, or credentials.

Tests may inject a manifest/transport, including loopback HTTP or a test CA for a
real local HTTPS fixture. Those fixtures are transport and failure-path evidence
only; they are **not public model-download proof**. No full selected-model download
or conversion is performed by this component's tests.
