"""
Агент для выполнения WhatsApp регистраций на эмуляторах.

Архитектура:
  - На тачке запущено N эмуляторов (emulator-5554, emulator-5556, ...)
  - Агент полит handler (/agent/poll), получает задачи (номера телефонов)
  - Распределяет задачи по свободным эмуляторам из пула (asyncio.Queue)
  - Каждый эмулятор выполняет регистрацию через RegistrationExecutor
  - После завершения эмулятор возвращается в пул свободных
  - Статусы отправляются в handler (/agent/status)

Env переменные:
  HANDLER_URL       — URL handler'а (default: http://5.129.204.230:8000)
  AGENT_ID          — ID агента (default: hostname)
  EMULATOR_COUNT    — Количество эмуляторов (default: 10)
  EMULATOR_BASE_PORT — Базовый порт (default: 5554)
  POLL_INTERVAL     — Интервал polling если нет задач (default: 5 сек)
  POLL_BACKOFF      — Пауза при ошибках (default: 10 сек)
  STATUS_RETRY      — Retry для отправки статусов (default: 3)
"""

import asyncio
import logging
import os
import signal
import socket
import sys
from typing import Any, Dict

import httpx

from registration_executor import RegistrationExecutor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Конфиг
HANDLER_URL = os.getenv("HANDLER_URL", "http://5.129.204.230:8000")
AGENT_ID = os.getenv("AGENT_ID", socket.gethostname())
EMULATOR_COUNT = int(os.getenv("EMULATOR_COUNT", "10"))
EMULATOR_BASE_PORT = int(os.getenv("EMULATOR_BASE_PORT", "5554"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
POLL_BACKOFF = float(os.getenv("POLL_BACKOFF", "10"))
STATUS_RETRY = int(os.getenv("STATUS_RETRY", "3"))

# Создаем список эмуляторов: emulator-5554, emulator-5556, ...
EMULATOR_IDS = [f"emulator-{EMULATOR_BASE_PORT + 2 * i}" for i in range(EMULATOR_COUNT)]

# Глобальный флаг для graceful shutdown
shutdown_event = asyncio.Event()


async def post_status(
    client: httpx.AsyncClient,
    phone: str,
    status: str,
    emulator: str = "",
    code: str = "",
    error: str = "",
) -> None:
    """Отправить статус выполнения в handler с retry"""
    payload: Dict[str, Any] = {"phone": phone, "status": status}
    if emulator:
        payload["emulator"] = emulator
    if code:
        payload["code"] = code
    if error:
        payload["error"] = error

    for attempt in range(STATUS_RETRY):
        try:
            resp = await client.post(f"{HANDLER_URL}/agent/status", json=payload, timeout=15)
            resp.raise_for_status()
            logger.debug(f"[{phone}] Status updated: {status}")
            return
        except Exception as e:
            if attempt == STATUS_RETRY - 1:
                logger.error(f"[{phone}] Failed to send status after {STATUS_RETRY} attempts: {e}")
                # Не падаем, просто логируем
                return
            await asyncio.sleep(1)


async def run_job(client: httpx.AsyncClient, phone: str, emulator_id: str) -> None:
    """Выполнить регистрацию на эмуляторе"""
    # Парсим порт из emulator_id (emulator-5554 -> 5554)
    try:
        port = int(emulator_id.split("-")[-1])
    except (ValueError, IndexError):
        logger.error(f"[{phone}] Invalid emulator_id format: {emulator_id}")
        await post_status(client, phone, "failed", emulator=emulator_id, error="Invalid emulator_id")
        return

    logger.info(f"[{phone}] Starting registration on {emulator_id}")
    await post_status(client, phone, "starting", emulator=emulator_id)

    try:
        # Создаем executor и запускаем в thread pool (не блокируем event loop!)
        executor = RegistrationExecutor(phone=phone, emulator_id=emulator_id, port=port)
        result = await asyncio.to_thread(executor.execute)
        
        # Извлекаем код из результата
        code = result.get("code", "") if isinstance(result, dict) else ""
        
        logger.info(f"[{phone}] Registration completed on {emulator_id}, code: {code or 'N/A'}")
        await post_status(client, phone, "completed", emulator=emulator_id, code=code)
        
    except Exception as e:
        logger.error(f"[{phone}] Registration failed on {emulator_id}: {e}", exc_info=True)
        await post_status(client, phone, "failed", emulator=emulator_id, error=str(e))


async def emulator_worker(
    client: httpx.AsyncClient,
    emulator_id: str,
    job_queue: asyncio.Queue,
    emulator_pool: asyncio.Queue
) -> None:
    """Воркер для одного эмулятора - берет задачи из очереди и выполняет"""
    logger.info(f"[{emulator_id}] Worker started")
    
    while not shutdown_event.is_set():
        try:
            # Помещаем эмулятор в пул свободных
            await emulator_pool.put(emulator_id)
            logger.debug(f"[{emulator_id}] Ready for work")
            
            # Ждем задачу из очереди
            try:
                job = await asyncio.wait_for(job_queue.get(), timeout=1.0)
                
                # Если взяли задачу - убираем себя из пула свободных
                try:
                    emulator_pool.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            except asyncio.TimeoutError:
                # Забираем эмулятор обратно из пула (он не взял задачу)
                try:
                    emulator_pool.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                continue
            
            phone = job.get("phone")
            if not phone:
                logger.warning(f"[{emulator_id}] Job without phone: {job}")
                job_queue.task_done()
                continue
            
            # Выполняем регистрацию
            await run_job(client, phone, emulator_id)
            job_queue.task_done()
            
        except Exception as e:
            logger.error(f"[{emulator_id}] Worker error: {e}", exc_info=True)
            await asyncio.sleep(1)
    
    logger.info(f"[{emulator_id}] Worker stopped")


async def poll_and_distribute(
    client: httpx.AsyncClient,
    job_queue: asyncio.Queue,
    emulator_pool: asyncio.Queue
) -> None:
    """Основной цикл: полит handler и распределяет задачи"""
    logger.info(f"Starting poll loop, handler={HANDLER_URL}")
    
    while not shutdown_event.is_set():
        try:
            # Проверяем сколько свободных эмуляторов
            free_count = emulator_pool.qsize()
            
            if free_count == 0:
                # Нет свободных эмуляторов - ждем
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            # Запрашиваем задачи (не больше чем свободных эмуляторов)
            capacity = min(free_count, EMULATOR_COUNT)
            
            resp = await client.post(
                f"{HANDLER_URL}/agent/poll",
                json={"agent_id": AGENT_ID, "capacity": capacity},
                timeout=15,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
            
            if not jobs:
                logger.debug(f"No jobs received, sleeping {POLL_INTERVAL}s")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            logger.info(f"Received {len(jobs)} job(s) from handler")
            
            # Добавляем задачи в очередь
            for job in jobs:
                await job_queue.put(job)
            
            # Небольшая пауза перед следующим poll
            await asyncio.sleep(0.5)
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error during poll: {e}")
            await asyncio.sleep(POLL_BACKOFF)
        except Exception as e:
            logger.error(f"Unexpected error during poll: {e}", exc_info=True)
            await asyncio.sleep(POLL_BACKOFF)
    
    logger.info("Poll loop stopped")


async def main_async() -> None:
    """Главная асинхронная функция"""
    # Создаем очереди
    job_queue: asyncio.Queue = asyncio.Queue()
    emulator_pool: asyncio.Queue = asyncio.Queue()
    
    # HTTP клиент
    async with httpx.AsyncClient() as client:
        # Запускаем воркеры для каждого эмулятора
        workers = []
        for emu_id in EMULATOR_IDS:
            worker = asyncio.create_task(
                emulator_worker(client, emu_id, job_queue, emulator_pool)
            )
            workers.append(worker)
        
        # Запускаем poll loop
        poll_task = asyncio.create_task(
            poll_and_distribute(client, job_queue, emulator_pool)
        )
        
        logger.info(f"Agent {AGENT_ID} started with {EMULATOR_COUNT} emulators")
        
        # Ждем сигнала shutdown
        await shutdown_event.wait()
        
        logger.info("Shutting down gracefully...")
        
        # Отменяем все задачи
        poll_task.cancel()
        for worker in workers:
            worker.cancel()
        
        # Ждем завершения
        await asyncio.gather(poll_task, *workers, return_exceptions=True)
        
        logger.info("Agent stopped")


def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def main() -> None:
    """Entry point"""
    # Настраиваем обработку сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info(f"Starting agent {AGENT_ID}")
    logger.info(f"Handler URL: {HANDLER_URL}")
    logger.info(f"Emulators: {EMULATOR_COUNT} (ports {EMULATOR_BASE_PORT} - {EMULATOR_BASE_PORT + 2 * (EMULATOR_COUNT - 1)})")
    
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
