# podpoint-mobile-api

Async Python client for `mobile-api.pod-point.com`, the backend behind Pod Point's "Pod Home"
app - as distinct from the legacy `api.pod-point.com/v4` API the community `pod_point` Home
Assistant integration targets. No Home Assistant dependency; just `aiohttp`.

Extracted from the `pod_home` Home Assistant integration's `custom_components/pod_home/api/`
into its own package, following the pattern most platinum-tier HA integrations with a
meaningful API surface use (e.g. `ohme` on PyPI, maintained by the same person as the `ohme`
HA integration) - keeps the client independently usable/testable, and satisfies HA's
`dependency-transparency`/`async-dependency` quality-scale rules, which only have something to
check once an external dependency like this actually exists.

## Usage

```python
import aiohttp
from podpoint_mobile_api import PodHomeAuth, PodHomeApiClient

async with aiohttp.ClientSession() as session:
    auth = PodHomeAuth(session, "you@example.com", "your-password")
    api = PodHomeApiClient(session, auth)
    chargers = await api.async_list_chargers()
```

## Status

Read-only. `charge-overrides` (charge now) and `remote-lock` (cable lock) are real, confirmed
endpoints (see the parent project's findings notes) but deliberately not implemented here yet -
they have real physical side effects on the charger and haven't been tested live.

## Local development

```bash
pip install -e .
```

Then the dev/test scripts under the parent project's `scratch/` can import `podpoint_mobile_api`
directly instead of the old sys.modules-stubbing workaround.

Not yet published anywhere pip-installable (no PyPI release, no tagged git release) - the
parent integration's `manifest.json` can't reference this as a real `requirements` entry until
that's done.
