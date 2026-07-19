import type { Matrix, StagePackDocument } from './contracts';

const MATRIX_PREFIX = new TextEncoder().encode('mycelium.matrix.f64be.v1\0');
const MAX_SEQUENCE_LENGTH = 256;
const SQRT_TWO = Math.sqrt(2);
const SQRT_TWO_OVER_PI = Math.sqrt(2 / Math.PI);

function fail(code: string): never {
  throw new Error(code);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringField(value: unknown, code: string): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) fail(code);
  return value;
}

function booleanField(value: unknown, code: string): boolean {
  if (typeof value !== 'boolean') fail(code);
  return value;
}

function integerField(value: unknown, code: string): number {
  if (!Number.isInteger(value)) fail(code);
  return value as number;
}

function finiteNumber(value: unknown, code: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(code);
  return value;
}

function validateDigest(value: unknown, code: string): string {
  const text = stringField(value, code);
  if (!/^sha256:[0-9a-f]{64}$/.test(text)) fail(code);
  return text;
}

function copyVector(value: unknown, length: number, code: string): number[] {
  if (!Array.isArray(value)) fail(code);
  if (Number.isFinite(length) && value.length !== length) fail(code);
  return value.map((item) => finiteNumber(item, code));
}

function copyDynamicVector(value: unknown, code: string): number[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 16384) fail(code);
  return value.map((item) => finiteNumber(item, code));
}

function copyMatrix(value: unknown, rows: number, columns: number, code: string): Matrix {
  if (!Array.isArray(value) || value.length !== rows) fail(code);
  return value.map((row) => copyVector(row, columns, code));
}

function validateHidden(value: unknown, hiddenSize: number): Matrix {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_SEQUENCE_LENGTH) {
    fail('request_hidden_shape_invalid');
  }
  return value.map((row) => {
    if (!Array.isArray(row) || row.length !== hiddenSize) fail('request_hidden_shape_invalid');
    return row.map((item) => finiteNumber(item, 'request_hidden_nonfinite'));
  });
}

export async function matrixDigest(matrix: Matrix): Promise<string> {
  if (!Array.isArray(matrix) || matrix.length < 1 || matrix.length > MAX_SEQUENCE_LENGTH) {
    fail('matrix_digest_invalid');
  }
  const columns = matrix[0]?.length;
  if (!Number.isInteger(columns) || columns < 1 || columns > 4096) fail('matrix_digest_invalid');
  const bytes = new Uint8Array(MATRIX_PREFIX.length + 8 + matrix.length * columns * 8);
  bytes.set(MATRIX_PREFIX, 0);
  const view = new DataView(bytes.buffer);
  let offset = MATRIX_PREFIX.length;
  view.setUint32(offset, matrix.length, false);
  offset += 4;
  view.setUint32(offset, columns, false);
  offset += 4;
  for (const row of matrix) {
    if (!Array.isArray(row) || row.length !== columns) fail('matrix_digest_invalid');
    for (const item of row) {
      if (typeof item !== 'number' || !Number.isFinite(item)) fail('matrix_digest_invalid');
      view.setFloat64(offset, item, false);
      offset += 8;
    }
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return (
    'sha256:' +
    [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
  );
}

function transpose(matrix: Matrix): Matrix {
  return matrix[0].map((_, column) => matrix.map((row) => row[column]));
}

function matmul(left: Matrix, right: Matrix): Matrix {
  if (left.length === 0 || right.length === 0 || left[0].length !== right.length) {
    fail('internal_matmul_shape_invalid');
  }
  const rows = left.length;
  const inner = right.length;
  const columns = right[0].length;
  const output: Matrix = Array.from({ length: rows }, () => Array(columns).fill(0));
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      let sum = 0;
      for (let index = 0; index < inner; index += 1) {
        sum += left[row][index] * right[index][column];
      }
      output[row][column] = sum;
    }
  }
  return output;
}

