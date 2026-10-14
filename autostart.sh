#!/bin/bash
# gPhoto Explorer autostart — lanciato da udev quando la ZV-E10 viene collegata.
# Passa la sessione grafica a python3 e evita istanze duplicate.

APP=/home/gibbo/gphoto-explorer/main.py

# Se l'app gira già, niente da fare
if pgrep -f "gphoto-explorer/main.py" >/dev/null 2>&1; then
    exit 0
fi

export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# Trova un processo CLIENT della sessione Plasma (plasmashell è un client Qt
# e ha DISPLAY/WAYLAND_DISPLAY/XAUTHORITY). kwin_wayland è il compositor:
# NON ha quelle variabili, quindi non usarlo come fonte.
for PROC in plasmashell dolphin konsole yakuake krunner; do
    SESSION_PID=$(pgrep -u "$(id -u)" -f "$PROC" | head -1)
    [ -n "$SESSION_PID" ] && break
done

if [ -n "$SESSION_PID" ] && [ -r "/proc/$SESSION_PID/environ" ]; then
    SESSION_ENV=$(tr '\0' '\n' < "/proc/$SESSION_PID/environ")
    export DISPLAY=$(echo "$SESSION_ENV" | grep '^DISPLAY=' | cut -d= -f2-)
    export WAYLAND_DISPLAY=$(echo "$SESSION_ENV" | grep '^WAYLAND_DISPLAY=' | cut -d= -f2-)
    export XAUTHORITY=$(echo "$SESSION_ENV" | grep '^XAUTHORITY=' | cut -d= -f2-)
    export DBUS_SESSION_BUS_ADDRESS=$(echo "$SESSION_ENV" | grep '^DBUS_SESSION_BUS_ADDRESS=' | cut -d= -f2-)
fi

# Fallback: derive dalle socket presenti nella sessione utente
[ -z "$DISPLAY" ] && [ -S "/tmp/.X11-unix/X0" ] && export DISPLAY=:0
[ -z "$WAYLAND_DISPLAY" ] && [ -S "$XDG_RUNTIME_DIR/wayland-0" ] && export WAYLAND_DISPLAY=wayland-0
[ -z "$DBUS_SESSION_BUS_ADDRESS" ] && [ -S "$XDG_RUNTIME_DIR/bus" ] && export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
if [ -z "$XAUTHORITY" ]; then
    # Cerca un file xauth attivo della sessione
    for f in "$XDG_RUNTIME_DIR"/xauth_*; do
        [ -f "$f" ] && export XAUTHORITY="$f" && break
    done
fi

# Piccolo delay: lascia che gphotofs finisca il mount
sleep 2

# exec sostituisce bash con python3: la app diventa il processo principale
# del service (Type=simple). Con nohup & systemd ucciderebbe i figli orfani
# quando lo script termina.
exec /usr/bin/python3 "$APP"