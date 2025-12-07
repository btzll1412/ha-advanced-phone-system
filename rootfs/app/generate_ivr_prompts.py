#!/usr/bin/env python3
"""
Generate IVR voice prompts using gTTS.
This script creates all necessary voice prompts for the IVR system.
"""

import os
import subprocess
from gtts import gTTS

# Directory for IVR prompts
IVR_DIR = "/var/lib/asterisk/sounds/ivr"

# All prompts needed for the IVR system
PROMPTS = {
    # Main menu
    "main-menu": "Welcome to the phone system. Press 1 to record a message. Press 2 to listen to your last recording. Press 3 to make an outbound call. Press 9 to repeat this menu. Press 0 to exit.",

    # Recording prompts
    "record-after-beep": "Please record your message after the beep. Press pound when finished.",
    "recording-saved": "Your recording has been saved.",
    "playing-recording": "Playing your recording.",
    "no-recording": "No recording found.",

    # Outbound call menu
    "outbound-menu": "Press 1 to broadcast to a group. Press 2 to call an individual number. Press 9 to go back.",

    # Group selection
    "select-group": "Please select a group.",
    "for": "for",
    "press-number": "Press the number of your selection.",
    "group-selected": "Group selected.",
    "no-groups": "No groups are configured. Please create groups in the web interface.",
    "invalid-group": "Invalid group selection. Please try again.",

    # Phone number entry
    "enter-phone-number": "Please enter the phone number using your keypad.",
    "you-entered": "You entered",
    "press-1-confirm-2-reenter": "Press 1 to confirm, or press 2 to re-enter the number.",
    "invalid-number": "Invalid phone number. Please enter at least 10 digits.",

    # Recording greeting for outbound
    "record-greeting-pound": "Please record your greeting after the beep. Press pound when finished.",

    # Recording review options (after recording)
    "recording-options": "Press 1 to listen to your recording. Press 2 to re-record. Press 3 to save. Press 9 to cancel.",

    # Outbound recording options (for broadcasts)
    "outbound-recording-options": "Press 1 to listen to your recording. Press 2 to re-record. Press 3 to send out now. Press 4 to schedule for later. Press 9 to cancel.",

    # Send now
    "message-sent": "Thank you. Your message is being sent now.",
    "error-sending": "There was an error sending your message. Please try again.",

    # Scheduling
    "enter-date": "Please enter the date using 2 digits for month, 2 digits for day, and 4 digits for year. For example, December 25th 2024 would be 1 2 2 5 2 0 2 4.",
    "invalid-date": "Invalid date entered. Please try again.",
    "enter-time": "Please enter the time in 24 hour format. For example, 2 30 PM would be 1 4 3 0.",
    "invalid-time": "Invalid time entered. Please try again.",
    "invalid-datetime": "The date and time you entered is not valid. Please try again.",
    "scheduled-for": "Your call is scheduled for",
    "confirm-schedule": "Press 1 to confirm the schedule. Press 2 to re-enter the date and time. Press 9 to go back.",
    "call-scheduled": "Your call has been scheduled successfully.",
    "error-scheduling": "There was an error scheduling your call. Please try again.",

    # General
    "goodbye": "Goodbye.",
    "invalid-option": "Invalid option. Please try again.",

    # Broadcast callback prompts
    "playing-broadcast-history": "Playing your broadcast message history, starting with the most recent.",
    "end-of-messages": "End of messages.",
    "no-messages": "There are no messages available for this number.",
    "no-recordings": "There are no recordings available.",
    "press-star-admin": "Press star for admin access.",

    # Admin PIN authentication prompts
    "enter-admin-pin": "Please enter your 4 digit admin PIN.",
    "access-granted": "Access granted.",
    "incorrect-pin": "Incorrect PIN. Please try again.",
    "too-many-attempts": "Too many incorrect attempts. Access denied.",
}


def generate_prompt(name, text):
    """Generate a single voice prompt."""
    mp3_path = os.path.join(IVR_DIR, f"{name}.mp3")
    wav_path = os.path.join(IVR_DIR, f"{name}.wav")
    ulaw_path = os.path.join(IVR_DIR, f"{name}.ulaw")

    # Skip if already exists (unless FORCE_REGENERATE env var is set)
    force_regen = os.environ.get('FORCE_REGENERATE_PROMPTS', '').lower() in ('1', 'true', 'yes')
    if not force_regen and os.path.exists(wav_path) and os.path.exists(ulaw_path):
        print(f"Prompt already exists: {name}")
        return True

    try:
        # Generate MP3 using gTTS
        tts = gTTS(text=text, lang='en', slow=False)
        tts.save(mp3_path)

        # Convert to WAV format (8kHz, mono, 16-bit) - this is slin format for Asterisk
        # Speed up by 25% using tempo for faster, clearer prompts
        try:
            subprocess.run([
                'sox', mp3_path, '-r', '8000', '-c', '1', '-b', '16', wav_path, 'tempo', '1.25'
            ], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to ffmpeg (atempo filter for speed)
            subprocess.run([
                'ffmpeg', '-y', '-i', mp3_path,
                '-ar', '8000', '-ac', '1', '-acodec', 'pcm_s16le',
                '-filter:a', 'atempo=1.25',
                wav_path
            ], check=True, capture_output=True)

        # Convert WAV to ulaw format for SIP trunk compatibility
        # ulaw (G.711 mu-law) is the standard codec for North American telephony
        try:
            subprocess.run([
                'sox', wav_path, '-t', 'ul', '-r', '8000', '-c', '1', ulaw_path
            ], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback: ffmpeg to ulaw
            subprocess.run([
                'ffmpeg', '-y', '-i', wav_path,
                '-ar', '8000', '-ac', '1', '-acodec', 'pcm_mulaw', '-f', 'mulaw',
                ulaw_path
            ], check=True, capture_output=True)

        # Clean up MP3 only (keep both wav/slin and ulaw for codec flexibility)
        if os.path.exists(mp3_path):
            os.remove(mp3_path)

        print(f"Generated prompt: {name}")
        return True

    except Exception as e:
        print(f"Error generating prompt {name}: {e}")
        # Clean up partial files
        for path in [mp3_path, wav_path, ulaw_path]:
            if os.path.exists(path):
                os.remove(path)
        return False


def main():
    """Generate all IVR prompts."""
    # Ensure directory exists
    os.makedirs(IVR_DIR, exist_ok=True)

    print(f"Generating IVR prompts in {IVR_DIR}")

    success_count = 0
    fail_count = 0

    for name, text in PROMPTS.items():
        if generate_prompt(name, text):
            success_count += 1
        else:
            fail_count += 1

    print(f"\nGenerated {success_count} prompts, {fail_count} failed")

    # Also ensure the custom recordings directory exists
    custom_dir = "/var/lib/asterisk/sounds/custom"
    os.makedirs(custom_dir, exist_ok=True)

    return fail_count == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