function addVector(matrix: Matrix, bias: number[]): Matrix {
  return matrix.map((row) => row.map((value, index) => value + bias[index]));
}

function add(left: Matrix, right: Matrix): Matrix {
  return left.map((row, rowIndex) => row.map((value, columnIndex) => value + right[rowIndex][columnIndex]));
}

function layerNorm(input: Matrix, weight: number[], bias: number[], epsilon: number): Matrix {
  return input.map((row) => {
    const mean = row.reduce((acc, value) => acc + value, 0) / row.length;
    const variance = row.reduce((acc, value) => acc + (value - mean) ** 2, 0) / row.length;
    const denominator = Math.sqrt(variance + epsilon);
    return row.map((value, index) => ((value - mean) / denominator) * weight[index] + bias[index]);
  });
}

function geluNew(value: number): number {
  return 0.5 * value * (1 + Math.tanh(SQRT_TWO_OVER_PI * (value + 0.044715 * value ** 3)));
}

function gelu(value: number): number {
  return 0.5 * value * (1 + erf(value / SQRT_TWO));
}

function erf(value: number): number {
  const sign = value < 0 ? -1 : 1;
  const x = Math.abs(value);
  const a1 = 0.254829592;
  const a2 = -0.284496736;
  const a3 = 1.421413741;
  const a4 = -1.453152027;
  const a5 = 1.061405429;
  const p = 0.3275911;
  const t = 1 / (1 + p * x);
  const y = 1 - (((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * Math.exp(-x * x));
  return sign * y;
}

function softmax(values: number[]): number[] {
  const max = Math.max(...values);
  const exp = values.map((value) => Math.exp(value - max));
  const denominator = exp.reduce((acc, value) => acc + value, 0);
  return exp.map((value) => value / denominator);
}

function splitQkv(projected: Matrix, hiddenSize: number): [Matrix, Matrix, Matrix] {
  return [
    projected.map((row) => row.slice(0, hiddenSize)),
    projected.map((row) => row.slice(hiddenSize, 2 * hiddenSize)),
    projected.map((row) => row.slice(2 * hiddenSize, 3 * hiddenSize)),
  ];
}

function causalAttention(
  query: Matrix,
  key: Matrix,
  value: Matrix,
  headCount: number,
  scaleAttnWeights: boolean,
): Matrix {
  const sequenceLength = query.length;
  const hiddenSize = query[0].length;
  const headDim = hiddenSize / headCount;
  const output: Matrix = Array.from({ length: sequenceLength }, () => Array(hiddenSize).fill(0));
  const scale = scaleAttnWeights ? 1 / Math.sqrt(headDim) : 1;
  for (let head = 0; head < headCount; head += 1) {
    const start = head * headDim;
    const end = start + headDim;
    for (let position = 0; position < sequenceLength; position += 1) {
      const scores: number[] = [];
      for (let source = 0; source <= position; source += 1) {
        let score = 0;
        for (let dim = start; dim < end; dim += 1) {
          score += query[position][dim] * key[source][dim];
        }
        scores.push(score * scale);
      }
      const weights = softmax(scores);
      for (let dim = start; dim < end; dim += 1) {
        let weighted = 0;
        for (let source = 0; source <= position; source += 1) {
          weighted += weights[source] * value[source][dim];
        }
        output[position][dim] = weighted;
      }
    }
  }
  return output;
}

class Tensors {
  private values: Record<string, unknown>;
  private hiddenSize: number;
  private innerSize: number;

  constructor(values: Record<string, unknown>, hiddenSize: number, innerSize: number) {
    this.values = values;
    this.hiddenSize = hiddenSize;
    this.innerSize = innerSize;
  }

  vector(name: string, length = this.hiddenSize): number[] {
    if (!(name in this.values)) fail('stage_pack_tensor_missing');
    return copyVector(this.values[name], length, 'stage_pack_tensor_shape_invalid');
  }

  matrix(name: string, rows: number, columns: number): Matrix {
    if (!(name in this.values)) fail('stage_pack_tensor_missing');
    return copyMatrix(this.values[name], rows, columns, 'stage_pack_tensor_shape_invalid');
  }

  get cAttnWeight(): Matrix {
    return this.matrix('attn.c_attn.weight', this.hiddenSize, 3 * this.hiddenSize);
  }

  get cAttnBias(): number[] {
    return this.vector('attn.c_attn.bias', 3 * this.hiddenSize);
  }

  get cProjWeight(): Matrix {
    return this.matrix('attn.c_proj.weight', this.hiddenSize, this.hiddenSize);
  }

  get cProjBias(): number[] {
    return this.vector('attn.c_proj.bias');
  }

  get fcWeight(): Matrix {
    return this.matrix('mlp.c_fc.weight', this.hiddenSize, this.innerSize);
  }

  get fcBias(): number[] {
    return this.vector('mlp.c_fc.bias', this.innerSize);
  }

  get mlpProjWeight(): Matrix {
    return this.matrix('mlp.c_proj.weight', this.innerSize, this.hiddenSize);
  }

  get mlpProjBias(): number[] {
    return this.vector('mlp.c_proj.bias');
  }
}

export class BrowserPixelStage {
  readonly assignmentId: string;
  readonly stageId: string;
  readonly packDigest: string;
  readonly startLayer: number;
  readonly endLayerExclusive: number;
  private readonly hiddenSize: number;
  private readonly headCount: number;
  private readonly epsilon: number;
  private readonly activationFunction: string;
  private readonly scaleAttnWeights: boolean;
  private readonly tensors: Tensors;

  private constructor(pack: StagePackDocument, tensors: Record<string, unknown>) {
    this.assignmentId = pack.assignment_id;
    this.stageId = pack.stage_id;
    this.packDigest = pack.pack_digest;
    this.startLayer = pack.start_layer;
    this.endLayerExclusive = pack.end_layer_exclusive;
    this.hiddenSize = pack.hidden_size;
    this.headCount = pack.n_head;
    this.epsilon = pack.epsilon;
    this.activationFunction = pack.activation_function;
    this.scaleAttnWeights = pack.scale_attn_weights;
    const innerSize = copyDynamicVector(tensors['mlp.c_fc.bias'], 'stage_pack_tensor_shape_invalid').length;
    this.tensors = new Tensors(tensors, this.hiddenSize, innerSize);
  }

  static fromDocument(document: unknown): BrowserPixelStage {
    if (!isRecord(document)) fail('stage_pack_invalid');
    const protocol = stringField(document.protocol, 'stage_pack_protocol_invalid');
    if (protocol !== 'mycelium.pixel_stage_pack.v1') fail('stage_pack_protocol_invalid');
    if (document.route_ready !== false) fail('stage_pack_route_ready_invalid');
    const assignmentId = stringField(document.assignment_id, 'stage_pack_identity_invalid');
    const stageId = stringField(document.stage_id, 'stage_pack_identity_invalid');
    const packDigest = validateDigest(document.pack_digest, 'stage_pack_digest_invalid');
    validateDigest(document.manifest_digest, 'stage_pack_digest_invalid');
    const startLayer = integerField(document.start_layer, 'stage_pack_layer_invalid');
    const endLayerExclusive = integerField(document.end_layer_exclusive, 'stage_pack_layer_invalid');
    const hiddenSize = integerField(document.hidden_size, 'stage_pack_shape_invalid');
    const headCount = integerField(document.n_head, 'stage_pack_shape_invalid');
    const epsilon = finiteNumber(document.epsilon, 'stage_pack_epsilon_invalid');
    const activationFunction = stringField(document.activation_function, 'stage_pack_activation_function_unsupported');
    if (endLayerExclusive !== startLayer + 1 || startLayer < 0) fail('stage_pack_layer_invalid');
    if (hiddenSize < 1 || hiddenSize > 4096 || hiddenSize % headCount !== 0) fail('stage_pack_shape_invalid');
    if (headCount < 1 || headCount > hiddenSize) fail('stage_pack_shape_invalid');
    if (epsilon <= 0) fail('stage_pack_epsilon_invalid');
    if (!['gelu_new', 'gelu'].includes(activationFunction)) {
      fail('stage_pack_activation_function_unsupported');
    }
    const unsupportedFlags = [
      ['scale_attn_by_inverse_layer_idx', false],
      ['reorder_and_upcast_attn', false],
      ['add_cross_attention', false],
    ] as const;
    for (const [field, expected] of unsupportedFlags) {
      if (document[field] !== expected) fail(`stage_pack_${field}_unsupported`);
    }
    const scaleAttnWeights = booleanField(document.scale_attn_weights, 'stage_pack_attention_flag_invalid');
    if (!isRecord(document.tensors)) fail('stage_pack_tensors_invalid');
    const prefix = `transformer.h.${startLayer}.`;
    const tensors: Record<string, unknown> = {};
    for (const [name, value] of Object.entries(document.tensors)) {
      if (!name.startsWith(prefix)) fail('stage_pack_tensor_namespace_invalid');
      tensors[name.slice(prefix.length)] = value;
    }
    const normalized: StagePackDocument = {
      protocol,
      route_ready: false,
      assignment_id: assignmentId,
      stage_id: stageId,
      start_layer: startLayer,
      end_layer_exclusive: endLayerExclusive,
      n_head: headCount,
      hidden_size: hiddenSize,
      epsilon,
      activation_function: activationFunction,
      scale_attn_weights: scaleAttnWeights,
      scale_attn_by_inverse_layer_idx: false,
      reorder_and_upcast_attn: false,
      add_cross_attention: false,
      pack_digest: packDigest,
      manifest_digest: document.manifest_digest as string,
      tensors: document.tensors as Record<string, Matrix | number[]>,
    };
    const stage = new BrowserPixelStage(normalized, tensors);
    stage.validateTensors();
    return stage;
  }

  private validateTensors(): void {
    this.tensors.vector('ln_1.weight');
    this.tensors.vector('ln_1.bias');
    this.tensors.cAttnWeight;
    this.tensors.cAttnBias;
    this.tensors.cProjWeight;
    this.tensors.cProjBias;
    this.tensors.vector('ln_2.weight');
    this.tensors.vector('ln_2.bias');
    this.tensors.fcWeight;
    this.tensors.fcBias;
    this.tensors.mlpProjWeight;
    this.tensors.mlpProjBias;
  }

  execute(hidden: unknown): Matrix {
    const x = validateHidden(hidden, this.hiddenSize);
    const norm1 = layerNorm(x, this.tensors.vector('ln_1.weight'), this.tensors.vector('ln_1.bias'), this.epsilon);
    const projected = addVector(matmul(norm1, this.tensors.cAttnWeight), this.tensors.cAttnBias);
    const [query, key, value] = splitQkv(projected, this.hiddenSize);
    const attention = causalAttention(query, key, value, this.headCount, this.scaleAttnWeights);
    const attentionProjected = addVector(matmul(attention, this.tensors.cProjWeight), this.tensors.cProjBias);
    const residual = add(x, attentionProjected);
    const norm2 = layerNorm(
      residual,
      this.tensors.vector('ln_2.weight'),
      this.tensors.vector('ln_2.bias'),
      this.epsilon,
    );
    const mlpHidden = addVector(matmul(norm2, this.tensors.fcWeight), this.tensors.fcBias).map((row) =>
      row.map((value) => (this.activationFunction === 'gelu_new' ? geluNew(value) : gelu(value))),
    );
    const mlpProjected = addVector(matmul(mlpHidden, this.tensors.mlpProjWeight), this.tensors.mlpProjBias);
    return add(residual, mlpProjected);
  }
}
