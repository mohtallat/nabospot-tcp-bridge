from dataclasses import dataclass
from typing import Optional


@dataclass
class OmniMessage:
    raw: str
    direction: str
    imei: Optional[str]
    command: Optional[str]
    parts: list[str]


def clean_raw(raw: bytes) -> str:
    raw = raw.replace(b"\xff\xff", b"")
    return raw.decode("ascii", errors="ignore").strip()


def parse_message(message: str) -> OmniMessage:
    parts = message.strip().replace("#", "").split(",")

    direction = "unknown"
    if parts and parts[0] == "*BGCR":
        direction = "lock_to_server"
    elif parts and parts[0] == "*BGCS":
        direction = "server_to_lock"

    imei = parts[2] if len(parts) > 2 else None
    command = parts[3] if len(parts) > 3 else None

    return OmniMessage(
        raw=message,
        direction=direction,
        imei=imei,
        command=command,
        parts=parts,
    )


def build_server_command(imei: str, command: str, *args) -> bytes:
    body = f"*BGCS,OM,{imei},{command}"
    if args:
        body += "," + ",".join(str(a) for a in args)
    body += "#\n"
    return b"\xff\xff" + body.encode("ascii")


def decode_s5(msg: OmniMessage) -> dict:
    # *BGCR,OM,IMEI,S5,voltage,battery,signal,lock_status,has_car,lever,iccid,apn,ble_mac,auto_lock
    p = msg.parts
    return {
        "voltage_mv": int(p[4]) if len(p) > 4 and p[4].isdigit() else None,
        "battery_percent": int(p[5]) if len(p) > 5 and p[5].isdigit() else None,
        "signal_strength": int(p[6]) if len(p) > 6 and p[6].isdigit() else None,
        "lock_state": "locked" if len(p) > 7 and p[7] == "1" else "unlocked" if len(p) > 7 and p[7] == "0" else "unknown",
        "occupancy_state": "occupied" if len(p) > 8 and p[8] == "1" else "empty" if len(p) > 8 and p[8] == "0" else "unknown",
        "lever_position": p[9] if len(p) > 9 else None,
        "sim_iccid": p[10] if len(p) > 10 else None,
        "apn": p[11] if len(p) > 11 else None,
        "ble_mac": p[12] if len(p) > 12 else None,
        "auto_lock_enabled": True if len(p) > 13 and p[13] == "1" else False,
    }


def decode_q0(msg: OmniMessage) -> dict:
    # *BGCR,OM,IMEI,Q0,voltage,ble_mac,reserved,reserved,lever
    p = msg.parts
    return {
        "voltage_mv": int(p[4]) if len(p) > 4 and p[4].isdigit() else None,
        "ble_mac": p[5] if len(p) > 5 else None,
        "lever_position": p[8] if len(p) > 8 else None,
    }


def decode_h0(msg: OmniMessage) -> dict:
    # *BGCR,OM,IMEI,H0,status,voltage,signal,has_car,lever,res,res,auto_lock
    p = msg.parts
    return {
        "lock_state": "locked" if len(p) > 4 and p[4] == "1" else "unlocked" if len(p) > 4 and p[4] == "0" else "unknown",
        "voltage_mv": int(p[5]) if len(p) > 5 and p[5].isdigit() else None,
        "signal_strength": int(p[6]) if len(p) > 6 and p[6].isdigit() else None,
        "occupancy_state": "occupied" if len(p) > 7 and p[7] == "1" else "empty" if len(p) > 7 and p[7] == "0" else "unknown",
        "lever_position": p[8] if len(p) > 8 else None,
        "auto_lock_enabled": True if len(p) > 11 and p[11] == "1" else False,
    }
