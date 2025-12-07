#!/usr/bin/env bash
# Start script for PiTempHumid GUI mirroring the systemd unit environment.
# Usage: ./start_pi_temp_humid.sh [--db PATH] [--no-prune|--prune-months N] [--eglfs]
#        [--rotation DEG] [--touch /dev/input/eventX] [--mouse /dev/input/eventY]
#        [--user USER] [--bg]

set -euo pipefail

PROG_NAME=$(basename "$0")

print_usage() {
    cat <<EOF
Usage: $PROG_NAME [options] -- [extra python args]

Options:
    --db PATH                 Path to SQLite DB (default: /var/lib/pi_temp_humid/readings.db)
    --no-prune                Disable daily pruning (PI_TEMP_PRUNE_ENABLED=0)
    --prune-months N          Prune readings older than N months (default: 3)
    --eglfs                   Force EGLFS platform (PIQT_FORCE_EGLFS=1)
    --rotation DEG            EGLFS rotation in degrees (0/90/180/270) (default: 180)
    --touch DEVICE            Touch device path (default: /dev/input/event0)
    --mouse DEVICE            Mouse/pointer device path (default: /dev/input/event1)
    --user USER               Run the GUI as this user (uses `sudo -u`) (default: pi)
    --bg                      Run in background (nohup, logs to /var/log/pi_temp_humid.log)
    --force-evdev             Prefer Qt evdev input plugin when available
    --help                    Show this help

Any arguments after a bare `--` are appended to the python command.
EOF
}

# Defaults mirroring the systemd unit
DB_DEFAULT="/var/lib/pi_temp_humid/readings.db"
PRUNE_ENABLED_DEFAULT=1
PRUNE_MONTHS_DEFAULT=3
PIQT_FORCE_EGLFS_DEFAULT=1
QT_QPA_PLATFORM_DEFAULT="eglfs"
TOUCH_DEFAULT="/dev/input/event5"
MOUSE_DEFAULT="/dev/input/event1"
WIDTH_DEFAULT=800
HEIGHT_DEFAULT=480
ROTATION_DEFAULT=180
RUN_USER_DEFAULT="pi"
LOGFILE="/var/log/pi_temp_humid.log"

# Current values (can be overridden by env or flags)
DB="${PI_TEMP_DB:-$DB_DEFAULT}"
PRUNE_ENABLED="${PI_TEMP_PRUNE_ENABLED:-$PRUNE_ENABLED_DEFAULT}"
PRUNE_MONTHS="${PI_TEMP_PRUNE_MONTHS:-$PRUNE_MONTHS_DEFAULT}"
PIQT_FORCE_EGLFS="${PIQT_FORCE_EGLFS:-$PIQT_FORCE_EGLFS_DEFAULT}"
QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-$QT_QPA_PLATFORM_DEFAULT}"
TOUCH_DEVICE="${QT_QPA_EVDEV_TOUCHSCREEN:-$TOUCH_DEFAULT}"
MOUSE_DEVICE="${QT_QPA_EVDEV_MOUSE:-$MOUSE_DEFAULT}"
WIDTH="${QT_QPA_EGLFS_PHYSICAL_WIDTH:-$WIDTH_DEFAULT}"
HEIGHT="${QT_QPA_EGLFS_PHYSICAL_HEIGHT:-$HEIGHT_DEFAULT}"
ROTATION="${QT_QPA_EGLFS_ROTATION:-$ROTATION_DEFAULT}"
TOUCH_PARAMS="${QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS:-$TOUCH_DEVICE:rotate=$ROTATION}"
MOUSE_PARAMS="${QT_QPA_EVDEV_MOUSE_PARAMETERS:-$MOUSE_DEVICE:rotate=$ROTATION}"

RUN_USER="${RUN_USER_DEFAULT}"
BACKGROUND=0
QPA_LOGGING=0
FORCE_EVDEV=0
LOGFILE_OVERRIDE=""
EXTRA_PY_ARGS=()

