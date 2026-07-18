"""The canonical companion tool-name registry — a leaf module with no imports.

Lives in core (not companion.prompts) so core modules (toolspec, tool_scopes) can import it
top-level with no cycle and no companion dependency: the OSS export deletes companion/ wholesale
(edition seam, docs/plans/2026-07-14-oss-edition.md), so nothing core may import from it.
companion.prompts re-exports TOOL_NAMES from here — companion depends on core, never the reverse.
"""

from __future__ import annotations

TOOL_NAMES = ("spawn_session", "check_status", "memory_read", "memory_write", "memory_delete", "recall",
              "read_canvas", "get_list", "create_list", "add_item", "set_item_status",
              "get_session_result", "place_on_canvas", "put_object", "edit_item",
              "continue_session", "delete_item", "delete_object", "enrich", "recommend_model",
              "save_document",
              # Custom-tools contract (TTC v1): always graph nodes + parseable, but gated OUT of
              # scope unless TOTO_GW_CUSTOM_TOOLS is on — a flag-off model never sees the stanzas
              # and any emitted call is refused at dispatch (fail-closed).
              "create_tool", "delete_tool", "run_custom_tool", "instantiate_template",
              # External tools (Pipedream pilot): same dynamic-membership pattern — always a graph
              # node + parseable, but only IN scope (and stanza-visible) when the pilot is enabled.
              "calendar_events")
