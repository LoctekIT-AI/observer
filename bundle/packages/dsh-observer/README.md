# Observer for DSH

Observer records metadata from native DSH final tool results and explicit native
user Skill invocation evidence. It preserves native results and never stores
arguments, Skill instructions, prompts, result bodies, or inferred contributions.
MCP identity is the complete normalized public name; raw server/tool identity,
start time, Skill version and token usage remain unprovided.

Verified baseline: DSH Desktop 0.9.0, `@deepseek-ai/dsh-*` 0.1.5-rc.2,
Cordis 4.0.2, Node 24.14.0, macOS arm64. Framework tests use genuine headless
services and do not prove Desktop UI acceptance. Other framework versions and
Windows DSH worker/installer behavior have not been verified.

For customer installation, follow the root Observer README and use the OS
bootstrap with explicit `dsh` selection, dedicated runtime/config/state paths,
and the actual existing native DSH profile and CLI. The bootstrap supplies
managed Python 3.12, Node and pnpm; it does not require global tools.

For direct maintenance with the prepared managed interpreter, source data can
live below `~/Library/Application Support/Observer/dsh`; pass that absolute path
with `--source-root` to the native installer and worker. `--check --no-flush` is
read-only prerequisite inspection; `--once --no-flush` does not prove delivery.

For another DSH profile, set `DSH_HOME` to its explicit harness root and choose
the existing profile directory under its `profiles` folder.

The installer requires Node and pnpm, uses native `dsh plugin --profile NAME add`,
and lets DSH reconcile its `dsh.bundle.patch` metadata. It installs a complete
package archive under the profile and preserves unrelated dependencies, bundle
layers and the user's `cordis.patch.yml`. The resulting bundle layer supplies a
unique profile installation ID and an absolute event path. Restart that profile
and verify its activation; Observer does not start Desktop itself.

The customer bootstrap prepares pinned managed Python 3.12 dependencies. The
worker LaunchAgent can be installed with `scripts/install_dsh_observer_launch_agent.py install` and
explicit worker/source/state/config arguments. Continuous delivery uses the
existing consent and Langfuse configuration. `--no-flush` is explicit local
inspection only and cannot mean remote delivery succeeded. Native receiver
failures remain pending in the existing outbox with attempts and `last_error`;
inspect that evidence before claiming remote acceptance.

Errors from plugin validation or append operations use the native Cordis/DSH
observer error channel. The plugin reports collection failures at INFO, visible through the
default native exporter, before rethrowing into native containment. Worker malformed-record and
I/O errors terminate the pass and leave a failed status; no alternate source is
substituted. Keep all source, state, outbox and configuration directories on
upgrade or removal. Removing the `@observer/dsh-observer` profile dependency
through native DSH removes its bundle layer; it does not delete events or state.

DSH terminal metadata has no measured execution start or duration. The existing
Langfuse exporter represents these facts as `langfuse.observation.type="event"`;
OTLP start/end both timestamp the terminal observation. Explicit metadata marks execution start and duration as unknown and identifies
the timestamp as terminal observation time, distinguishing it from execution
timing. The original native observation kind, stage and error code
remain metadata; local `started_at` stays null. Cancelled calls use OTLP UNSET
(status code 0); timeout/failed use ERROR (2), completed uses OK (1). Genuine
cancellation is verified; deadline-triggered timeout acceptance remains unverified.
See [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types),
[OTLP mapping](https://langfuse.com/integrations/native/opentelemetry), and
[OpenTelemetry status semantics](https://opentelemetry.io/docs/specs/otel/trace/api/#set-status).
