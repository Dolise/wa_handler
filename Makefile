.PHONY: help install run test logs clean

help:
	@echo "WhatsApp Registration Agent - Makefile"
	@echo ""
	@echo "Доступные команды:"
	@echo "  make install     - Установить зависимости"
	@echo "  make run         - Запустить агент"
	@echo "  make test        - Тестовый запуск с отладкой"
	@echo "  make logs        - Показать логи systemd service"
	@echo "  make clean       - Очистить временные файлы"

install:
	@echo "📦 Установка зависимостей..."
	pip install -r requirements.txt
	@echo "✅ Готово!"

run:
	@echo "🚀 Запуск агента..."
	python main.py

test:
	@echo "🧪 Тестовый запуск с подробными логами..."
	POLL_INTERVAL=2 python main.py

logs:
	@echo "📋 Логи systemd service..."
	journalctl -u wa-agent -n 100 -f

clean:
	@echo "🧹 Очистка..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Готово!"

