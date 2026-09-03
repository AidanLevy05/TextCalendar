#!/usr/bin/env bash
# Link signal-cli to your existing Signal account as a secondary device.
#
# This does NOT need a second phone number. Your phone stays the primary device;
# signal-cli becomes another linked device, the same as Signal Desktop.
#
# Usage:  ./scripts/link_signal.sh "legion-calendar-bot"
set -euo pipefail

DEVICE_NAME="${1:-signal-calendar-bot}"

if ! command -v signal-cli >/dev/null 2>&1; then
  echo "signal-cli not found on PATH." >&2
  echo "Install it first — see docs/SETUP.md." >&2
  exit 1
fi

echo "Generating a linking URI for device name: ${DEVICE_NAME}"
echo
echo "A 'sgnl://linkdevice?...' URI will be printed below."
echo "Turn it into a QR code and scan it with your phone:"
echo "    Signal -> Settings -> Linked devices -> Link new device"
echo
echo "To make a QR code from the URI:"
echo "    signal-cli link -n '${DEVICE_NAME}' | tee /dev/tty | qrencode -t ANSIUTF8"
echo
echo "Press Enter to start linking (Ctrl-C to abort)."
read -r

if command -v qrencode >/dev/null 2>&1; then
  signal-cli link -n "${DEVICE_NAME}" | while read -r line; do
    echo "${line}"
    case "${line}" in
      sgnl://*) echo "${line}" | qrencode -t ANSIUTF8 ;;
    esac
  done
else
  echo "(qrencode not installed — copy the URI into any QR generator)" >&2
  signal-cli link -n "${DEVICE_NAME}"
fi

echo
echo "Linked. Verify with:  signal-cli -a '+YOURNUMBER' receive"
