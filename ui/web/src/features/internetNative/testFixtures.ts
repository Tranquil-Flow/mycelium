import productSnapshotFixture from '../../../../../contracts/compatibility-fixtures/product-snapshot-v1.json';
import bootstrapStatusFixture from '../../../../../contracts/compatibility-fixtures/internet-bootstrap-status-v1.json';
import activationObservationFixture from '../../../../../contracts/compatibility-fixtures/internet-activation-observation-v1.json';
import relayProjectionFixture from '../../../../../contracts/compatibility-fixtures/relay-projection-v1.json';
import qualificationFixture from '../../../../../contracts/compatibility-fixtures/internet-native-qualification-v1.json';

export const INTERNET_NATIVE_FIXTURE = Object.freeze({
  bootstrap_status: bootstrapStatusFixture,
  activation_observation: activationObservationFixture,
  activation_history: [activationObservationFixture],
  relay_projection: relayProjectionFixture,
  qualification: qualificationFixture,
});

export function productSnapshotWithInternetNative(
  internetNative: unknown = INTERNET_NATIVE_FIXTURE,
): Record<string, unknown> {
  return {
    ...structuredClone(productSnapshotFixture),
    internet_native: structuredClone(internetNative),
  };
}
