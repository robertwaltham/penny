"""The /config command — view and modify runtime configuration parameters."""

from __future__ import annotations

from penny.commands.base import Command
from penny.commands.models import CommandContext, CommandResult
from penny.config_params import get_params_by_group, get_runtime_param
from penny.responses import PennyResponse


class ConfigCommand(Command):
    """View and modify runtime configuration parameters."""

    name = "config"
    description = "View and modify runtime configuration parameters"
    help_text = (
        "View and modify runtime configuration parameters like timing settings. "
        "Configuration is stored in the database and takes effect immediately.\n\n"
        "**Usage**:\n"
        "• `/config` — List all available configuration parameters and their current values\n"
        "• `/config <key>` — Show the value of a specific configuration parameter\n"
        "• `/config <key> <value>` — Update a configuration parameter"
    )

    async def execute(self, args: str, context: CommandContext) -> CommandResult:
        """Execute config command."""
        parts = args.strip().split(maxsplit=1)
        runtime = context.config.runtime

        # Case 1: List all config
        if not args.strip():
            lines = [PennyResponse.CONFIG_HEADER, ""]

            for group_name, params in get_params_by_group(runtime):
                lines.append(PennyResponse.CONFIG_GROUP_HEADER.format(group=group_name))
                for param in params:
                    current_value = getattr(runtime, param.key)
                    lines.append(
                        PennyResponse.CONFIG_PARAM_DISPLAY.format(
                            key=param.key,
                            value=current_value,
                            description=param.description,
                        )
                    )
                lines.append("")

            lines.append(PennyResponse.CONFIG_FOOTER)
            return CommandResult(text="\n".join(lines))

        key = parts[0].upper()
        param = get_runtime_param(key, runtime)
        if param is None:
            return CommandResult(text=PennyResponse.CONFIG_UNKNOWN_PARAM.format(key=key))

        # Case 2: Get specific config
        if len(parts) == 1:
            current_value = getattr(runtime, key)
            return CommandResult(text=f"**{key}**: {current_value} ({param.description})")

        # Case 3: Set config value
        value_str = parts[1]

        # Validate value
        try:
            parsed_value = param.validator(value_str)
        except ValueError as e:
            return CommandResult(text=PennyResponse.CONFIG_INVALID_VALUE.format(key=key, error=e))

        # Store in database
        context.db.config.set(key, str(parsed_value), param.description)

        # Config changes take effect immediately via RuntimeParams DB lookup
        return CommandResult(text=PennyResponse.CONFIG_UPDATED.format(key=key, value=parsed_value))
