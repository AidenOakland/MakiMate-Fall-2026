# Final script
# Waits for trigger from LLM_Bridge.py (door open);
# Triggers single image capture and delivers via BT to Pi.
# Currently using Raspberry Pi Pico 2 W with Arducam Mega 5MP
# Arduino IDE script for Pico saved in MicroMate-Pico_ImageCapture_AnalogCkt.c

import base64
import os
import time
import serial

# --- Configuration ---
SERIAL_PORT = "/dev/rfcomm0"
BAUD_RATE = 115200

OUTPUT_DIR = "/home/makimate/MakiMate-Fall-2026/MicroMate/LLM_Capture"
IMAGE_PATH = os.path.join(OUTPUT_DIR, "timer_capture.jpg")
BASE64_PATH = os.path.join(OUTPUT_DIR, "llm_base64.txt")


def atomic_write_bytes(target_path: str, data: bytes) -> None:
    """Write to a temp file first, then automically replace target so readers never see partial files."""
    tmp_path = f"{target_path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target_path)


def atomic_write_text(target_path: str, text: str) -> None:
    """Atomically overwrite a UTF-8 text file."""
    tmp_path = f"{target_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target_path)


def read_exact(ser: serial.Serial, num_bytes: int) -> bytes:
    """Read an exact number of bytes from serial, raising TimeoutError if incomplete."""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = ser.read(num_bytes - len(buf))
        if not chunk:
            raise TimeoutError(f"Timed out reading payload ({len(buf)}/{num_bytes} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def request_and_save_frame(ser: serial.Serial) -> str | None:
    """Send 'R' trigger, parse 'IMG_START:<id>,<length>\\n', save JPEG + Base64, and return Base64 string."""
    req_start = time.perf_counter()

    # 1. Clear stale input buffer and send 'R' trigger
    ser.reset_input_buffer()
    ser.write(b"R")
    ser.flush()

    # 2. Read ASCII header line: IMG_START:<img_id>,<length>
    deadline = time.time() + 5.0
    img_id = None
    length = None

    while time.time() < deadline:
        raw_line = ser.readline()
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if line.startswith("ERR_"):
            print(f"[CAM ERROR] Pico reported camera error: {line}")
            return None
        if line.startswith("IMG_START:"):
            try:
                parts = line.split("IMG_START:")[1].split(",")
                img_id = int(parts[0])
                length = int(parts[1])
                break
            except (IndexError, ValueError):
                print(f"[WARN] Malformed header received: {line!r}")
                return None
    else:
        print("[WARN] Timed out waiting for 'IMG_START:' header.")
        return None

    if length <= 0 or length > 500_000:
        print(f"[WARN] Packet #{img_id} | Invalid frame length: {length} bytes. Skipping.")
        return None

    # 3. Read exact payload and isolate clean JPEG between SOI (0xFFD8) and EOI (0xFFD9)
    raw_payload = read_exact(ser, length)

    soi = raw_payload.find(b"\xff\xd8")
    eoi = raw_payload.rfind(b"\xff\xd9")

    if soi == -1 or eoi == -1 or eoi <= soi:
        print(
            f"[WARN] Packet #{img_id} | Missing JPEG markers "
            f"(first 4B: {raw_payload[:4].hex()}, last 4B: {raw_payload[-4:].hex()}). Skipping."
        )
        return None

    jpeg_bytes = raw_payload[soi : eoi + 2]

    # 4. Encode to clean single-line Base64 string and atomically overwrite both files
    b64_string = base64.b64encode(jpeg_bytes).decode("ascii")
    atomic_write_bytes(IMAGE_PATH, jpeg_bytes)
    atomic_write_text(BASE64_PATH, b64_string)

    latency_ms = (time.perf_counter() - req_start) * 1000.0
    """print(
        f"VALID Packet #{img_id} | "
        f"Base64 Size: {len(b64_string)} chars | "
        f"JPG Size: {len(jpeg_bytes)} bytes | "
        f"Capture Latency: {latency_ms:.1f} ms"
    )"""

    print(f"VALID Packet #{img_id} | Successful Image Capture | Capture Time: {latency_ms:.1f} ms")

    return b64_string



def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Connecting to {SERIAL_PORT}...")

    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2.0) as ser:
        ser.dtr = True
        ser.rts = True
        time.sleep(2.0)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print(f"Ready! Saving into {OUTPUT_DIR}")
        print("Type 'r' + Enter to capture a frame (or 'q' + Enter to quit).\n")

        while True:
            cmd = input("[Trigger] Press 'r' + Enter to snap (or 'q' to quit): ").strip().lower()
            if cmd == "q":
                print("Exiting capture.")
                break
            if cmd != "r":
                print("Ignored — type 'r' and press Enter to trigger a capture.")
                continue

            try:
                request_and_save_frame(ser)
            except TimeoutError as e:
                print(f"[WARN] {e}")
            except serial.SerialException as e:
                print(f"[ERROR] Serial connection lost: {e}")
                break


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped capture.")
        



