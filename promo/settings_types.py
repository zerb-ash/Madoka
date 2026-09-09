from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromoMonitorSettings:
    channel_id: int | None
    test_channel_id: int | None
    role_id: int | None

    @property
    def enabled(self) -> bool:
        return bool(self.channel_id or self.test_channel_id)

    def watched_channel_ids(self) -> set[int]:
        out: set[int] = set()
        if self.channel_id:
            out.add(self.channel_id)
        if self.test_channel_id:
            out.add(self.test_channel_id)
        return out

    def is_test(self, channel_id: int) -> bool:
        return self.test_channel_id is not None and int(channel_id) == int(self.test_channel_id)
