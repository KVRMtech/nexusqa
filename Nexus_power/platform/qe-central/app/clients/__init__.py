"""QE-Central — outbound clients + Phase-1 explorer dispatch wiring.

Modules:
  * ``config``          — Phase-1 dispatch settings (``phase1_settings``) plus
                          the HMAC sign/verify helpers that MIRROR the explorer
                          ``app/config.py`` sign contract byte-for-byte.
  * ``explorer_client`` — typed httpx client that dispatches ``POST
                          /api/v1/explore`` to the contained explorer with the
                          ``X-QEC-Token`` shared secret (RUNNER_TOKEN pattern).
  * ``platform_api``    — typed httpx client for the UNCHANGED VKPower factory;
                          Phase-1 uses only the E3 auth-import endpoint, driven
                          with a minted ``role=manager`` service JWT.
  * ``manifest_mapper`` — the PURE explorer-manifest → ``ExplorationBundle``
                          mapper (the cross-subsystem contract pin).
"""
