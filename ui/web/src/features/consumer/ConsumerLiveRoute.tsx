// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useState } from 'react';
import { bootstrapProductGatewaySession } from '../observatory/live/productGatewaySession';
import { ProductEvidenceProvider, useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import { createLiveProductEvidenceSource, type LiveProductEvidenceSource } from '../productEvidence/source';
import { productEvidenceRouteReady } from '../productEvidence/ProductEvidenceWorkspace';
import { ConsumerDemoPage } from './ConsumerDemoPage';
import { useElapsedSeconds } from './useElapsedSeconds';
import styles from './ConsumerDemoPage.module.css';

type Connection =
  | { readonly phase: 'connecting'; readonly source?: undefined }
  | { readonly phase: 'unavailable'; readonly code: string; readonly source?: undefined }
  | { readonly phase: 'ready'; readonly source: LiveProductEvidenceSource };

function QualifiedConsumer({ connection }: { readonly connection: Connection }) {
  const evidence = useProductEvidence();
  const reason = connection.phase === 'connecting' ? 'Connecting to the same-origin gateway.'
    : connection.phase === 'unavailable' ? 'Gateway unavailable. No fixture or direct-backend fallback.'
    : !productEvidenceRouteReady(evidence.latest) ? 'Inference disabled: live product evidence is not current and qualified.' : null;
  return <ConsumerDemoPage externalBlockReason={reason} bootstrapReady={connection.phase === 'ready'} />;
}

/** Production root: only real same-origin clients. URL/env fixture switches cannot
 * select a demonstration here. Bootstrap failures leave the consumer visible and blocked. */
export function ConsumerLiveRoute() {
  const [attempt, setAttempt] = useState(0);
  const [connection, setConnection] = useState<Connection>({ phase: 'connecting' });
  const [attemptSince, setAttemptSince] = useState<number | null>(null);
  const [lostSince, setLostSince] = useState<number | null>(null);
  const attemptElapsed = useElapsedSeconds(attemptSince);
  const lostElapsed = useElapsedSeconds(lostSince);
  useEffect(() => {
    let active = true;
    setAttemptSince(Date.now());
    setLostSince(null);
    setConnection({ phase: 'connecting' });
    void bootstrapProductGatewaySession().then(bootstrap => {
      if (!active) return;
      setAttemptSince(null);
      if (bootstrap.source_mode !== 'live') {
        setLostSince(Date.now());
        setConnection({ phase: 'unavailable', code: 'product_gateway_source_mode_mismatch' });
        return;
      }
      setConnection({ phase: 'ready', source: createLiveProductEvidenceSource() });
    }).catch(() => {
      if (!active) return;
      setAttemptSince(null);
      setLostSince(Date.now());
      setConnection({ phase: 'unavailable', code: 'product_gateway_unavailable' });
    });
    return () => { active = false; };
  }, [attempt]);
  return <ProductEvidenceProvider source={connection.source}>
    <div className={styles.connection} role={connection.phase === 'unavailable' ? 'alert' : 'status'} aria-live="polite">
      {connection.phase === 'connecting'
        ? attempt > 0 && attemptElapsed !== null
          ? <>Reconnecting (~{attemptElapsed} s) <span>No fallback is used while reconnecting.</span></>
          : 'Connecting to the same-origin product gateway…'
        : connection.phase === 'ready' ? 'Live gateway session established. Route qualification is checked separately.'
        : <><span>Route unavailable · <code>{connection.code}</code>{lostElapsed === null ? '' : ` for ~${lostElapsed} s`}. No fallback was attempted.</span><button type="button" onClick={() => setAttempt(value => value + 1)}>Retry connection</button></>}
    </div>
    <QualifiedConsumer connection={connection} />
  </ProductEvidenceProvider>;
}
