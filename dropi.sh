#!/usr/bin/env bash
# Control del servicio de la página de reportes Dropi.
#
# Ejecútalo sin argumentos para ver el MENÚ DE SELECCIÓN:
#
#   ./dropi.sh
#
# También acepta el comando directo si lo prefieres:
#
#   ./dropi.sh iniciar | detener | reiniciar | estado
#
# El puerto se puede cambiar:  PUERTO=9000 ./dropi.sh
# El acceso desde otros equipos de la red viene ACTIVADO por defecto.
# Para restringirlo a solo este equipo:  LAN=0 ./dropi.sh

CARPETA="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
SERVIDOR="$CARPETA/servidor_dropi.py"
PIDFILE="$CARPETA/.dropi_servicio.pid"
LOG="$CARPETA/.dropi_servicio.log"
PUERTO="${PUERTO:-8765}"
LAN="${LAN:-1}"
URL="http://127.0.0.1:$PUERTO"
EXTRA=()
[ "$LAN" = "1" ] && EXTRA+=(--lan)

rojo()  { printf '\033[31m%s\033[0m\n' "$1"; }
verde() { printf '\033[32m%s\033[0m\n' "$1"; }
azul()  { printf '\033[36m%s\033[0m\n' "$1"; }

ip_red() { hostname -I 2>/dev/null | awk '{print $1}'; }

# URL para otros equipos de la red: si el servicio corre, se fija en cómo
# se arrancó el proceso; si no, en lo que se pidió con LAN=1
url_red() {
    if pid_activo; then
        en_lan || return 1
    else
        [ "$LAN" = "1" ] || return 1
    fi
    local ip
    ip="$(ip_red)"
    [ -n "$ip" ] || return 1
    echo "http://$ip:$PUERTO"
}

# ¿el proceso activo fue arrancado con --lan?
en_lan() {
    pid_activo || return 1
    tr '\0' ' ' < "/proc/$(cat "$PIDFILE")/cmdline" 2>/dev/null | grep -q -- '--lan'
}

pid_activo() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

corriendo() { pid_activo || pgrep -f "servidor_dropi\.py" >/dev/null 2>&1; }

# ------------------------------------------------------------- acciones --
accion_iniciar() {
    if pid_activo; then
        echo "El servicio ya está corriendo (PID $(cat "$PIDFILE"))."
        azul "  Página: $URL"
        return 0
    fi
    # si quedó un proceso huérfano de una corrida anterior, limpiarlo
    pids_huerfanos="$(pgrep -f "servidor_dropi\.py" 2>/dev/null)"
    [ -n "$pids_huerfanos" ] && kill $pids_huerfanos 2>/dev/null && sleep 0.5

    echo "Levantando el servicio…"
    nohup python3 "$SERVIDOR" --puerto "$PUERTO" "${EXTRA[@]}" >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1

    if pid_activo; then
        verde "✔ Servicio activo (PID $(cat "$PIDFILE"))"
        azul  "  Página: $URL"
        red="$(url_red)"
        [ -n "$red" ] && azul "  Desde otros equipos de tu red: $red"
        echo  "  Bitácora: $LOG"
        xdg-open "$URL" >/dev/null 2>&1 || true
    else
        rojo "✖ No pudo arrancar. Últimas líneas de la bitácora:"
        tail -n 10 "$LOG"
        rm -f "$PIDFILE"
        return 1
    fi
}

accion_detener() {
    if pid_activo; then
        pid="$(cat "$PIDFILE")"
        kill "$pid" 2>/dev/null
        sleep 0.5
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        verde "✔ Servicio detenido (PID $pid)"
    else
        # por si no hay pidfile pero quedó el proceso
        pids_huerfanos="$(pgrep -f "servidor_dropi\.py" 2>/dev/null)"
        if [ -n "$pids_huerfanos" ]; then
            kill $pids_huerfanos 2>/dev/null
            verde "✔ Servicio detenido (proceso huérfano: $pids_huerfanos)"
        else
            echo "El servicio no está corriendo."
        fi
        rm -f "$PIDFILE"
    fi
}

accion_estado() {
    if pid_activo; then
        verde "✔ Servicio corriendo (PID $(cat "$PIDFILE")) → $URL"
    elif pgrep -f "servidor_dropi\.py" >/dev/null 2>&1; then
        azul "≈ Servicio corriendo (sin PID propio) → $URL"
    else
        echo "El servicio está detenido."
    fi
    red="$(url_red)"
    [ -n "$red" ] && verde "  Accesible en la red local: $red"
    if curl -s -o /dev/null --max-time 2 "$URL/api/estado"; then
        verde "  La página responde correctamente."
    fi
}

accion_abrir() {
    if corriendo; then
        xdg-open "$URL" >/dev/null 2>&1 \
            && verde "✔ Abriendo $URL en tu navegador…" \
            || echo "No pude abrir el navegador; entra manualmente a: $URL"
    else
        rojo "El servicio está detenido; iníticalo primero (opción 1)."
    fi
}

# ------------------------------------------------------------------ menú --
mostrar_menu() {
    clear 2>/dev/null || true
    echo "╔═══════════════════════════════════════════════╗"
    echo "║        📦  REPORTES DROPI · SERVICIO          ║"
    echo "╚═══════════════════════════════════════════════╝"
    echo
    if corriendo; then
        verde " Estado: ● ACTIVO  →  $URL"
        red="$(url_red)"
        [ -n "$red" ] && verde "                       $red  (otros equipos)"
    else
        echo " Estado: ○ Detenido"
    fi
    echo
    echo " ─────────────────────────────────────────────"
    echo "   1) ▶  Iniciar servicio"
    echo "   2) ■  Detener servicio"
    echo "   3) ↻  Reiniciar servicio"
    echo "   4) 👁  Ver estado"
    echo "   5) 🌐  Abrir la página"
    echo "   0) ✕  Salir"
    echo " ─────────────────────────────────────────────"
    printf " Selecciona una opción [0-5]: "
}

menu() {
    while true; do
        mostrar_menu
        read -r opcion || { echo; exit 0; }   # EOF (Ctrl+D) => salir
        echo
        case "$opcion" in
            1) accion_iniciar ;;
            2) accion_detener ;;
            3) accion_detener >/dev/null; sleep 0.5; accion_iniciar ;;
            4) accion_estado ;;
            5) accion_abrir ;;
            0|q|Q) echo "Hasta pronto 👋"; exit 0 ;;
            *) rojo "Opción no válida: escribe un número del 0 al 5." ;;
        esac
        echo
        printf "Presiona Enter para volver al menú…"
        read -r _ || exit 0
    done
}

# ----------------------------------------------------------- punto entrada --
case "${1:-}" in
    iniciar|start)    accion_iniciar ;;
    detener|stop)     accion_detener ;;
    reiniciar|restart) accion_detener >/dev/null; sleep 0.5; accion_iniciar ;;
    estado|status)    accion_estado ;;
    abrir)            accion_abrir ;;
    "")               menu ;;
    *)
        echo "Uso: ./dropi.sh [iniciar|detener|reiniciar|estado|abrir]"
        echo "     (sin argumentos abre el menú de selección)"
        echo "Variables: PUERTO=9000 (otro puerto)  LAN=0 (solo este equipo, sin red)"
        exit 1
        ;;
esac
