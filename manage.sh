#!/bin/bash

# 20260828

# Пути к файлам и папкам
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
BOT_SCRIPT="bot.py"

echo "PROJECT_DIR  = $PROJECT_DIR"
echo "BOT_SCRIPT   = $BOT_SCRIPT"

ENV_NAME=$(basename "$PROJECT_DIR")
echo "ENV_NAME     = $ENV_NAME"

# Определяем бинарник Python с проверкой прав на выполнение (-x)
if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_EXEC="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$HOME/.venv/bin/python3" ]; then
    PYTHON_EXEC="$HOME/.venv/bin/python3"
else
    PYTHON_EXEC="python3"
fi
echo "PYTHON_EXEC  = $PYTHON_EXEC"

# ОПРЕДЕЛЕНИЕ ПУТИ К RAM-ДИСКУ С УЧЕТОМ ОКРУЖЕНИЯ PPTX2PNG:
if [ -n "$2" ]; then
    SHM_DIR="$2"
else
    SHM_DIR=${SHM_DIR:-"/dev/shm/pptx2png_tasks/$ENV_NAME"}
fi

# УПРАВЛЯЮЩИЙ СКРИПТ ОДНОЗНАЧНО СТРОИТ ПУТЬ К ЛОГАМ ДЛЯ СЕБЯ И ДЛЯ БОТА
LOG_DIR="$SHM_DIR/logs"
LOG_FILE="$LOG_DIR/bot.log"
DEBUG_LOG_FILE="$LOG_DIR/debug.log"
NOHUP_LOG="$LOG_DIR/sys_nohup.log"

# ИСПОЛЬЗУЕМ BASH-МАССИВ: безопасное раскрытие путей с пробелами
EXTRA_ARGS=(
    "--shm-dir" "$SHM_DIR"
    "--log-dir" "$LOG_DIR"
)

echo "SHM_DIR      = $SHM_DIR"
echo "LOG_DIR      = $LOG_DIR"
echo "EXTRA_ARGS   = ${EXTRA_ARGS[*]}"
echo "LOG_FILE     = $LOG_FILE"

case "$1" in
    start)
        echo "🚀 Запуск бота ($ENV_NAME)..."
        # Создаем всю иерархию папок в RAM-диске с сохранением структуры
        mkdir -p "$LOG_DIR"
        chmod 775 "$SHM_DIR" 2>/dev/null || true
        chmod 775 "$LOG_DIR" 2>/dev/null || true
        
        if pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" > /dev/null; then
            echo "⚠️ Бот уже запущен!"
            exit 1
        fi

        # Запускаем скрипт, раскрывая массив аргументов с сохранением пробелов
        nohup "$PYTHON_EXEC" -u "$PROJECT_DIR/$BOT_SCRIPT" "${EXTRA_ARGS[@]}" > "$NOHUP_LOG" 2>&1 &        
        sleep 1.5
        
        if pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT" > /dev/null; then
            echo "✅ Бот успешно запущен в фоне."
            echo "📄 Основные логи пишутся в: $LOG_FILE"
            echo "📄 Подробный дебаг пишется в: $DEBUG_LOG_FILE"
        else
            echo "❌ Ошибка старта! Проверьте системный лог: tail -n 20 \"$NOHUP_LOG\""
        fi
        ;;
        
    stop)
        echo "🛑 Остановка бота ($ENV_NAME)..."
        BOT_PID=$(pgrep -f "python3.*$PROJECT_DIR/$BOT_SCRIPT")
        if [ -n "$BOT_PID" ]; then
            kill $BOT_PID
            echo "✅ Бот успешно остановлен."
        else
            echo "⚠️ Процесс бота не найден."
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
            echo "📊 Активный RAM-диск: $SHM_DIR"
            echo "✏️ Основной лог: $LOG_FILE"
            echo "✏️ Подробный лог: $DEBUG_LOG_FILE"
        else
            echo "🔴 Бот ОСТАНОВЛЕН [$ENV_NAME]"
        fi
        ;;
        
    logs)
        if [ -f "$LOG_FILE" ]; then
            echo "📋 Вывод основного лога (INFO) в реальном времени (Ctrl+C для выхода) [$ENV_NAME]:"
            tail -n 10 -f "$LOG_FILE"
        else
            echo "❌ Файл логов еще не создан по пути: $LOG_FILE"
        fi
        ;;

    debug-logs)
        if [ -f "$DEBUG_LOG_FILE" ]; then
            echo "📋 Вывод подробного лога (DEBUG) в реальном времени (Ctrl+C для выхода) [$ENV_NAME]:"
            tail -n 15 -f "$DEBUG_LOG_FILE"
        else
            echo "❌ Файл дебаг-логов еще не создан по пути: $DEBUG_LOG_FILE"
        fi
        ;;
        
    clear-logs)
        # Очищаем файлы логов, корректно обрабатывая возможные пробелы в путях
        for f in "$LOG_FILE" "$DEBUG_LOG_FILE" "$NOHUP_LOG"; do
            [ -f "$f" ] && true > "$f"
        done
        echo "🧹 Все лог-файлы в RAM-диске окружения успешно очищены."
        ;;
        
    *)
        echo "📋 Использование: $0 {start|stop|restart|status|logs|debug-logs|clear-logs} [кастомный_путь_shm]"
        exit 1
        ;;
esac
exit 0
