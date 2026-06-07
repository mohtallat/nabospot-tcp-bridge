import socket
import threading
import os
import uvicorn
import httpx
from fastapi import FastAPI, Header, HTTPException
from omni_connection_manager import manager
from omni_protocol import (
    clean_raw,
    parse_message,
    decode_q0,
    decode_h0,
    decode_s5,
)
from zento_barrier import zento_provider

BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "change-me")
NABOSPOT_BACKEND_BASE_URL = os.getenv("NABOSPOT_BACKEND_BASE_URL", "").rstrip("/")
NABOSPOT_INTERNAL_API_KEY = os.getenv("NABOSPOT_INTERNAL_API_KEY", "")
http_app = FastAPI(title="NaboSpot TCP Bridge")

def post_telemetry_to_backend(imei: str, event_type: str, telemetry: dict | None, raw_payload: dict | None):
    if not NABOSPOT_BACKEND_BASE_URL or not NABOSPOT_INTERNAL_API_KEY:
        print("[BACKEND SYNC] skipped: backend URL or key missing")
        return

    url = f"{NABOSPOT_BACKEND_BASE_URL}/api/v1/parking-locks/internal/telemetry"

    payload = {
        "imei": imei,
        "event_type": event_type,
        "telemetry": telemetry or {},
        "raw_payload": raw_payload or {},
    }

    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                url,
                headers={"X-Internal-Key": NABOSPOT_INTERNAL_API_KEY},
                json=payload,
            )
            print(f"[BACKEND SYNC] {event_type} {imei} -> {r.status_code}")
            if r.status_code >= 400:
                print("[BACKEND SYNC ERROR]", r.text)
    except Exception as e:
        print("[BACKEND SYNC EXCEPTION]", str(e))

def handle_omni_message(raw: bytes, conn: socket.socket, addr):
    text = clean_raw(raw)
    if not text:
        return

    print("")
    print("===================================")
    print("[RX RAW]", raw)
    print("[RX PARSED]", text)
    print("===================================")

    msg = parse_message(text)

    if not msg.imei:
        print("[WARN] No IMEI found")
        return

    manager.register(msg.imei, conn, addr)
    manager.mark_seen(msg.imei)

    if msg.command == "Q0":
        telemetry = decode_q0(msg)
        manager.update_telemetry(msg.imei, telemetry)
        print(f"[Q0] online imei={msg.imei} telemetry={telemetry}")
        post_telemetry_to_backend(msg.imei, "Q0", telemetry, {"raw": text, "parts": msg.parts})

    elif msg.command == "H0":
        telemetry = decode_h0(msg)
        manager.update_telemetry(msg.imei, telemetry)
        print(f"[H0] heartbeat imei={msg.imei} telemetry={telemetry}")
        post_telemetry_to_backend(msg.imei, "H0", telemetry, {"raw": text, "parts": msg.parts})

    elif msg.command == "S5":
        telemetry = decode_s5(msg)
        manager.update_telemetry(msg.imei, telemetry)
        print(f"[S5] status imei={msg.imei} telemetry={telemetry}")
        post_telemetry_to_backend(msg.imei, "S5", telemetry, {"raw": text, "parts": msg.parts})

    elif msg.command == "R0":
        # *BGCR,OM,IMEI,R0,operation,key,user_id,timestamp
        if len(msg.parts) >= 8:
            operation_code = msg.parts[4]
            key = msg.parts[5]
            user_id = msg.parts[6]
            ts = msg.parts[7]
            print(f"[R0] imei={msg.imei} operation={operation_code} key={key} user_id={user_id} ts={ts}")
            manager.handle_operation_key(msg.imei, operation_code, key, user_id, ts)

    elif msg.command in ("L0", "L1"):
        # L0: *BGCR,OM,IMEI,L0,status,user_id,timestamp
        result_code = msg.parts[4] if len(msg.parts) > 4 else None
        print(f"[{msg.command}] result imei={msg.imei} status={result_code}")
        post_telemetry_to_backend(
         msg.imei,
         msg.command,
        {
            "operation_status": result_code,
            "lock_state": "unlocked" if msg.command == "L0" and result_code == "0"
         else "locked" if msg.command == "L1" and result_code == "0"
         else "unknown",
        },
        {"raw": text, "parts": msg.parts},
       )
        if result_code == "0":
            if msg.command == "L0":
                manager.update_telemetry(msg.imei, {"lock_state": "unlocked"})
            else:
                manager.update_telemetry(msg.imei, {"lock_state": "locked"})

        manager.complete_operation(msg.imei)

        # Acknowledge result to lock
        manager.send_command(msg.imei, "Re", msg.command)

    elif msg.command == "W0":
        print(f"[ALARM] imei={msg.imei} parts={msg.parts}")
        manager.send_command(msg.imei, "Re", "W0")
        post_telemetry_to_backend(
         msg.imei,
         "W0",
         {"alarm_code": msg.parts[4] if len(msg.parts) > 4 else None},
         {"raw": text, "parts": msg.parts},
        )
    else:
        print(f"[INFO] Unhandled command={msg.command} imei={msg.imei} parts={msg.parts}")


