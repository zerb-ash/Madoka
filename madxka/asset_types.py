from __future__ import annotations

ASSET_TYPE_NAMES: dict[int, str] = {
    2: "T-Shirt",
    8: "Hat",
    11: "Shirt",
    12: "Pants",
    17: "Head",
    18: "Face",
    19: "Gear",
    27: "Torso",
    32: "Package",
    38: "Plugin",
    41: "Hair Accessory",
    42: "Face Accessory",
    43: "Neck Accessory",
    44: "Shoulder Accessory",
    45: "Front Accessory",
    46: "Back Accessory",
    47: "Waist Accessory",
    48: "Climb Animation",
    49: "Death Animation",
    50: "Fall Animation",
    51: "Idle Animation",
    52: "Jump Animation",
    53: "Run Animation",
    54: "Swim Animation",
    55: "Walk Animation",
    56: "Pose Animation",
    57: "Emote Animation",
    61: "Emote",
    62: "Video",
    64: "Tween Animation",
    67: "Mesh",
    68: "MeshPart",
    69: "Model",
    71: "GamePass",
    72: "Badge",
}


def asset_type_name(asset_type_id: int) -> str:
    return ASSET_TYPE_NAMES.get(int(asset_type_id), f"Type {int(asset_type_id)}")
