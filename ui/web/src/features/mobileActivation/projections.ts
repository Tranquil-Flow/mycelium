// SPDX-License-Identifier: AGPL-3.0-only
//
// Eight-workspace projection of mobile activation rungs.
//
// The source is one public generation of the mobile rung ledger: for each
// pseudonymous mobile member, the four separately authored rungs (signed
// membership, capability eligibility, artifact readiness, operation
// qualification), each role's qualification decision, and the freshness of the
// native evidence behind them.  Helpers are pure.
//
// Invariants:
//
//   * one rung never authors another -- each rung renders only its own decision
//     and blockers, and a later rung is never shown as earned because an earlier
//     one is;
//   * unknown-not-zero -- missing evidence renders as "unknown" with its blocker;
//   * native versus synthetic truth stays separate -- only native device
//     observations can show an eligible operation;
//   * Apple mobile is deferred -- an iOS or iPadOS member stays visibly ineligible
//     with the deferral as its blocker;
//   * privacy -- members are pseudonymous (sha256: or family: references); raw
//     endpoint identities, addresses or device names are rejected at the boundary;
//   * product copy names platforms and roles, never phone brands or internal gate
//     labels.

export type Freshness = 'live' | 'stale' | 'historical' | 'fixture' | 'unknown';
export type RungId =
  | 'signed_membership'
  | 'capability_eligibility'
  | 'artifact_readiness'
  | 'operation_qualification';
export type RungDecision =
  | 'eligible'
  | 'ineligible'
  | 'not_applicable'
  | 'withheld'
  | 'unknown'
  | 'stale'
  | 'revoked';
export type MobileRole = 'probe_contributor' | 'speculative_draft_worker' | 'qualified_model_stage';
export type PlatformFamily = 'android' | 'ios' | 'ipados' | 'unknown';
export type SourceKind =
  | 'native_device_observation'
  | 'synthetic_conformance'
  | 'termux_conformance'
  | 'operator_declared'
  | 'unknown';

export interface RungState {
  decision: RungDecision;
  blockers: string[];
  sourceKind: SourceKind;
}

export interface MobileMember {
  memberRef: string;
  platformFamily: PlatformFamily;
  lifecycle: 'foreground' | 'background' | 'drained' | 'stale' | 'unknown';
  rungs: Partial<Record<RungId, RungState>>;
  roles: Partial<Record<MobileRole, RungState>>;
  thermalMaxCelsius: number | null;
  batteryPercent: number | null;
  relayOnline: boolean | null;
  incidents: string[];
}

export interface MobileRungSource {
  generation: number;
  freshness: Freshness;
  members: MobileMember[];
}

export class MobileProjectionError extends Error {
  constructor(code: string) {
    super(code);
    this.name = 'MobileProjectionError';
  }
}

export const RUNG_ORDER: RungId[] = [
  'signed_membership',
  'capability_eligibility',
  'artifact_readiness',
  'operation_qualification',
];
export const WORKSPACES = [
  'inference',
  'device_lab',
  'network',
  'nodes',
  'plans',
  'readiness',
  'incidents',
  'settings',
] as const;
export type Workspace = (typeof WORKSPACES)[number];

const RUNG_LABELS: Record<RungId, string> = {
  signed_membership: 'Signed membership',
  capability_eligibility: 'Capability eligibility',
  artifact_readiness: 'Artifact readiness',
  operation_qualification: 'Operation qualification',
};
const ROLE_LABELS: Record<MobileRole, string> = {
  probe_contributor: 'Probe contributor',
  speculative_draft_worker: 'Draft worker (optional)',
  qualified_model_stage: 'Model stage',
};
const PLATFORM_LABELS: Record<PlatformFamily, string> = {
  android: 'Android',
  ios: 'iOS',
  ipados: 'iPadOS',
  unknown: 'Unknown platform',
};
export const APPLE_DEFERRED_BLOCKER = 'apple_mobile_deferred';
const MEMBER_REF_RE = /^(sha256:[0-9a-f]{64}|family:[0-9a-f]{16,64}|member:[0-9a-z-]{1,64})$/;

export interface Row {
  label: string;
  value: string;
  state: 'current' | 'blocked' | 'unknown' | 'deferred';
  blockers: string[];
}

export interface WorkspaceProjection {
  workspace: Workspace;
  generation: number;
  freshness: Freshness;
  rows: Row[];
}

function isApple(platform: PlatformFamily): boolean {
  return platform === 'ios' || platform === 'ipados';
}

function memberLabel(member: MobileMember): string {
  const suffix = member.memberRef.includes(':') ? member.memberRef.split(':')[1].slice(0, 8) : '';
  return `${PLATFORM_LABELS[member.platformFamily]} member ${suffix}`;
}

export function validateMember(member: MobileMember): MobileMember {
  if (!MEMBER_REF_RE.test(member.memberRef)) {
    throw new MobileProjectionError('member_ref_not_pseudonymous');
  }
  return member;
}

