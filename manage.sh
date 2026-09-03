#!/bin/bash
# 20260903 - улучшенная версия с PID-файлом, проверкой времени старта и состоянием процесса

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

# PID-файл и директория
PID_FILE="$PROJECT_DIR/.run_state/bot.pid"
mkdir -p "$(dirname "$PID_FILE")"

EXTRA_ARGS=(
    "--shm-dir" "$SHM_DIR"
    "--log-dir" "$LOG_DIR"
)

# --------------------------------------------
# Функция получения времени старта процесса (в тиках)
# --------------------------------------------
get_process_starttime() {
    local pid=$1
    if [ ! -f "/proc/$pid/stat" ]; then
        echo ""
        return 1
    fi
    awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

# --------------------------------------------
# Функция получения состояния процесса (R, S, Z, T и т.д.)
# --------------------------------------------
get_process_state() {
    local pid=$1
    ps -o state= -p "$pid" 2>/dev/null
}

# --------------------------------------------
# Проверка, что процесс с данным PID и starttime существует и не является зомби
# --------------------------------------------
is_valid_process() {
    local pid=$1
    local saved_starttime=$2
    local current_starttime=$(get_process_starttime "$pid")
    if [ -z "$current_starttime" ]; then
        return 1
    fi
    # Проверяем, что starttime совпадает
    [ "$current_starttime" != "$saved_starttime" ] && return 1

    # Проверяем состояние процесса (не должен быть зомби)
    local state=$(get_process_state "$pid")
    if [ -z "$state" ] || [ "$state" == "Z" ]; then
        return 1
    fi
    return 0
}

# --------------------------------------------
# Убить процесс с ожиданием, если он соответствует сохранённому starttime
# --------------------------------------------
kill_with_wait() {
    local pid=$1
    local expected_starttime=$2
    local timeout=${3:-10}
    local name=${4:-"процесс"}

    if ! is_valid_process "$pid" "$expected_starttime"; then
        echo "⚠️ Процесс $pid не соответствует ожидаемому (starttime не совпадает или зомби). Пропускаем."
        return 0   # Возвращаем 0, т.к. процесс уже не тот, которого мы ждали
    fi

    echo "⏳ Остановка $name (PID $pid)..."
    kill "$pid"

    local waited=0
    while is_valid_process "$pid" "$expected_starttime"; do
        if [ $waited -ge $timeout ]; then
            echo "⚠️ $name не завершился за ${timeout}с, принудительно завершаем..."
            kill -9 "$pid"
            # Ждём до 5 секунд после SIGKILL
            local kill_waited=0
            while is_valid_process "$pid" "$expected_starttime" && [ $kill_waited -lt 5 ]; do
                sleep 1
                kill_waited=$((kill_waited + 1))
            done
            if is_valid_process "$pid" "$expected_starttime"; then
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
# Остановка одного legacy-процесса по PID с проверкой starttime и состояния
# --------------------------------------------
stop_legacy_process() {
    local pid=$1
    local name=${2:-"legacy-процесс"}

    local original_starttime=$(get_process_starttime "$pid")
    if [ -z "$original_starttime" ]; then
        echo "ℹ️ $name (PID $pid) уже не активен"
        return 0
    fi

    echo "⏳ Остановка $name (PID $pid)..."

    # Отправляем SIGTERM
    kill "$pid" 2>/dev/null

    # Ждём до 5 секунд для graceful завершения
    local waited=0
    while [ $waited -lt 5 ]; do
        # Проверяем, жив ли процесс с тем же starttime и не зомби ли он
        if ! is_valid_process "$pid" "$original_starttime"; then
            echo "✅ $name (PID $pid) завершён"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Если процесс с тем же starttime всё ещё жив, принудительно завершаем
    if is_valid_process "$pid" "$original_starttime"; then
        echo "⚠️ $name не завершился gracefully, принудительно завершаем..."
        kill -9 "$pid" 2>/dev/null

        # Ждём до 5 секунд после SIGKILL
        local kill_waited=0
        while [ $kill_waited -lt 5 ]; do
            if ! is_valid_process "$pid" "$original_starttime"; then
                echo "✅ $name (PID $pid) принудительно завершён"
                return 0
            fi
            sleep 1
            kill_waited=$((kill_waited + 1))
        done

        # Если после SIGKILL процесс с тем же starttime всё ещё жив — ошибка
        if is_valid_process "$pid" "$original_starttime"; then
            echo "❌ Не удалось завершить $name (PID $pid)"
            return 1
        else
            echo "✅ $name (PID $pid) завершён"
            return 0
        fi
    else
        echo "✅ $name (PID $pid) завершён"
        return 0
    fi
}

# --------------------------------------------
# Команда start
# --------------------------------------------
cmd_start() {
    echo "🚀 Запуск бота ($ENV_NAME)..."
    mkdir -p "$LOG_DIR"
    chmod 775 "$SHM_DIR" 2>/dev/null || true
    chmod 775 "$LOG_DIR" 2>/dev/null || true

    if [ -f "$PID_FILE" ]; then
        local saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        local saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ] && is_valid_process "$saved_pid" "$saved_starttime"; then
            echo "⚠️ Бот уже запущен (PID $saved_pid)!"
            exit 1
        else
            echo "⚠️ Найден устаревший PID-файл. Удаляем."
            rm -f "$PID_FILE"
        fi
    fi

    nohup "$PYTHON_EXEC" -u "$PROJECT_DIR/$BOT_SCRIPT" "${EXTRA_ARGS[@]}" > "$NOHUP_LOG" 2>&1 &
    local new_pid=$!

    sleep 0.5
    local new_starttime=$(get_process_starttime "$new_pid")
    if [ -z "$new_starttime" ]; then
        echo "❌ Не удалось получить время старта процесса $new_pid"
        rm -f "$PID_FILE"
        exit 1
    fi

    echo "$new_pid:$new_starttime" > "$PID_FILE"

    sleep 1
    if is_valid_process "$new_pid" "$new_starttime"; then
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

    # 1. Остановка по PID-файлу
    if [ -f "$PID_FILE" ]; then
        local saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        local saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ]; then
            if is_valid_process "$saved_pid" "$saved_starttime"; then
                kill_with_wait "$saved_pid" "$saved_starttime" 10 "бот из PID-файла"
                if [ $? -eq 0 ]; then
                    rm -f "$PID_FILE"
                else
                    echo "❌ Не удалось остановить процесс из PID-файла"
                    return 1
                fi
            else
                echo "⚠️ PID-файл есть, но процесс $saved_pid не соответствует сохранённому starttime или стал зомби. Удаляем файл."
                rm -f "$PID_FILE"
            fi
        else
            echo "⚠️ Некорректный PID-файл, удаляем."
            rm -f "$PID_FILE"
        fi
    fi

    # 2. Поиск и остановка legacy-процессов (запущенных без PID-файла)
    local legacy_pids=$(pgrep -f "python.*$PROJECT_DIR/$BOT_SCRIPT" 2>/dev/null)
    if [ -n "$legacy_pids" ]; then
        echo "🔍 Найдены legacy-процессы: $legacy_pids"
        for pid in $legacy_pids; do
            # Пропускаем, если процесс уже обработан через PID-файл
            if [ -f "$PID_FILE" ]; then
                local file_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
                if [ "$pid" == "$file_pid" ]; then
                    continue
                fi
            fi

            # Проверяем, что процесс действительно принадлежит этому проекту
            local cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
            if [[ "$cmdline" == *"$PROJECT_DIR/$BOT_SCRIPT"* ]]; then
                # Останавливаем legacy-процесс с проверкой starttime и состояния
                stop_legacy_process "$pid" "legacy-процесс $pid"
                if [ $? -ne 0 ]; then
                    echo "❌ Не удалось остановить legacy-процесс $pid"
                    return 1
                fi
            else
                echo "ℹ️ Процесс $pid не относится к этому боту (пропускаем)."
            fi
        done
    else
        echo "ℹ️ Legacy-процессы не найдены"
    fi

    # Удаляем PID-файл, если он ещё существует
    rm -f "$PID_FILE"
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
    if [ -f "$PID_FILE" ]; then
        local saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        local saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ] && is_valid_process "$saved_pid" "$saved_starttime"; then
            echo "🟢 Бот РАБОТАЕТ (PID: $saved_pid) [$ENV_NAME]"
            echo "📊 RAM: $SHM_DIR"
            return 0
        else
            echo "🔴 Бот ОСТАНОВЛЕН (PID-файл есть, но процесс не соответствует, мёртв или стал зомби) [$ENV_NAME]"
            rm -f "$PID_FILE"
            return 1
        fi
    else
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
