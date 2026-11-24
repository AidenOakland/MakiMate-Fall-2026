# COMMUNICATION BETWEEN LLM THROUGH TAILSCALE AND RASP. PI
# INCLUDES DOOR SENSOR CONTROL
# THEN RUNS AUTONOMOUSLY UNTIL TIMER REACHES 0 OR TIMER STOPS
# INCLUDES:
#      AI RESPONSE TIME
#      TIMER READING
#      TOTAL COOKING TIME
#      TARGET COOKING TIME
#      STATUS
#      RELAY

# THIS VERSION ALSO INCLUDES THE ANALOG CIRCUIT: DOOR SENSOR, RELAY
# INCLUDES 180-DEGREE ROTATION + HIGH-CONTRAST LCD PREPROCESSING

import base64
import io
import os
import re
import time
import requests
import serial
from PIL import Image, ImageEnhance, ImageOps

# INSERT SCRIPT TO PULL DIRECTIONS FROM CAMERA MODULE 3
from Camera_Module_3 import get_camera_directions



from Call_and_Respond_LLM import (
    SERIAL_PORT,
    BAUD_RATE,
    OUTPUT_DIR,
    BASE64_PATH,
    request_and_save_frame,
)

# --- Ollama / Tailscale Configuration ---
OLLAMA_URL = "http://100.124.123.50:11434/api/generate"
MODEL_NAME = "qwen3-vl"
PROMPT = (
    "Read only the primary remaining countdown timer shown on the digital display or phone screen. "
    "Ignore background movement, expiration clock times, and secondary preset labels. "
    "Reply ONLY with MM:SS (or 00:00 if it shows 0, or NONE if no timer digits are visible). /no_think"
)
OLLAMA_TIMEOUT = 60.0

# --- Target Cook Time & Cumulative Safety Thresholds ---
DEFAULT_TARGET_SECONDS = 47    # Placeholder until Camera Module 3 feeds box instructions
EXCESSIVE_WARN_PCT = 0.25      # +25% over target -> warn to open door immediately
RELAY_CUTOFF_PCT = 0.50        # +50% over target -> trigger relay: 1

# --- Wall-Clock Timeout & Display Safeguards ---
EMPTY_IDLE_TIMEOUT_SEC = 30.0  # Wait up to 30 real seconds for user to start typing after door close
MAX_BLANK_READS = 2            # Consecutive NONE/black reads after cooking -> power cut
DOOR_CLOSE_SETTLE_SEC = 1.5    # Confirm door stayed closed for 1.5s before starting cook cycle
DOOR_OPEN_CONFIRM_SEC = 3.5    # Require 3.5s of uninterrupted '0' (no '1' blips) to abort mid-cycle


def format_mmss(total_seconds: int | float) -> str:
    secs = max(0, int(round(total_seconds)))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def parse_mmss(raw_text: str) -> int | None:
    """Extracts MM:SS (or standalone '0') from LLM reply; returns None if NONE/blank."""
    cleaned = raw_text.strip().upper()
    if cleaned == "NONE" or not cleaned:
        return None
    if cleaned == "0":
        return 0
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", cleaned)
    if not match:
        return None
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    return minutes * 60 + seconds


# CREATE DEF FOR PULLING FOOD INFO
# TEMPLATE ONLY CURRENTLY
def get_food_info() -> dict:
    """Returns information about the food item. Hook Camera Module 3 value here later."""
    return {
        "name": "Unknown",
        "weight": 0,
        "cook_time": DEFAULT_TARGET_SECONDS,
    }

# PULL IN RPI CAMERA MODULE 3 TIME DATA HERE
def get_target_seconds() -> int:
    """Returns required cook time in seconds. Hook Camera Module 3 value here later."""
    return DEFAULT_TARGET_SECONDS


# ANALOG ADDITION
def send_relay_command(ser: serial.Serial, relay_state: int) -> None:
    """Sends '1' (active) or '0' (inactive) to Pico 2 W over Bluetooth."""
    cmd = b"1" if relay_state == 1 else b"0"
    ser.write(cmd)
    ser.flush()
    time.sleep(0.05)  # Give Pico loop time to process byte before any next 'R' trigger
    print(f"[HW RELAY] Sent '{cmd.decode()}' -> Relay {'ON (1)' if relay_state == 1 else 'OFF (0)'}")