def handle_client(conn: socket.socket, addr):
    imei = None
    print(f"[CLIENT CONNECTED] {addr}")

    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break

            # Some devices may send multiple lines; split safely.
            chunks = data.split(b"\n")
            for chunk in chunks:
                if not chunk.strip():
                    continue

                handle_omni_message(chunk + b"\n", conn, addr)

                try:
                    parsed = parse_message(clean_raw(chunk))
                    if parsed.imei:
                        imei = parsed.imei
                except Exception:
                    pass

    except Exception as e:
        print("[CLIENT ERROR]", e)

    finally:
        if imei:
            manager.unregister(imei)
        try:
            conn.close()
        except Exception:
            pass
        print(f"[CLIENT DISCONNECTED] {addr}")


def start_server(host="0.0.0.0", port=9666):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(100)

    print("")
    print("===================================")
    print(f"NABOSPOT OMNI TCP BRIDGE LISTENING ON {host}:{port}")
    print("===================================")
    print("")

    while True:
        conn, addr = server.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


def command_console():
    print("Commands:")
    print("  list")
    print("  s5 <imei>")
    print("  unlock <imei>")
    print("  lock <imei>")
    print("")

    while True:
        line = input("> ").strip()
        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if cmd == "list":
            conns = manager.list_connections()
            if not conns:
                print("No connected locks")
                continue

            for c in conns:
                print(
                    f"IMEI={c.imei} addr={c.addr} last_seen={c.last_seen_at} telemetry={c.telemetry}"
                )

        elif cmd == "s5":
            if len(parts) < 2:
                print("Usage: s5 <imei>")
                continue
            manager.send_command(parts[1], "S5")

        elif cmd == "unlock":
            if len(parts) < 2:
                print("Usage: unlock <imei>")
                continue
            manager.request_unlock(parts[1], user_id=1)

        elif cmd == "lock":
            if len(parts) < 2:
                print("Usage: lock <imei>")
                continue
            manager.request_lock(parts[1], user_id=1)

        else:
            print("Unknown command")

def require_bridge_key(x_bridge_key: str | None):
    if not x_bridge_key or x_bridge_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


@http_app.get("/health")
def health():
    return {"status": "ok"}


@http_app.get("/locks/{imei}/status")
def get_lock_status(imei: str, x_bridge_key: str | None = Header(default=None)):
    require_bridge_key(x_bridge_key)

    c = manager.get(imei)
    if not c:
        raise HTTPException(status_code=404, detail="Lock is not connected")

    return {
        "imei": c.imei,
        "connected": True,
        "last_seen_at": c.last_seen_at.isoformat(),
        "telemetry": c.telemetry,
        "pending": c.pending is not None,
    }


