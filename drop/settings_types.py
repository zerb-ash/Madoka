from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DropMonitorSettings:
    user_token: str
    user_token_fallback: str
    guild_id: int | None
    channel_id: int | None
    test_guild_id: int | None
    test_channel_id: int | None
    role_id: int | None
    flag_wait_seconds: int
    snipe_delay_ms_min: int
    snipe_delay_ms_max: int

    @property
    def enabled(self) -> bool:
        return bool(self.user_token or self.user_token_fallback) and bool(
            self.channel_id or self.test_channel_id
        )

    def watched_channel_ids(self) -> set[int]:
        out: set[int] = set()
        if self.channel_id:
            out.add(self.channel_id)
        if self.test_channel_id:
            out.add(self.test_channel_id)
        return out
