import { useEffect, useState } from 'react';
import { bootstrapProductGatewaySession } from '../observatory/live/productGatewaySession';
import { ProductEvidenceProvider, useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import { createLiveProductEvidenceSource, type LiveProductEvidenceSource } from '../productEvidence/source';
import { productEvidenceRouteReady } from '../productEvidence/ProductEvidenceWorkspace';
import { ConsumerDemoPage } from './ConsumerDemoPage';
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
  return <ConsumerDemoPage externalBlockReason={reason} />;
}

/** Production root: only real same-origin clients. URL/env fixture switches cannot
 * select a demonstration here. Bootstrap failures leave the consumer visible and blocked. */
export function ConsumerLiveRoute() {
  const [attempt, setAttempt] = useState(0);
  const [connection, setConnection] = useState<Connection>({ phase: 'connecting' });
  useEffect(() => {
    let active = true;
    setConnection({ phase: 'connecting' });
    void bootstrapProductGatewaySession().then(bootstrap => {
      if (!active) return;
      if (bootstrap.source_mode !== 'live') {
        setConnection({ phase: 'unavailable', code: 'product_gateway_source_mode_mismatch' });
        return;
      }
      setConnection({ phase: 'ready', source: createLiveProductEvidenceSource() });
    }).catch(() => {
      if (active) setConnection({ phase: 'unavailable', code: 'product_gateway_unavailable' });
    });
    return () => { active = false; };
  }, [attempt]);
  return <ProductEvidenceProvider source={connection.source}>
    <div className={styles.connection} role={connection.phase === 'unavailable' ? 'alert' : 'status'} aria-live="polite">
      {connection.phase === 'connecting' ? 'Connecting to the same-origin product gateway…'
        : connection.phase === 'ready' ? 'Live gateway session established. Route qualification is checked separately.'
        : <><span>Gateway unavailable · <code>{connection.code}</code>. No fallback was attempted.</span><button type="button" onClick={() => setAttempt(value => value + 1)}>Retry connection</button></>}
    </div>
    <QualifiedConsumer connection={connection} />
  </ProductEvidenceProvider>;
}
