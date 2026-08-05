/**
 * useJourneyCases — the journey ⇄ test-case map, fetched by the panel that
 * needs it.
 *
 * The ported factory panels are artifact-keyed and know nothing about
 * journeys; this hook is the one seam that gives them business context. It
 * self-fetches from qe-central (never from the factory client) so a panel
 * shows journey names even when it is mounted without a parent that has
 * already resolved them.
 *
 * Failure is silent BY DESIGN: journeys are additive context on top of a
 * panel that must keep working, so a fetch failure yields an empty map and
 * the panel renders exactly as it did before journeys existed.
 */
import { useEffect, useState } from 'react';

import { api as qecApi } from '../lib/api';

export interface JourneyCaseInfo {
  /** The journey's business name, as shown in the Journeys tab. */
  name: string;
  /** True for THE adopted end-to-end case — the journey's runnable form. */
  endToEnd: boolean;
  journeyId: string;
  /** Percent of the journey's walked path this case covers. */
  coverage: number;
}

export function useJourneyCases(appId?: string): Record<string, JourneyCaseInfo> {
  const [map, setMap] = useState<Record<string, JourneyCaseInfo>>({});

  useEffect(() => {
    if (!appId) return;
    let alive = true;
    (async () => {
      try {
        const list = await qecApi.listJourneys(appId);
        const next: Record<string, JourneyCaseInfo> = {};
        await Promise.all(list.journeys.map(async (j) => {
          try {
            const detail = await qecApi.getJourney(appId, j.journey_id);
            for (const c of detail.cases) {
              next[c.test_case_id] = {
                name: j.business_name || j.entry_title || 'Journey',
                endToEnd: c.kind === 'journey_e2e',
                journeyId: j.journey_id,
                coverage: c.coverage_score,
              };
            }
          } catch {
            /* one journey's cases missing must not blank the whole map */
          }
        }));
        if (alive) setMap(next);
      } catch {
        /* journeys are additive context — never break the panel */
      }
    })();
    return () => { alive = false; };
  }, [appId]);

  return map;
}
