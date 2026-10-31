# penny-client
iOS client for the penny personal assistant

Agent status uses the existing `agent_progress` websocket message. After a model
step returns thinking text, `event: "thinking_updated"` carries the complete trace
in an optional `thinking` string, keyed by `run_id` and `step` (also carrying
`agent`, `scope`, `max_steps`, and an empty `tools` array). This covers both tool
steps and final answers; no event is sent when thinking is absent. Traces arrive
after the step's model call finishes, rather than token by token. Foreground
updates go to the requesting device; background updates go to connected devices.
The chat activity card lists steps, tool calls, and thinking previews that expand
into selectable full traces. The activity sheet uses the same rows. Foreground
activity remains visible through completion and disconnect/reconnect, and clears
only after the next message is successfully sent. Failed sends, empty drafts,
and unrelated requests preserve it. A new run arriving during a send is retained.
Completed activity stops animating; connection loss marks a running entry as
interrupted. Background work remains an active-run list in the activity sheet.
This history is held in memory for the current app session; durable traces remain
in the server's prompt logs. Older clients skip the new event, and the new client
accepts existing events without `thinking`.

Build and test from the repo root with `make client-check` (requires Xcode; runs
the standalone service tests, app-hosted `PennyClientTests`, and the Testflight
build verification on a freshly booted simulator). For service-only changes,
`make client-services-check` runs `PennyServicesStandaloneTests` without
building or launching an app target. CI runs the complete gate on pull requests
touching `penny-client/`.

Settings → Image Attachments controls automatic images for the entire iOS channel:
cited-page images, same-site images, and broader related images. All switches start
enabled. Changes save immediately to the server with confirmation and error state;
the connection settings' Save button is separate. Explicit attachments and images
generated for the current request always remain deliverable. The controls affect
new replies; existing messages and the History attachment option are unchanged.
An older server that does not expose all four switches shows disabled controls.


The top-left memories button opens a read-only data browser (iPad popover, iPhone
sheet). Memories are grouped into Collections, Logs, and Archived; each has Entries,
Activity, and read-only Config panels. Prompts shows all agent runs with filters and
expandable run → prompt → turn details. The previous Insights and Memory Management
screens have been removed. Global settings remain under the gear button.

The browser reads the current memory contract, including `schedule` (RRULE) and
notification status (`published` on the wire). Deploy the accompanying iOS server
support before this client: memory list/detail/page and prompt-log reads use optional
`request_id` values echoed on responses and `data_read_error` failures. Existing
clients without IDs remain supported. Browser requests time out after 30 seconds;
filter changes, dismissal, and reconnect discard obsolete results. No memory data is
persisted on the client, and this browser provides no mutation controls.
