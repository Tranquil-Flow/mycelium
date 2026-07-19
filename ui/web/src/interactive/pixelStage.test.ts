import { describe, expect, it } from 'vitest';
import fixture from '../test/browserStageVectors.json';
import { BrowserPixelStage, matrixDigest } from './pixelStage';

function maxError(left: number[][], right: number[][]): number {
  return Math.max(
    ...left.flatMap((row, rowIndex) =>
      row.map((value, columnIndex) => Math.abs(value - right[rowIndex][columnIndex])),
    ),
  );
}

describe('BrowserPixelStage', () => {
  it('executes Python PixelStage vectors with cross-runtime parity', async () => {
    const stage = BrowserPixelStage.fromDocument(fixture.pack);
    for (const vector of fixture.vectors) {
      expect(await matrixDigest(vector.input)).toBe(vector.input_digest);
      const output = stage.execute(vector.input);
      expect(maxError(output, vector.output)).toBeLessThan(1e-12);
      expect(await matrixDigest(output)).toMatch(/^sha256:[0-9a-f]{64}$/);
    }
  });

  it('fails closed on pack identity, unsupported semantics, and tensor shape', () => {
    expect(() => BrowserPixelStage.fromDocument({ ...fixture.pack, route_ready: true })).toThrow(
      'stage_pack_route_ready_invalid',
    );
    expect(() =>
      BrowserPixelStage.fromDocument({ ...fixture.pack, activation_function: 'relu' }),
    ).toThrow('stage_pack_activation_function_unsupported');
    expect(() =>
      BrowserPixelStage.fromDocument({
        ...fixture.pack,
        tensors: { ...fixture.pack.tensors, 'transformer.h.1.ln_1.weight': [1] },
      }),
    ).toThrow('stage_pack_tensor_shape_invalid');
  });

  it('fails closed on malformed hidden matrices', () => {
    const stage = BrowserPixelStage.fromDocument(fixture.pack);
    expect(() => stage.execute([])).toThrow('request_hidden_shape_invalid');
    expect(() => stage.execute([[1, 2]])).toThrow('request_hidden_shape_invalid');
    expect(() => stage.execute([[1, 2, 3, Number.NaN]])).toThrow(
      'request_hidden_nonfinite',
    );
  });

  it('uses a binary float64 matrix digest that distinguishes shape and signed zero', async () => {
    expect(await matrixDigest([[1, 2], [3, 4]])).not.toBe(
      await matrixDigest([[1, 2, 3, 4]]),
    );
    expect(await matrixDigest([[0]])).not.toBe(await matrixDigest([[-0]]));
    await expect(matrixDigest([[Number.POSITIVE_INFINITY]])).rejects.toThrow(
      'matrix_digest_invalid',
    );
  });
});
