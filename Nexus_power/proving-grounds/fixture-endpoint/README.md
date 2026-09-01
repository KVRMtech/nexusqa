# fixture-endpoint — a client's environment, standing in

`RestProvider` (`platform/qe-central/app/services/env_data_transports.py`) asks a
client's own system for the values a crawl needs. The contract is deliberately
small enough to implement in an afternoon:

    GET {base}/slots            -> {"slots": ["member number", ...]}
    GET {base}/value/{slot_key} -> {"value": "25000001"}

Until this existed there was nothing in the repository on the other end of it, so
the environment-provider path could only be exercised against a mock written by
the same hand as the code under test.

## Run it

    python server.py --port 8130 --token dev-token       # local
    docker build -t qec-fixture-endpoint . && \
      docker run -p 8130:8130 -e FIXTURE_TOKEN=dev-token qec-fixture-endpoint

## What it is careful about, and why

Each of these is a behaviour `RestProvider` actually depends on — read off the
consumer, not imagined:

| behaviour | why it matters |
| --- | --- |
| `Authorization: Bearer <token>` enforced when a token is set | a fixture that ignored it would let a broken auth path ship green |
| an unknown slot is a **404**, never `200 {"value": null}` | the provider reads a 200 body; a null would be an answer it had *received*, so an unanswerable slot would read as answered |
| every response is a JSON **object** | `body if isinstance(body, dict) else None` — a bare list is a decline |
| no value exceeds 512 chars (`MAX_VALUE_CHARS`) | the provider treats more as a misconfiguration, so serving it would teach clients a shape the resolver rejects |

## The values are fictional and say so

Nothing here is a real person's data. The point is **provenance**: a field
answered from this endpoint arrives in the ledger as `env`-provenance, which is
the one thing a crawl cannot fake by inventing a value itself.

## Tests

`platform/qe-central/tests/test_the_fixture_endpoint_answers_the_resolver.py`
runs this service on a real socket and points the real provider at it — both
ends, no mock. It includes the control that a wrong token is refused, without
which every assertion would also pass against a fixture that ignored auth.
