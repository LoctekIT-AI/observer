import { appendFileSync, closeSync, fsyncSync, mkdirSync, openSync } from 'node:fs';
import { dirname, isAbsolute, resolve } from 'node:path';
import { homedir } from 'node:os';
import { createHash } from 'node:crypto';

export const name = 'dsh-observer';
export const inject = ['tools', 'sessions'];
const pluginId = 'dsh-observer';
const identifier = (value, field, limit = 256) => {
  if (typeof value !== 'string' || !value || value !== value.trim() || value.length > limit) {
    throw new TypeError(`dsh-observer: invalid ${field}`);
  }
  return value;
};
function validateConfig(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('dsh-observer: config must be an object');
  for (const key of Object.keys(value)) if (!['eventFile', 'installationId'].includes(key)) throw new TypeError(`dsh-observer: unknown config field ${key}`);
  const eventFile = value.eventFile ?? resolve(homedir(), '.aihub/observer/dsh/component-events.jsonl');
  if (typeof eventFile !== 'string' || !isAbsolute(eventFile)) throw new TypeError('dsh-observer: eventFile must be absolute');
  return { eventFile, installationId: identifier(value.installationId ?? 'install-dsh-1', 'installationId') };
}
// Cordis 4 使用 Standard Schema；校验失败直接成为原生插件加载错误。
export const Config = { '~standard': { version: 1, vendor: 'dsh-observer', validate(value) {
  try { return { value: validateConfig(value) }; }
  catch (error) { return { issues: [{ message: error.message }] }; }
} } };
const stableId = (...parts) => `dsh:${createHash('sha256').update(JSON.stringify(parts)).digest('hex')}`;
function eventBase(config, sessionId, callId, occurredAt) {
  const invocationId = stableId(config.installationId, sessionId, callId);
  return {
    schema_version: 'component_event.v1', event_id: stableId(invocationId, 'terminal'),
    invocation_id: invocationId, run_id: identifier(sessionId, 'session id'), runtime: 'dsh',
    plugin_id: pluginId, installation_id: config.installationId,
    occurred_at: occurredAt, started_at: null, ended_at: occurredAt,
    parent_invocation_id: null, evidence_level: 'verified', source_type: 'dsh_native_metadata_jsonl',
  };
}
export function apply(ctx, suppliedConfig) {
  const config = validateConfig(suppliedConfig);
  const calls = new Map();
  // 仅保留原生 token → 调用关联，不包裹业务执行或读取参数正文。
  ctx.on('tools/execute', (exec, next) => {
    if (exec.agent) calls.set(exec.token, { sessionId: exec.agent.session.id, callId: exec.callId });
    return next();
  });
  const append = record => {
    mkdirSync(dirname(config.eventFile), { recursive: true, mode: 0o700 });
    const descriptor = openSync(config.eventFile, 'a', 0o600);
    try {
      appendFileSync(descriptor, JSON.stringify(record) + '\n', 'utf8');
      fsyncSync(descriptor);
    } finally { closeSync(descriptor); }
  };
  const observe = (seam, callback) => (...args) => {
    try { return callback(...args); }
    catch (error) {
      // INFO is exported by default; report only collection error metadata, never business content.
      ctx.logger.info(`dsh-observer collection failed (${seam}): ${error.name}${error.code ? ` ${error.code}` : ''}`);
      // A DSH update changed the hook data: still count the call (inferred), and
      // never let collection break the user's tool call.
      if (seam === 'tools/result') {
        try {
          const [exec] = args;
          const sessionId = typeof exec?.agent?.session?.id === 'string' && exec.agent.session.id ? exec.agent.session.id : 'dsh-unknown-session';
          const callId = typeof exec?.callId === 'string' && exec.callId ? exec.callId : `unknown-${Date.now()}-${Math.random().toString(16).slice(2)}`;
          const record = eventBase(config, identifier(sessionId, 'session id'), callId, new Date().toISOString());
          record.component_kind = 'unknown';
          record.component_id = 'unrecognized:dsh_tool';
          record.error_code = null;
          record.stage = 'completed';
          record.observation_kind = 'unrecognized_call';
          record.evidence_source = 'dsh:tools/result';
          record.evidence_level = 'inferred';
          append(record);
        } catch { /* nothing else can be recorded safely */ }
      }
      return undefined;
    }
  };
  ctx.on('tools/result', observe('tools/result', (exec, result) => {
    if (!exec.agent) return;
    const sessionId = exec.agent.session.id;
    const callId = identifier(exec.callId, 'call id');
    const toolName = identifier(exec.name, 'tool name', 512);
    const parent = calls.get(exec.parent);
    calls.delete(exec.token);
    const record = eventBase(config, sessionId, callId, new Date().toISOString());
    let componentId = toolName;
    let kind = toolName.startsWith('mcp__') ? 'mcp_tool' : 'tool';
    if (toolName === 'skill') {
      const skillName = result.isError ? exec.arguments.name : result.value.name;
      if (typeof skillName === 'string' && skillName && skillName === skillName.trim() && skillName.length <= 512) {
        kind = 'skill'; componentId = skillName;
      }
    }
    // MCP 模型可见名称不等于原始 server/tool 身份，完整保留，不拆分。
    const code = result.isError ? (result.error?.info?.code ?? null) : null;
    if (code != null) identifier(code, 'native error code', 128);
    record.component_kind = kind;
    record.component_id = componentId;
    record.error_code = code;
    record.stage = !result.isError ? 'completed' : ['ABORTED', 'ABORTED_BEFORE_DISPATCH'].includes(code) ? 'cancelled' : code === 'TOOL_TIMEOUT' ? 'timeout' : 'failed';
    record.observation_kind = kind === 'skill' ? 'skill_tool_result' : 'tool_result';
    record.evidence_source = 'dsh:tools/result';
    if (parent && parent.sessionId === sessionId) record.parent_invocation_id = stableId(config.installationId, parent.sessionId, parent.callId);
    append(record);
  }));
  ctx.on('session/event', observe('session/event', (session, event) => {
    if (event.type !== 'user/message' || event.data.source?.kind !== 'skill-invocation' || event.data.source.form !== 'instructions') return;
    const record = eventBase(config, session.id, `session-event:${event.seq}`, new Date(event.time).toISOString());
    record.component_kind = 'skill';
    record.component_id = identifier(event.data.source.name, 'skill name', 512);
    record.stage = 'completed'; record.error_code = null;
    record.observation_kind = 'skill_user_invocation'; record.evidence_source = 'dsh:session/event:skill-invocation';
    append(record);
  }));
  ctx.on('dispose', () => calls.clear());
}
