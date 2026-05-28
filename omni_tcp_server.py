import socket
import threading
from datetime import datetime, timezone

connections = {}


def parse_message(raw: bytes) -> str:
    raw = raw.replace(b"\xff\xff", b"")
    return raw.decode("ascii", errors="ignore").strip()


def extract_imei(message: str):
    try:
        parts = message.split(",")
        if len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return None


def handle_client(conn, addr):
    imei = None

    print(f"[CLIENT CONNECTED] {addr}")

    try:
        while True:
            data = conn.recv(4096)

            if not data:
                break

            msg = parse_message(data)

            print("")
            print("===================================")
            print("[RX RAW]", data)
            print("[RX PARSED]", msg)
            print("===================================")

            parsed_imei = extract_imei(msg)

            if parsed_imei:
                imei = parsed_imei
                connections[imei] = {
                    "conn": conn,
                    "addr": addr,
                    "last_seen": datetime.now(timezone.utc), 
                    }

                print(f"[REGISTERED IMEI] {imei}")

    except Exception as e:
        print("[CLIENT ERROR]", e)

    finally:
        if imei and imei in connections:
            del connections[imei]

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
    print(f"OMNI TCP SERVER LISTENING ON {host}:{port}")
    print("===================================")
    print("")

    while True:
        conn, addr = server.accept()

        t = threading.Thread(
            target=handle_client,
            args=(conn, addr),
            daemon=True,
        )

        t.start()
def send_to_lock(imei: str, command: str):
    if imei not in connections:
        print(f"[ERROR] IMEI {imei} not connected")
        return

    conn = connections[imei]["conn"]

    # Omni requires 0xFFFF before server -> lock command
    payload = b"\xff\xff" + command.encode("ascii") + b"\n"

    print(f"[TX] {payload}")
    conn.sendall(payload)

if __name__ == "__main__":
    t = threading.Thread(target=start_server, daemon=True)
    t.start()

    print("Type commands like:")
    print("s5 <imei>")
    print("unlock_request <imei>")
    print("lock_request <imei>")
    print("")

    while True:
        line = input("> ").strip()
        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if len(parts) < 2:
            print("Missing IMEI")
            continue

        imei = parts[1]

        if cmd == "s5":
            send_to_lock(imei, f"*BGCS,OM,{imei},S5#")

        elif cmd == "unlock_request":
            import time
            user_id = 1
            ts = int(time.time())
            send_to_lock(imei, f"*BGCS,OM,{imei},R0,0,300,{user_id},{ts}#")

        elif cmd == "lock_request":
            import time
            user_id = 1
            ts = int(time.time())
            send_to_lock(imei, f"*BGCS,OM,{imei},R0,1,300,{user_id},{ts}#")
        
        elif cmd == "unlock":
           if len(parts) < 5:
               print("Usage: unlock <imei> <key> <user_id> <timestamp>")
               continue

           key = parts[2]
           user_id = parts[3]
           ts = parts[4]
           send_to_lock(imei, f"*BGCS,OM,{imei},L0,{key},{user_id},{ts}#")
        
        elif cmd == "ack":
           if len(parts) < 3:
               print("Usage: ack <imei> <cmd>")
               continue

           ack_cmd = parts[2]

           send_to_lock(
               imei,
               f"*BGCS,OM,{imei},Re,{ack_cmd}#"
           )
        else:
            print("Unknown command")
