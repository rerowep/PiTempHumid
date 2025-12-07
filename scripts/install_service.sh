#!/usr/bin/env bash
set -euo pipefail

# Install script for the pi_temp_humid systemd service
# Usage:
#   Install system-wide (requires sudo):
#     sudo ./scripts/install_service.sh [--workdir /path/to/installed/project]
#   Install for current user (starts on login):
#     ./scripts/install_service.sh --user [--workdir /path/to/installed/project]

WORKDIR="/opt/pi_temp_humid"
SERVICE_SRC_SYS="$(dirname "$0")/../packaging/systemd/pi_temp_humid.service"
SERVICE_SRC_USER="$(dirname "$0")/../packaging/systemd/pi_temp_humid.user.service"

INSTALL_USER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir)
      WORKDIR="$2"
      shift 2
      ;;
    --user)
      INSTALL_USER=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$INSTALL_USER" -eq 1 ]]; then
  # User-level install (no sudo)
  SERVICE_SRC="$SERVICE_SRC_USER"
  SERVICE_DST="$HOME/.config/systemd/user/pi_temp_humid.service"
  mkdir -p "$(dirname "$SERVICE_DST")"
  if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "User unit file not found: $SERVICE_SRC" >&2
    exit 1
  fi

  echo "Installing pi_temp_humid as a systemd user service"
  echo "Destination: $SERVICE_DST"
  echo "Working directory set to: $WORKDIR"

  cp "$SERVICE_SRC" "$SERVICE_DST"

  # If the user provided a custom workdir, replace WorkingDirectory line
  if [[ -n "$WORKDIR" ]]; then
    sed -i.bak "s|WorkingDirectory=%h/devel/tmp/pi_temp_hunid|WorkingDirectory=$WORKDIR|g" "$SERVICE_DST" || true
    sed -i.bak "s|WorkingDirectory=/opt/pi_temp_humid|WorkingDirectory=$WORKDIR|g" "$SERVICE_DST" || true
  fi

  # Ensure ExecStart points to the chosen workdir/start script
  sed -i.bak "s|%h/devel/tmp/pi_temp_hunid/packaging/scripts/start_pi_temp_humid.sh|$WORKDIR/packaging/scripts/start_pi_temp_humid.sh|g" "$SERVICE_DST" || true
  sed -i.bak "s|/opt/pi_temp_humid/packaging/scripts/start_pi_temp_humid.sh|$WORKDIR/packaging/scripts/start_pi_temp_humid.sh|g" "$SERVICE_DST" || true

  # Reload user daemon and enable service for the user
  systemctl --user daemon-reload
  systemctl --user enable --now pi_temp_humid.service

  echo "User service installed and started. Use 'systemctl --user status pi_temp_humid' to check."
else
  # System-wide install (requires sudo)
  SERVICE_SRC="$SERVICE_SRC_SYS"
  SERVICE_DST="/etc/systemd/system/pi_temp_humid.service"

  if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "Unit file not found: $SERVICE_SRC" >&2
    exit 1
  fi

  echo "Installing pi_temp_humid system service"
  echo "Destination: $SERVICE_DST"
  echo "Working directory set to: $WORKDIR"

  sudo cp "$SERVICE_SRC" "$SERVICE_DST"

  # Replace the placeholder WorkingDirectory in the unit file with the chosen path
  sudo sed -i.bak "s|WorkingDirectory=/opt/pi_temp_humid|WorkingDirectory=$WORKDIR|g" "$SERVICE_DST"

  # Ensure the DB path directory exists and is owned by the service user
  DB_DIR="/var/lib/pi_temp_humid"
  sudo mkdir -p "$DB_DIR"
  sudo chown -R pi:pi "$DB_DIR" || true

  sudo systemctl daemon-reload
  sudo systemctl enable --now pi_temp_humid.service

  echo "Service installed and started. Use 'sudo systemctl status pi_temp_humid' to check."
fi