@http_app.post("/locks/{imei}/refresh")
def refresh_lock_status(imei: str, x_bridge_key: str | None = Header(default=None)):
    require_bridge_key(x_bridge_key)

    ok = manager.send_command(imei, "S5")
    if not ok:
        raise HTTPException(status_code=404, detail="Lock is not connected")

    return {"imei": imei, "command": "S5", "status": "sent"}


@http_app.post("/locks/{imei}/unlock")
def unlock_lock(imei: str, x_bridge_key: str | None = Header(default=None)):
    require_bridge_key(x_bridge_key)

    ok = manager.request_unlock(imei, user_id=1)
    if not ok:
        raise HTTPException(status_code=404, detail="Lock is not connected")

    return {"imei": imei, "command": "unlock", "status": "sent"}


@http_app.post("/locks/{imei}/lock")
def lock_lock(imei: str, x_bridge_key: str | None = Header(default=None)):
    require_bridge_key(x_bridge_key)

    ok = manager.request_lock(imei, user_id=1)
    if not ok:
        raise HTTPException(status_code=404, detail="Lock is not connected")

    return {"imei": imei, "command": "lock", "status": "sent"}


def _run_zento_command(gateway_serial: str, rs485_address: int, command: str):
    try:
        return zento_provider.execute(gateway_serial, rs485_address, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        print(f"[ZENTO ERROR] command={command} gateway={gateway_serial} rs485={rs485_address}: {exc}")
        raise HTTPException(status_code=500, detail="Boom barrier command failed")


@http_app.get("/barriers/{gateway_serial}/{rs485_address}/status")
def get_barrier_status(
    gateway_serial: str,
    rs485_address: int,
    x_bridge_key: str | None = Header(default=None),
):
    require_bridge_key(x_bridge_key)
    state = zento_provider.get_state(gateway_serial, rs485_address)
    return {
        "success": True,
        "provider": "zento",
        "gateway_serial": state.gateway_serial,
        "rs485_address": state.rs485_address,
        "connected": True,
        "mock": zento_provider.mock_mode,
        "barrier_state": state.barrier_state,
        "fault_state": state.fault_state,
        "voltage": state.voltage,
        "current": state.current,
        "last_seen_at": state.last_seen_at.isoformat() if state.last_seen_at else None,
        "last_command": state.last_command,
        "last_command_at": state.last_command_at.isoformat() if state.last_command_at else None,
    }


@http_app.post("/barriers/{gateway_serial}/{rs485_address}/open")
def open_barrier(
    gateway_serial: str,
    rs485_address: int,
    x_bridge_key: str | None = Header(default=None),
):
    require_bridge_key(x_bridge_key)
    return _run_zento_command(gateway_serial, rs485_address, "open")


@http_app.post("/barriers/{gateway_serial}/{rs485_address}/close")
def close_barrier(
    gateway_serial: str,
    rs485_address: int,
    x_bridge_key: str | None = Header(default=None),
):
    require_bridge_key(x_bridge_key)
    return _run_zento_command(gateway_serial, rs485_address, "close")


@http_app.post("/barriers/{gateway_serial}/{rs485_address}/stop")
def stop_barrier(
    gateway_serial: str,
    rs485_address: int,
    x_bridge_key: str | None = Header(default=None),
):
    require_bridge_key(x_bridge_key)
    return _run_zento_command(gateway_serial, rs485_address, "stop")


@http_app.post("/barriers/{gateway_serial}/{rs485_address}/status")
def query_barrier_status(
    gateway_serial: str,
    rs485_address: int,
    x_bridge_key: str | None = Header(default=None),
):
    require_bridge_key(x_bridge_key)
    return _run_zento_command(gateway_serial, rs485_address, "status")

if __name__ == "__main__":
    tcp_thread = threading.Thread(target=start_server, daemon=True)
    tcp_thread.start()

    uvicorn.run(http_app, host="0.0.0.0", port=8080)
