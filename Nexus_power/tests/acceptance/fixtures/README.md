# Acceptance fixtures

One YAML file per recording.  Each file declares the ground-truth user
actions a competent reviewer observed when watching the video manually,
and the thresholds the pipeline must meet against those actions.

## Schema

```yaml
name: short-stable-id                   # used in pytest test ID; required
description: human summary              # optional
video_path: ../recordings/foo.mp4       # optional — for re-processing
artifact_id: <uuid>                     # optional — use cached artifact
thresholds:
  action_kind_accuracy: 0.80            # default 0.60
  target_match_rate:    0.70            # default 0.50
  value_match_rate:     0.50            # default 0.40
  max_spurious_steps:   3               # default 5
expected_actions:
  - timestamp_ms_min: 1000              # earliest acceptable timestamp
    timestamp_ms_max: 3000              # latest acceptable timestamp
    action_kind: click_cta              # canonical kind (see below)
    target_label_contains: Submit       # case-insensitive substring
    observed_value_contains: ''         # required when value matters
    notes: 'Form submission'            # free-form; rendered on failure
  - timestamp_ms_min: 5500
    timestamp_ms_max: 7500
    action_kind: enter_text
    target_label_contains: birthdate
    observed_value_contains: '1990'
```

### Canonical action_kind values

The matcher accepts compatible synonyms automatically; pick the one
that best describes the user's *intent*:

| Annotation kind   | Compatible classifier outputs       |
|---                |---                                  |
| `click_cta`       | click_cta · click                   |
| `enter_text`      | enter_text · type                   |
| `select_option`   | select_option · select              |
| `submit_form`     | submit_form · submit                |
| `navigate`        | navigate · open                     |
| `open_overlay`    | open_overlay · open                 |
| `review`          | review · verify · inspect           |
| `scroll`          | scroll · scroll_or_repaint          |

## Adding a fixture

1. Drop the recording in your operator-managed media volume (the
   file does not need to live in the repo).
2. Run the canonical pipeline against it through the normal upload
   flow; copy the resulting `artifact_id`.
3. Watch the video and write the `expected_actions` list — one entry
   per observable user-intent step you saw on screen.
4. Save the YAML file in this directory.  Pytest collection picks up
   ``*.yaml`` and ``*.yml`` files automatically.
5. Run `pytest tests/acceptance` to validate.

## Running locally

The default fetcher hits the local platform API at
`http://localhost:8091`.  Set:

```bash
export NEXUS_ACCEPTANCE_API_BASE=http://localhost:8091
export NEXUS_ACCEPTANCE_TOKEN="$(cat ~/.nexus/jwt)"
pytest tests/acceptance -v
```

## Running in CI

Override the step-fetcher fixture in your CI conftest to load canned
JSON instead of hitting a live API:

```python
import json, pathlib
import pytest

@pytest.fixture
def acceptance_fetch_steps():
    def fetch(fixture):
        path = pathlib.Path(__file__).parent / "canned" / f"{fixture.name}.json"
        return json.loads(path.read_text())["steps"]
    return fetch
```
