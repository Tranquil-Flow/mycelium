import type { InferenceAcceptedResponse, InferenceCancelResponse, InferenceEvent, InferenceSubmission, ProductQualification } from './contracts';
import { InferenceClientError, type InferenceClient } from '../features/inference/requestClient';
import { makeProductQualificationFixture } from '../test/productFixtures';

export class FixtureInferenceClient implements InferenceClient {
  async loadQualification(): Promise<ProductQualification> { return makeProductQualificationFixture(); }
  async submit(_submission: InferenceSubmission): Promise<InferenceAcceptedResponse> { throw new InferenceClientError('fixture_source_not_authoritative', false); }
  async stream(_request: InferenceAcceptedResponse, _lastEventId: number | null, _onEvent: (event: InferenceEvent) => void): Promise<void> { throw new InferenceClientError('fixture_source_not_authoritative', false); }
  async cancel(_request: InferenceAcceptedResponse): Promise<InferenceCancelResponse> { throw new InferenceClientError('fixture_source_not_authoritative', false); }
}
