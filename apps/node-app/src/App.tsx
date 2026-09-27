import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';

type CoreInfo = { core_version: string; target: string; started_ms: number };

export function App() {
  const [info, setInfo] = useState<CoreInfo | null>(null);
  const [taps, setTaps] = useState(0);
  const [rtt, setRtt] = useState<number | null>(null);
  const [svc, setSvc] = useState<string>('stopped');
  useEffect(() => {
    const t0 = performance.now();
    invoke<CoreInfo>('core_info').then((v) => {
      setInfo(v);
      setRtt(performance.now() - t0);
      console.log(`MYCELIUM_SPIKE first_invoke_ms=${(performance.now() - t0).toFixed(2)} since_nav_ms=${performance.now().toFixed(1)}`);
    });
  }, []);
  return (
    <main>
      <h1>Mycelium Node</h1>
      <p>{info ? `core ${info.core_version} on ${info.target}` : 'starting…'}</p>
      <p>invoke rtt: {rtt?.toFixed(2)} ms</p>
      <button onClick={() => setTaps(taps + 1)}>tap {taps}</button>
      <p>service: {svc}</p>
      <button onClick={() => invoke<{ running: boolean; mode: string }>('serving_start').then((s) => setSvc(`${s.running} ${s.mode}`)).catch((e) => setSvc(String(e)))}>start serving</button>
      <button onClick={() => invoke<{ running: boolean; mode: string }>('serving_stop').then((s) => setSvc(`${s.running} ${s.mode}`)).catch((e) => setSvc(String(e)))}>stop serving</button>
    </main>
  );
}