# ANALOG ADDITION
def parse_door_line(raw_line: bytes) -> str | None:
    """Parses 'DOOR_STATE:1' or 'DOOR_STATE:0' from serial bytes; returns '1', '0', or None."""
    line = raw_line.decode("utf-8", errors="ignore").strip()
    if line.startswith("DOOR_STATE:"):
        val = line.split(":", 1)[1].strip()
        if val in ("0", "1"):
            return val
    return None


def settle_door_state(ser: serial.Serial, initial_val: str, settle_sec: float = DOOR_CLOSE_SETTLE_SEC) -> str:
    """
    Waits until mechanical switch bounce stops for `settle_sec` (1.5s) and returns
    the final settled door state ('0' or '1').
    """
    latest = initial_val
    deadline = time.perf_counter() + settle_sec
    while time.perf_counter() < deadline:
        if ser.in_waiting > 0:
            parsed = parse_door_line(ser.readline())
            if parsed is not None:
                latest = parsed
                # Extend settle window whenever a new bounce arrives
                deadline = time.perf_counter() + settle_sec
        else:
            time.sleep(0.01)
    return latest


def confirm_door_open(ser: serial.Serial, confirm_sec: float = DOOR_OPEN_CONFIRM_SEC) -> bool:
    """
    Returns True ONLY if no '1' (closed contact) arrives during `confirm_sec` (3.5s).
    Any '1' contact blip from a vibrating copper coil immediately cancels the open trigger.
    """
    deadline = time.perf_counter() + confirm_sec
    while time.perf_counter() < deadline:
        if ser.in_waiting > 0:
            parsed = parse_door_line(ser.readline())
            if parsed == "1":
                # Coil touched metal contact again -> door is still closed!
                return False
        else:
            time.sleep(0.02)
    return True


def evaluate_cooking_status(
    total_cooked_sec: int,
    target_sec: int,
    current_read_sec: int | None,
    step_lapsed: int | None,
    idle_remaining_sec: float | None = None,
) -> tuple[str, int]:
    """
    Evaluates status purely from total_cooked_sec (0 vs > 0):
    - total_cooked_sec == 0: user is still entering time / hasn't pressed Start yet.
    - total_cooked_sec > 0: active or completed cooking session.
    """
    warn_limit = target_sec * (1.0 + EXCESSIVE_WARN_PCT)
    cutoff_limit = target_sec * (1.0 + RELAY_CUTOFF_PCT)

    # 1. Cumulative cook-time safety thresholds (highest priority)
    if total_cooked_sec >= cutoff_limit:
        return "Excessive cook time. Triggering relay.", 1
    if total_cooked_sec >= warn_limit:
        return "Excessive cook time. Open door immediately.", 0
    if total_cooked_sec >= target_sec:
        return "Cook complete. Open door.", 0

    # 2. Pre-start phase (total_cooked_sec == 0): tolerate slow keypad entry
    if total_cooked_sec == 0:
        if current_read_sec is None or current_read_sec == 0:
            rem_str = f" ({idle_remaining_sec:.0f}s left before idle timeout)" if idle_remaining_sec is not None else ""
            return f"Waiting for time entry{rem_str}", 0
        return "Entering time / Waiting for start", 0

    # 3. Active cooking phase (0 < total_cooked_sec < target_sec)
    if current_read_sec is None:
        return "Display blank / Power cut", 0
    if current_read_sec == 0:
        return "Insufficient cook time", 0
    if step_lapsed is not None and step_lapsed > 0:
        return "cooking", 0

    return "Insufficient cook time", 0


