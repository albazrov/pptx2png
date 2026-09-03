#!/bin/bash
# 20260828

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
BOT_SCRIPT="bot.py"
ENV_NAME=$(basename "$PROJECT_DIR")

if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_EXEC="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$HOME/.venv/bin/python3" ]; then
    PYTHON_EXEC="$HOME/.venv/bin/python3"
else
    PYTHON_EXEC="python3"
fi

if [ -n "$2" ]; then
    SHM_DIR="$2"
else
    SHM_DIR=${SHM_DIR:-"/dev/shm/pptx2png_tasks/$ENV_NAME"}
fi

LOG_DIR="$SHM_DIR/logs"
LOG_FILE="$LOG_DIR/bot.log"
DEBUG_LOG_FILE="$LOG_DIR/debug.log"
NOHUP_LOG="$LOG_DIR/sys_nohup.log"

EXTRA_ARGS=(
    "--shm-dir" "$SHM_DIR"
    "--log-dir" "$LOG_DIR"
)

case "$1" in
    start)
        echo "🚀 Запуск бота ($ENV_NAME)..."
        mkdir -p "$LOG_DIR"
        chmod 775 "$SHM_DIR" 2>/dev/null || true
        chmod 775 "$LOG_DIR" 2>/dev/null || true

        if pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" > /dev/null; then
            echo "⚠️ Бот уже запущен!"
            exit 1
        fi

        nohup "$PYTHON_EXEC" -u "$PROJECT_DIR/$BOT_SCRIPT" "${EXTRA_ARGS[@]}" > "$NOHUP_LOG" 2>&1 &
        sleep 1.5

        if pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" > /dev/null; then
            echo "✅ Бот успешно запущен."
            echo "📄 Логи: $LOG_FILE, $DEBUG_LOG_FILE"
            exit 0
        else
            echo "❌ Ошибка старта! Проверьте $NOHUP_LOG"
            exit 1
        fi
        ;;

    stop)
        echo "🛑 Остановка бота ($ENV_NAME)..."
        BOT_PID=$(pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT")
        if [ -n "$BOT_PID" ]; then
            kill $BOT_PID
            echo "✅ Бот остановлен."
            exit 0
        else
            echo "⚠️ Процесс не найден."
            exit 1
        fi
        ;;

    restart)
        $0 stop
        sleep 1.5
        $0 start "$2"
        ;;

    status)
        if pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" > /dev/null; then
            PID=$(pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" | head -n 1)
            echo "🟢 Бот РАБОТАЕТ (PID: $PID) [$ENV_NAME]"
            echo "📊 RAM: $SHM_DIR"
            exit 0
        else
            echo "🔴 Бот ОСТАНОВЛЕН [$ENV_NAME]"
            exit 1
        fi
        ;;

    logs)
        if [ -f "$LOG_FILE" ]; then
            tail -n 10 -f "$LOG_FILE"
        else
            echo "❌ Лог не найден: $LOG_FILE"
            exit 1
        fi
        ;;

    debug-logs)
        if [ -f "$DEBUG_LOG_FILE" ]; then
            tail -n 15 -f "$DEBUG_LOG_FILE"
        else
            echo "❌ Лог не найден: $DEBUG_LOG_FILE"
            exit 1
        fi
        ;;

    clear-logs)
        for f in "$LOG_FILE" "$DEBUG_LOG_FILE" "$NOHUP_LOG"; do
            [ -f "$f" ] && true > "$f"
        done
        echo "🧹 Логи очищены."
        exit 0
        ;;

    *)
        echo "📋 Использование: $0 {start|stop|restart|status|logs|debug-logs|clear-logs} [кастомный_shm]"
        exit 1
        ;;
esac
