from __future__ import annotations

from .models import ScheduleConfiguration


def render_supercronic_schedule(configuration: ScheduleConfiguration) -> str:
    """Render validated local-wall-time schedules for Supercronic."""
    lines = [f"CRON_TZ={configuration.timezone}"]
    if configuration.daily.enabled:
        lines.append(f"{configuration.daily.cron} career-monitor daily")
    if configuration.weekly.enabled:
        lines.append(f"{configuration.weekly.cron} career-monitor weekly")
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""
