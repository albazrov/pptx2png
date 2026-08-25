#!/bin/bash

# Пути к файлам и папкам
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
BOT_SCRIPT="bot.py"
LOG_DIR="/dev/shm/pptx2png_tasks/logs"
LOG_FILE="$LOG_DIR/bot.log"

# Функция для подготовки окружения в RAM
init_ram_disk() {
    mkdir -p "$LOG_DIR"
    chmod -R 775 "/dev/shm/pptx2png_tasks" 2>/dev/null
}

case "$1" in
    start)
        echo "🚀 Запуск бота..."
        init_ram_disk
        
        # Точная проверка: ищем только запущенный python-процесс с нашим ботом
        if pgrep -f "python3.*$BOT_SCRIPT" > /dev/null; then
            echo "⚠️ Бот уже запущен!"
            exit 1
        fi
        
        # Запуск из виртуального окружения (проверяем локально или в корне пользователя)
        if [ -f "$PROJECT_DIR/.venv/bin/python3" ]; then
            PYTHON_EXEC="$PROJECT_DIR/.venv/bin/python3"
        elif [ -f "$HOME/.venv/bin/python3" ]; then
            PYTHON_EXEC="$HOME/.venv/bin/python3"
        else
            PYTHON_EXEC="python3"
        fi

        nohup "$PYTHON_EXEC" "$PROJECT_DIR/$BOT_SCRIPT" > "$LOG_FILE" 2>&1 &
        
        sleep 1.5
        if pgrep -f "python3.*$BOT_SCRIPT" > /dev/null; then
            echo "✅ Бот успешно запущен в фоне."
            echo "📄 Логи пишутся в RAM: $LOG_FILE"
        else
            echo "❌ Ошибка старта! Проверьте логи командой: ./manage.sh logs"
        fi
        ;;
        
    stop)
        echo "🛑 Остановка бота..."
        # Находим конкретный PID python-процесса бота
        BOT_PID=$(pgrep -f "python3.*$BOT_SCRIPT")
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
        $0 start
        ;;
        
    status)
        if pgrep -f "python3.*$BOT_SCRIPT" > /dev/null; then
            PID=$(pgrep -f "python3.*$BOT_SCRIPT" | head -n 1)
            echo "🟢 Бот РАБОТАЕТ (PID: $PID)"
            echo "📊 Использование RAM-диска папки задач:"
            du -sh /dev/shm/pptx2png_tasks 2>/dev/null
        else
            echo "🔴 Бот ОСТАНОВЛЕН"
        fi
        ;;
        
    logs)
        if [ -f "$LOG_FILE" ]; then
            echo "📋 Вывод логов в реальном времени (нажмите Ctrl+C для выхода):"
            tail -f "$LOG_FILE"
        else
            echo "❌ Файл логов еще не создан. Запустите бота."
        fi
        ;;
        
    clear-logs)
        if [ -f "$LOG_FILE" ]; then
            true > "$LOG_FILE"
            echo "🧹 Лог-файл в RAM успешно очищен и сброшен до 0 байт."
        else
            echo "❌ Лог-файл не найден."
        fi
        ;;
        
    *)
        echo "📋 Использование: $0 {start|stop|restart|status|logs|clear-logs}"
        exit 1
        ;;
esac

exit 0

