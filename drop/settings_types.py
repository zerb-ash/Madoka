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

    def guild_channel_map(
        self,
        *,
        promo_channel_id: int | None = None,
        promo_test_channel_id: int | None = None,
    ) -> dict[int, set[int]]:
        # Discord user gateway needs OP 14 per guild or large servers stay silent.
        out: dict[int, set[int]] = {}

        def add(guild_id: int | None, channel_id: int | None) -> None:
            if not guild_id or not channel_id:
                return
            out.setdefault(int(guild_id), set()).add(int(channel_id))

        add(self.guild_id, self.channel_id)
        add(self.test_guild_id, self.test_channel_id)
        add(self.guild_id, promo_channel_id)
        add(self.test_guild_id, promo_test_channel_id)
        return out
