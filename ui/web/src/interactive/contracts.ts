export type Matrix = number[][];

export interface BrowserWorkDocument {
  protocol: 'mycelium.browser_stage_work.v1';
  job_id: string;
  request_id: string;
  assignment_id: string;
  stage_id: string;
  pack_digest: string;
  input_digest: string;
  hidden: Matrix;
  route_ready: false;
}

export interface BrowserStageResultDocument {
  protocol: 'mycelium.browser_stage_result.v1';
  job_id: string;
  request_id: string;
  assignment_id: string;
  stage_id: string;
  pack_digest: string;
  input_digest: string;
  output: Matrix;
  output_digest: string;
  route_ready: false;
}

export interface StagePackDocument {
  protocol: 'mycelium.pixel_stage_pack.v1';
  route_ready: false;
  assignment_id: string;
  stage_id: string;
  start_layer: number;
  end_layer_exclusive: number;
  n_head: number;
  hidden_size: number;
  epsilon: number;
  activation_function: string;
  scale_attn_weights: boolean;
  scale_attn_by_inverse_layer_idx: boolean;
  reorder_and_upcast_attn: boolean;
  add_cross_attention: boolean;
  pack_digest: string;
  manifest_digest: string;
  tensors: Record<string, Matrix | number[]>;
}