/** One rung, rendered only from its own record; absence is unknown, never earned. */
export function rungRow(member: MobileMember, rung: RungId): Row {
  const label = `${memberLabel(member)}: ${RUNG_LABELS[rung]}`;
  if (isApple(member.platformFamily)) {
    return { label, value: 'deferred', state: 'deferred', blockers: [APPLE_DEFERRED_BLOCKER] };
  }
  const state = member.rungs[rung];
  if (state === undefined) {
    return { label, value: 'unknown', state: 'unknown', blockers: ['rung_record_absent'] };
  }
  const nativeOnly = rung === 'operation_qualification' && state.sourceKind !== 'native_device_observation';
  const current = (state.decision === 'eligible' || state.decision === 'not_applicable') && !nativeOnly;
  return {
    label,
    value: nativeOnly && state.decision === 'eligible' ? 'withheld' : state.decision,
    state: current ? 'current' : state.decision === 'unknown' ? 'unknown' : 'blocked',
    blockers: nativeOnly ? [...state.blockers, 'non_native_source_cannot_qualify'] : state.blockers,
  };
}

export function roleRow(member: MobileMember, role: MobileRole): Row {
  const label = `${memberLabel(member)}: ${ROLE_LABELS[role]}`;
  if (isApple(member.platformFamily)) {
    return { label, value: 'deferred', state: 'deferred', blockers: [APPLE_DEFERRED_BLOCKER] };
  }
  const state = member.roles[role];
  if (state === undefined) return { label, value: 'unknown', state: 'unknown', blockers: ['role_not_evaluated'] };
  const eligible = state.decision === 'eligible' && state.sourceKind === 'native_device_observation';
  return { label, value: eligible ? 'eligible' : state.decision === 'eligible' ? 'withheld' : state.decision, state: eligible ? 'current' : 'blocked', blockers: state.blockers };
}

function metric(label: string, value: number | boolean | null): Row {
  if (value === null) return { label, value: 'unknown', state: 'unknown', blockers: ['native_observation_absent'] };
  return { label, value: typeof value === 'boolean' ? (value ? 'yes' : 'no') : String(value), state: 'current', blockers: [] };
}

function stageEligible(member: MobileMember): boolean {
  return roleRow(member, 'qualified_model_stage').state === 'current';
}

const PROJECTORS: Record<Workspace, (member: MobileMember) => Row[]> = {
  inference: (member) => [
    {
      label: `${memberLabel(member)}: participates in inference`,
      value: stageEligible(member) ? 'eligible stage' : 'not participating',
      state: stageEligible(member) ? 'current' : 'blocked',
      blockers: stageEligible(member) ? [] : roleRow(member, 'qualified_model_stage').blockers,
    },
  ],
  device_lab: (member) => [
    ...RUNG_ORDER.map((rung) => rungRow(member, rung)),
    ...(Object.keys(ROLE_LABELS) as MobileRole[]).map((role) => roleRow(member, role)),
  ],
  network: (member) => [metric(`${memberLabel(member)}: relay path online`, member.relayOnline)],
  nodes: (member) => [
    { label: `${memberLabel(member)}: platform`, value: PLATFORM_LABELS[member.platformFamily], state: 'current', blockers: [] },
    { label: `${memberLabel(member)}: lifecycle`, value: member.lifecycle, state: member.lifecycle === 'unknown' ? 'unknown' : 'current', blockers: [] },
    metric(`${memberLabel(member)}: thermal (°C)`, member.thermalMaxCelsius),
    metric(`${memberLabel(member)}: battery (%)`, member.batteryPercent),
  ],
  plans: (member) => [
    {
      label: `${memberLabel(member)}: placement intent`,
      value: stageEligible(member) ? 'may be planned as a stage' : 'excluded',
      state: stageEligible(member) ? 'current' : 'blocked',
      blockers: stageEligible(member) ? [] : ['operation_qualification_not_current'],
    },
  ],
  readiness: (member) => RUNG_ORDER.map((rung) => rungRow(member, rung)),
  incidents: (member) =>
    member.incidents.length === 0
      ? [{ label: `${memberLabel(member)}: incidents`, value: 'none recorded', state: 'current', blockers: [] }]
      : member.incidents.map((incident) => ({ label: `${memberLabel(member)}: incident`, value: incident, state: 'blocked' as const, blockers: [] })),
  settings: (member) => [
    isApple(member.platformFamily)
      ? { label: `${memberLabel(member)}: contribution`, value: 'disabled', state: 'deferred', blockers: [APPLE_DEFERRED_BLOCKER] }
      : { label: `${memberLabel(member)}: contribution`, value: 'opt-in per role', state: 'current', blockers: [] },
  ],
};

export function projectWorkspace(source: MobileRungSource, workspace: Workspace): WorkspaceProjection {
  const members = source.members.map(validateMember);
  return {
    workspace,
    generation: source.generation,
    freshness: source.freshness,
    rows: members.flatMap((member) => PROJECTORS[workspace](member)),
  };
}

/** One public generation reconstructed identically in all eight workspaces. */
export function projectAllWorkspaces(source: MobileRungSource): WorkspaceProjection[] {
  return WORKSPACES.map((workspace) => projectWorkspace(source, workspace));
}