def preprocess_image_for_llm(base64_image: str) -> str:
    """
    Rotates upside-down camera frame 180 degrees and enhances contrast/sharpness
    for low-contrast non-backlit LCD digits before sending to Ollama.
    """
    raw_bytes = base64.b64decode(base64_image)
    img = Image.open(io.BytesIO(raw_bytes))

    # 1. Fix upside-down camera mount
    img = img.rotate(180)

    # 2. Boost non-backlit LCD contrast (ignore top/bottom 2% glare outliers)
    gray = ImageOps.grayscale(img)
    contrasted = ImageOps.autocontrast(gray, cutoff=2)
    sharpened = ImageEnhance.Sharpness(contrasted).enhance(1.5)

    # 3. Save a debug copy so you can inspect what the LLM sees
    debug_path = os.path.join(OUTPUT_DIR, "latest_preprocessed.jpg")
    sharpened.save(debug_path, format="JPEG", quality=90)

    buf = io.BytesIO()
    sharpened.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def query_vision_llm(base64_image: str, prompt: str = PROMPT) -> tuple[str | None, float]:
    """Send a Base64 JPEG string to Ollama over Tailscale and return (raw_reply, elapsed_sec)."""
    processed_b64 = preprocess_image_for_llm(base64_image)

    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "images": [processed_b64],
        "stream": False,
        "think": False,
        "options": {
            "num_predict": 512,
            "temperature": 0.0,
        },
    }

    llm_start = time.perf_counter()

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
    except requests.RequestException as e:
        print(f"[LLM ERROR] Could not reach Ollama at {OLLAMA_URL}: {e}")
        return None, time.perf_counter() - llm_start

    llm_elapsed = time.perf_counter() - llm_start

    if response.status_code == 200:
        data = response.json()
        reply = data.get("response", "").strip()
        if not reply and data.get("thinking"):
            out_tokens = data.get("eval_count", 0)
            reply = f"[EMPTY RESPONSE - thinking used {out_tokens} tokens]"
        return reply, llm_elapsed
    else:
        print(f"[LLM ERROR] Status {response.status_code}: {response.text}")
        return None, llm_elapsed