# Simple arg parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)
            DB="$2"; shift 2;;
        --no-prune)
            PRUNE_ENABLED=0; shift;;
        --prune-months)
            PRUNE_MONTHS="$2"; shift 2;;
        --eglfs)
            PIQT_FORCE_EGLFS=1; QT_QPA_PLATFORM="eglfs"; shift;;
        --rotation)
            ROTATION="$2"; shift 2;;
        --touch)
            TOUCH_DEVICE="$2"; shift 2;;
        --mouse)
            MOUSE_DEVICE="$2"; shift 2;;
        --user)
            RUN_USER="$2"; shift 2;;
        --bg)
            BACKGROUND=1; shift;;
        --qpa-log)
            QPA_LOGGING=1; shift;;
        --force-evdev)
            FORCE_EVDEV=1; shift;;
        --logfile)
            LOGFILE_OVERRIDE="$2"; shift 2;;
        --help)
            print_usage; exit 0;;
        --)
            shift; while [[ $# -gt 0 ]]; do EXTRA_PY_ARGS+=("$1"); shift; done; break;;
        *)
            # Unknown; forward to python module
            EXTRA_PY_ARGS+=("$1"); shift;;
    esac
done

# If the user provided a logfile override, apply it now (parsing above).
if [[ -n "$LOGFILE_OVERRIDE" ]]; then
    LOGFILE="$LOGFILE_OVERRIDE"
fi

# Build parameters that some EGLFS/evdev builds accept
# Use simple rotate-only parameters for evdev touchscreen/mouse so
# Qt's evdev plugin receives only rotation directives (e.g. "rotate=180").
TOUCH_PARAMS="${QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS:-rotate=${ROTATION}}"
MOUSE_PARAMS="${QT_QPA_EVDEV_MOUSE_PARAMETERS:-rotate=${ROTATION}}"

# Export environment variables
export QT_QPA_EVDEV_TOUCHSCREEN="$TOUCH_DEVICE"
export QT_QPA_EGLFS_PHYSICAL_WIDTH="$WIDTH"
export QT_QPA_EGLFS_PHYSICAL_HEIGHT="$HEIGHT"
export QT_QPA_EGLFS_ROTATION="$ROTATION"
export QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS="$TOUCH_PARAMS"
export QT_QPA_EVDEV_TOUCHSCREEN_ROTATION="$ROTATION"
export QT_QPA_EVDEV_MOUSE="$MOUSE_DEVICE"
export QT_QPA_EVDEV_MOUSE_PARAMETERS="$MOUSE_PARAMS"
export QT_QPA_EVDEV_MOUSE_ROTATION="$ROTATION"
export PI_TEMP_DB="$DB"
export PI_TEMP_PRUNE_ENABLED="$PRUNE_ENABLED"
export PI_TEMP_PRUNE_MONTHS="$PRUNE_MONTHS"
export PIQT_FORCE_EGLFS="$PIQT_FORCE_EGLFS"
if [[ "$QPA_LOGGING" -eq 1 ]]; then
    export QT_LOGGING_RULES="qt.qpa.*=true"
fi
if [[ "$FORCE_EVDEV" -eq 1 ]]; then
    # Ask Qt to prefer the evdev input plugin if the build contains it.
    export QT_QPA_GENERIC_PLUGINS="evdev"
    echo "Forcing preference for Qt evdev input plugin (if available)"
fi

# When using EGLFS prefer disabling libinput so evdev rotation params
# are applied consistently on Raspberry Pi systems.
if [[ "${QT_QPA_PLATFORM:-$QT_QPA_PLATFORM_DEFAULT}" == "eglfs" ]]; then
    export QT_QPA_EGLFS_NO_LIBINPUT=1
    echo "Exporting QT_QPA_EGLFS_NO_LIBINPUT=1 to prefer evdev rotation handling"
fi

# Hide the mouse cursor for EGLFS fullscreen mode on embedded displays
if [[ "${QT_QPA_PLATFORM:-$QT_QPA_PLATFORM_DEFAULT}" == "eglfs" ]]; then
    export QT_QPA_EGLFS_HIDECURSOR=1
    echo "Exporting QT_QPA_EGLFS_HIDECURSOR=1 to hide cursor in EGLFS"
fi

# Print a short summary
cat <<EOF
Starting PiTempHumid with:
    QT_QPA_PLATFORM=$QT_QPA_PLATFORM
    EGLFS rotation=$ROTATION (physical ${WIDTH}x${HEIGHT})
    touchscreen=$TOUCH_DEVICE (params: $TOUCH_PARAMS)
    mouse=$MOUSE_DEVICE (params: $MOUSE_PARAMS)
    force_evdev=$FORCE_EVDEV
    DB=$DB
    prune_enabled=$PRUNE_ENABLED prune_months=$PRUNE_MONTHS
    run_user=$RUN_USER background=$BACKGROUND
EOF

PY_CMD=(python3 -m pi_temp_humid.gui)
if [[ ${#EXTRA_PY_ARGS[@]} -gt 0 ]]; then
    PY_CMD+=("${EXTRA_PY_ARGS[@]}")
fi

# Compose env assignment string for sudo when running as different user
ENV_VARS=(
    "QT_QPA_PLATFORM=$QT_QPA_PLATFORM"
    "QT_QPA_EVDEV_TOUCHSCREEN=$TOUCH_DEVICE"
    "QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS=$TOUCH_PARAMS"
    "QT_QPA_EVDEV_TOUCHSCREEN_ROTATION=$ROTATION"
    "QT_QPA_EGLFS_PHYSICAL_WIDTH=$WIDTH"
    "QT_QPA_EGLFS_PHYSICAL_HEIGHT=$HEIGHT"
    "QT_QPA_EGLFS_ROTATION=$ROTATION"
    "QT_QPA_EVDEV_MOUSE=$MOUSE_DEVICE"
    "QT_QPA_EVDEV_MOUSE_PARAMETERS=$MOUSE_PARAMS"
    "QT_QPA_EVDEV_MOUSE_ROTATION=$ROTATION"
    "PI_TEMP_DB=$DB"
    "PI_TEMP_PRUNE_ENABLED=$PRUNE_ENABLED"
    "PI_TEMP_PRUNE_MONTHS=$PRUNE_MONTHS"
    "PIQT_FORCE_EGLFS=$PIQT_FORCE_EGLFS"
)

if [[ "$FORCE_EVDEV" -eq 1 ]]; then
        ENV_VARS+=("QT_QPA_GENERIC_PLUGINS=$QT_QPA_GENERIC_PLUGINS")
fi

if [[ "$QPA_LOGGING" -eq 1 ]]; then
        ENV_VARS+=("QT_LOGGING_RULES=$QT_LOGGING_RULES")
fi

# Include EGLFS/libinput override in sudo envs when present
if [[ -n "${QT_QPA_EGLFS_NO_LIBINPUT:-}" ]]; then
    ENV_VARS+=("QT_QPA_EGLFS_NO_LIBINPUT=$QT_QPA_EGLFS_NO_LIBINPUT")
fi
if [[ -n "${QT_QPA_EGLFS_HIDECURSOR:-}" ]]; then
    ENV_VARS+=("QT_QPA_EGLFS_HIDECURSOR=$QT_QPA_EGLFS_HIDECURSOR")
fi

# Run command helper
run_cmd() {
    if [[ "$RUN_USER" != "$(whoami)" ]]; then
        # Use sudo to run as the requested user and pass env vars inline
        SUDO_ENV=""
        for e in "${ENV_VARS[@]}"; do SUDO_ENV+="$e "; done
        if [[ $BACKGROUND -eq 1 ]]; then
            nohup sudo -u "$RUN_USER" env $SUDO_ENV "${PY_CMD[@]}" >> "$LOGFILE" 2>&1 &
            echo "Started in background; logs -> $LOGFILE"
        else
            exec sudo -u "$RUN_USER" env $SUDO_ENV "${PY_CMD[@]}"
        fi
    else
        if [[ $BACKGROUND -eq 1 ]]; then
            nohup "${PY_CMD[@]}" >> "$LOGFILE" 2>&1 &
            echo "Started in background; logs -> $LOGFILE"
        else
            exec "${PY_CMD[@]}"
        fi
    fi
}

run_cmd
