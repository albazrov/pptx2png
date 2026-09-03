#!/bin/bash
# 20260903 - улучшенная версия с PID-файлом и ожиданием завершения

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
BOT_SCRIPT="bot.py"
ENV_NAME=$(basename "$PROJECT_DIR")

# Определяем Python
if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_EXEC="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$HOME/.venv/bin/python3" ]; then
    PYTHON_EXEC="$HOME/.venv/bin/python3"
else
    PYTHON_EXEC="python3"
fi

# Параметры SHM и логов
if [ -n "$2" ]; then
    SHM_DIR="$2"
else
    SHM_DIR=${SHM_DIR:-"/dev/shm/pptx2png_tasks/$ENV_NAME"}
fi

LOG_DIR="$SHM_DIR/logs"
LOG_FILE="$LOG_DIR/bot.log"
DEBUG_LOG_FILE="$LOG_DIR/debug.log"
NOHUP_LOG="$LOG_DIR/sys_nohup.log"

# PID-файл для управления процессом
PID_FILE="$PROJECT_DIR/.run_state/bot.pid"
mkdir -p "$(dirname "$PID_FILE")"

EXTRA_ARGS=(
    "--shm-dir" "$SHM_DIR"
    "--log-dir" "$LOG_DIR"
)

# --------------------------------------------
# Вспомогательная функция: убить процесс с ожиданием
# Параметры: PID, таймаут (секунды), имя процесса (для лога)
# Возвращает 0 при успешном завершении, 1 при ошибке
# --------------------------------------------
kill_with_wait() {
    local pid=$1
    local timeout=${2:-10}
    local name=${3:-"процесс"}

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ℹ️ $name (PID $pid) уже не активен"
        return 0
    fi

    echo "⏳ Остановка $name (PID $pid)..."
    kill "$pid"

    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ $waited -ge $timeout ]; then
            echo "⚠️ $name не завершился за ${timeout}с, принудительно завершаем..."
            kill -9 "$pid"
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                echo "❌ Не удалось завершить $name (PID $pid)"
                return 1
            else
                echo "✅ $name принудительно завершён"
                return 0
            fi
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "✅ $name завершён"
    return 0
}

# --------------------------------------------
# Команда start
# --------------------------------------------
cmd_start() {
    echo "🚀 Запуск бота ($ENV_NAME)..."
    mkdir -p "$LOG_DIR"
    chmod 775 "$SHM_DIR" 2>/dev/null || true
    chmod 775 "$LOG_DIR" 2>/dev/null || true

    # Проверяем, не запущен ли уже бот (по PID-файлу)
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️ Бот уже запущен (PID $pid)!"
            exit 1
        else
            rm -f "$PID_FILE"
        fi
    fi

    # Запускаем бота
    nohup "$PYTHON_EXEC" -u "$PROJECT_DIR/$BOT_SCRIPT" "${EXTRA_ARGS[@]}" > "$NOHUP_LOG" 2>&1 &
    local new_pid=$!
    echo $new_pid > "$PID_FILE"

    sleep 1.5
    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Бот успешно запущен (PID $new_pid)."
        echo "📄 Логи: $LOG_FILE, $DEBUG_LOG_FILE"
        exit 0
    else
        echo "❌ Ошибка старта! Проверьте $NOHUP_LOG"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# --------------------------------------------
# Команда stop
# --------------------------------------------
cmd_stop() {
    echo "🛑 Остановка бота ($ENV_NAME)..."
    local stop_success=0

    # 1. Остановка по PID-файлу
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill_with_wait "$pid" 10 "бот из PID-файла"
            if [ $? -eq 0 ]; then
                rm -f "$PID_FILE"
                stop_success=0
            else
                echo "❌ Не удалось остановить процесс из PID-файла"
                return 1
            fi
        else
            echo "⚠️ PID-файл есть, но процесс $pid не активен. Удаляем файл."
            rm -f "$PID_FILE"
        fi
    fi

    # 2. Поиск и остановка legacy-процессов (запущенных без PID-файла)
    # Ищем все процессы python, запускающие bot.py из этой директории
    local legacy_pids=$(pgrep -f "python.*$PROJECT_DIR/$BOT_SCRIPT" 2>/dev/null)
    if [ -n "$legacy_pids" ]; then
        echo "🔍 Найдены legacy-процессы: $legacy_pids"
        for pid in $legacy_pids; do
            # Пропускаем процесс, который уже был остановлен (если PID совпадает)
            if [ -f "$PID_FILE" ] && [ "$pid" == "$(cat "$PID_FILE")" ]; then
                continue
            fi
            kill_with_wait "$pid" 10 "legacy-процесс $pid"
            if [ $? -ne 0 ]; then
                echo "❌ Не удалось остановить legacy-процесс $pid"
                return 1
            fi
        done
        rm -f "$PID_FILE"
    else
        echo "ℹ️ Legacy-процессы не найдены"
    fi

    echo "✅ Все процессы остановлены"
    return 0
}

# --------------------------------------------
# Команда restart
# --------------------------------------------
cmd_restart() {
    echo "🔄 Перезапуск бота ($ENV_NAME)..."
    cmd_stop
    local stop_status=$?
    if [ $stop_status -ne 0 ]; then
        echo "❌ Не удалось остановить бота, перезапуск прерван."
        exit 1
    fi
    sleep 1.5
    cmd_start "$2"
}

# --------------------------------------------
# Команда status
# --------------------------------------------
cmd_status() {
    # Проверяем по PID-файлу
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "🟢 Бот РАБОТАЕТ (PID: $pid) [$ENV_NAME]"
            echo "📊 RAM: $SHM_DIR"
            return 0
        else
            echo "🔴 Бот ОСТАНОВЛЕН (PID-файл есть, но процесс мёртв) [$ENV_NAME]"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        # Проверяем, не запущен ли legacy процесс без PID-файла
        local legacy_pid=$(pgrep -f "python.*$PROJECT_DIR/$BOT_SCRIPT" 2>/dev/null | head -n 1)
        if [ -n "$legacy_pid" ]; then
            echo "⚠️ Найден legacy-процесс (PID: $legacy_pid) без PID-файла. Рекомендуется выполнить 'stop'."
            return 1
        else
            echo "🔴 Бот ОСТАНОВЛЕН [$ENV_NAME]"
            return 1
        fi
    fi
}

# --------------------------------------------
# Команды для логов
# --------------------------------------------
cmd_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -n 10 -f "$LOG_FILE"
    else
        echo "❌ Лог не найден: $LOG_FILE"
        exit 1
    fi
}

cmd_debug_logs() {
    if [ -f "$DEBUG_LOG_FILE" ]; then
        tail -n 15 -f "$DEBUG_LOG_FILE"
    else
        echo "❌ Лог не найден: $DEBUG_LOG_FILE"
        exit 1
    fi
}

cmd_clear_logs() {
    for f in "$LOG_FILE" "$DEBUG_LOG_FILE" "$NOHUP_LOG"; do
        [ -f "$f" ] && true > "$f"
    done
    echo "🧹 Логи очищены."
    exit 0
}

# --------------------------------------------
# Диспетчер команд
# --------------------------------------------
case "$1" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart "$2"
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    debug-logs)
        cmd_debug_logs
        ;;
    clear-logs)
        cmd_clear_logs
        ;;
    *)
        echo "📋 Использование: $0 {start|stop|restart|status|logs|debug-logs|clear-logs} [кастомный_shm]"
        exit 1
        ;;
esac