def run_autonomous_cook_cycle(ser: serial.Serial) -> str:
    """
    Runs immediately upon settled door close ('1'). Returns latest door_state ('1' or '0').
    - While total_cooked_sec == 0: allows up to EMPTY_IDLE_TIMEOUT_SEC (30s) wall-clock
      time if display stays 00:00/NONE, and resets that 30s clock whenever digits are entered.
    - Once total_cooked_sec > 0: tracks cook time and auto-stops when timer hits 0,
      pauses/stops (step_lapsed == 0), or goes black (power cut).
    """
    prev_read_sec: int | None = None
    total_cooked_sec: int = 0
    consecutive_blank_reads: int = 0
    empty_idle_start_ts: float | None = time.perf_counter()
    last_sent_relay: int = 0

    send_relay_command(ser, 0)
    ser.reset_input_buffer()

    print("\n--- Door Closed (1): Monitoring Display (Ctrl+C to abort) ---\n")

    while True:
        # 0. Check if Pico pushed a door-open signal (ignore momentary copper-coil contact blips)
        saw_open_signal = False
        while ser.in_waiting > 0:
            incoming = parse_door_line(ser.readline())
            if incoming == "0":
                saw_open_signal = True
            elif incoming == "1":
                saw_open_signal = False  # Already re-closed before we even checked

        if saw_open_signal and confirm_door_open(ser):
            print("\n[DOOR] Door Opened (0) confirmed — stopping monitoring and returning to idle.\n")
            if last_sent_relay != 0:
                send_relay_command(ser, 0)
            return "0"

        # 1. Capture fresh frame over Bluetooth
        try:
            b64_image = request_and_save_frame(ser)
        except TimeoutError as e:
            print(f"[CAM WARN] {e} — retrying...")
            continue
        except serial.SerialException as e:
            print(f"[CAM ERROR] Serial connection lost: {e}")
            raise

        if not b64_image:
            print("[WARN] Capture failed — retrying...\n")
            continue

        # 2. Query LLM for timer digits
        reply, response_time = query_vision_llm(b64_image)
        if reply is None:
            continue

        current_read_sec = parse_mmss(reply)
        target_sec = get_target_seconds()
        step_lapsed: int | None = None
        idle_remaining_sec: float | None = None

        # 3. Track display state, wall-clock idle timer, and positive countdown drops
        if current_read_sec is None:
            consecutive_blank_reads += 1
            if total_cooked_sec == 0:
                if empty_idle_start_ts is None:
                    empty_idle_start_ts = time.perf_counter()
                idle_elapsed = time.perf_counter() - empty_idle_start_ts
                idle_remaining_sec = max(0.0, EMPTY_IDLE_TIMEOUT_SEC - idle_elapsed)
        else:
            consecutive_blank_reads = 0

            if total_cooked_sec == 0:
                if current_read_sec == 0:
                    if empty_idle_start_ts is None:
                        empty_idle_start_ts = time.perf_counter()
                    idle_elapsed = time.perf_counter() - empty_idle_start_ts
                    idle_remaining_sec = max(0.0, EMPTY_IDLE_TIMEOUT_SEC - idle_elapsed)
                else:
                    # Nonzero digits on display -> user is actively entering/adjusting time; reset 30s idle timer
                    empty_idle_start_ts = None

            if prev_read_sec is not None:
                step_lapsed = prev_read_sec - current_read_sec
                if step_lapsed > 0:
                    total_cooked_sec += step_lapsed
            prev_read_sec = current_read_sec

        status_msg, relay_state = evaluate_cooking_status(
            total_cooked_sec=total_cooked_sec,
            target_sec=target_sec,
            current_read_sec=current_read_sec,
            step_lapsed=step_lapsed,
            idle_remaining_sec=idle_remaining_sec,
        )

        # Send hardware relay command whenever relay_state changes
        if relay_state != last_sent_relay:
            send_relay_command(ser, relay_state)
            last_sent_relay = relay_state

        # 4. Print cycle telemetry
        print(f"[RESPONSE TIME]       {response_time:.2f}s")
        print(f"[TIMER READING]       {reply}")
        print(f"[TOTAL COOKING TIME]  {format_mmss(total_cooked_sec)} ({total_cooked_sec}s)")
        print(f"[TARGET COOKING TIME] {format_mmss(target_sec)} ({target_sec}s)")
        print(f"[STATUS]              {status_msg}")
        print(f"[RELAY]               relay: {relay_state}\n")

        # 5. Stop conditions split cleanly by total_cooked_sec == 0 vs total_cooked_sec > 0
        if total_cooked_sec == 0:
            if empty_idle_start_ts is not None:
                idle_elapsed = time.perf_counter() - empty_idle_start_ts
                if idle_elapsed >= EMPTY_IDLE_TIMEOUT_SEC:
                    print(
                        f"[AUTO-STOP] No time entered within {EMPTY_IDLE_TIMEOUT_SEC:.0f}s of door close. "
                        "Returning to idle.\n"
                    )
                    return "1"
        else:
            # Active cooking occurred (total_cooked_sec > 0)
            if consecutive_blank_reads >= MAX_BLANK_READS:
                print("[AUTO-STOP] Timer display went black (power cut). Ending cycle.\n")
                return "1"

            if current_read_sec == 0:
                print("[AUTO-STOP] Timer reached 0. Ending cycle.\n")
                return "1"

            if step_lapsed is not None and step_lapsed == 0:
                print("[AUTO-STOP] Timer stopped counting down (0s lapsed). Ending cycle.\n")
                return "1"


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Connecting to MicroMate on {SERIAL_PORT}...")

    door_state: str | None = None
    has_seen_open: bool = False

    with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2.0) as ser:
        ser.dtr = True
        ser.rts = True
        time.sleep(2.0)  # Let Bluetooth RFCOMM link settle
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print("Camera + Vision LLM + Live Door Sensor pipeline ready!")
        print("Waiting for Closed (1) -> Open (0) -> Closed (1) sequence (Ctrl+C to quit)...\n")

        while True:
            # USE DOOR SENSOR INPUT INSTEAD OF KEYBOARD INPUT
            if ser.in_waiting == 0:
                time.sleep(0.02)
                continue

            raw_val = parse_door_line(ser.readline())
            if raw_val is None:
                continue

            # Wait 1.5s for mechanical coil/contact bounce to settle before acting
            val = settle_door_state(ser, raw_val, settle_sec=DOOR_CLOSE_SETTLE_SEC)

            if val == "0":
                if door_state != "0":
                    door_state = "0"
                    has_seen_open = True
                    print("[DOOR] Door Open (0) — ready! Close door (1) to start cooking monitor...\n")

            elif val == "1":
                if door_state != "1":
                    door_state = "1"
                    if not has_seen_open:
                        print("[DOOR] Door Closed (1) detected on startup — open door (0) first to arm cycle.\n")
                        continue
                    has_seen_open = False
                    door_state = run_autonomous_cook_cycle(ser)
                    if door_state == "0":
                        has_seen_open = True


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped LLM bridge.")
