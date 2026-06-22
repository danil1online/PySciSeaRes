# Проработка проблемы управления памятью

## Контекст

**Железо:**
- CPU: 2 ядра
- RAM: 16 ГБ
- GPU: Nvidia GTX 1060 3 ГБ
- ОЗУ других сервисов: ~7 ГБ
- Доступно для LLM: ~9 ГБ

**llama.cpp server:**
```
llama-server -m Qwen3.5-2B-Q4_K_M.gguf \
  -ngl 99 -c 32768 --ctx-checkpoints 4 \
  --chat-template-kwargs '{"enable_thinking": false}' \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --host 0.0.0.0 --port 8080
```

**Проблема:** За 7-8 часов работы llama.cpp потребляет всю доступную RAM (~9 ГБ) из-за кэша KV.

---

## Анализ проблемы

### Почему llama.cpp потребляет много памяти

1. **KV Cache** — основная причина. При `-c 32768` и `--cache-type-k/q8_0`:
   - Память KV = n_layers × n_ctx × n_heads × head_dim × block_size
   - Qwen3.5-2B: ~28 layers, 32 heads, 128 dim = ~28 × 32768 × 32 × 128 × 2 bytes ≈ **7.6 ГБ**
   - Это теоретический максимум, но llama.cpp использует chunked cache (через `--ctx-checkpoints 4`)

2. **Chunked KV cache** (`--ctx-checkpoints 4`):
   - Разбивает контекст на 4 чанка
   - Каждый чанк: ~7.6 / 4 ≈ **1.9 ГБ**
   - Но при длительной работе кэш может фрагментироваться и расти

3. **Model weights** (Q4_K_M):
   - ~2B параметров × ~0.55 bytes/param ≈ **1.1 ГБ**
   - Загружены в RAM + GPU (ngl=99 означает почти весь модель на GPU)

4. **Output buffer + activations**:
   - ~200-500 МБ в пике при генерации

**Итого:** 1.1 (weights) + 1.9×4 (chunked cache) + 0.5 (activations) ≈ **9 ГБ** — совпадает с наблюдаемым потреблением.

---

## Варианты решения

### Вариант A: Периодический перезапуск llama.cpp сервера (рекомендуется)

**Принцип:** Мониторить свободную RAM и перезапускать сервер при критическом уровне.

**Реализация:**

1. **Отдельный процесс-монитор** (systemd service или cron job):
   ```bash
   # /usr/local/bin/llama-monitor.sh
   #!/bin/bash
   while true; do
       FREE_MEM=$(free -m | awk '/^Mem:/{print $7}')
       if [ $FREE_MEM -lt 2048 ]; then
           echo "$(date): Low memory $FREE_MEM MB, restarting llama-server..."
           pkill -f llama-server
           sleep 2
           /path/to/llama-server ... &
       fi
       sleep 30
   done
   ```

2. **Встроенный монитор в daily_sync.py** (проще, но менее надёжно):
   ```python
   # В daily_sync.py, перед каждым LLM-запросом:
   import psutil
   mem = psutil.virtual_memory()
   if mem.available < 2 * 1024:  # < 2 ГБ свободно
       subprocess.run(["systemctl", "restart", "llama-server"])
       time.sleep(5)  # ждём пока сервер поднимется
   ```

**Плюсы:**
- Простая реализация
- Полностью сбрасывает KV cache
- Не требует изменения llama.cpp

**Минусы:**
- Перезапуск = 30-90 сек простоя (cold start)
- Прерывает текущие запросы
- Нужно обрабатывать падения сервера

---

### Вариант B: Уменьшить размер контекста

**Принцип:** Меньше контекст = меньше KV cache.

**Изменить параметры запуска:**
```bash
# Было:
-c 32768 --ctx-checkpoints 4

# Стало (агрессивно):
-c 8192 --ctx-checkpoints 2

# Или умеренно:
-c 16384 --ctx-checkpoints 4
```

**Расчёт памяти для -c 8192:**
- KV cache: ~7.6 / 4 ≈ **1.9 ГБ** (вместо 7.6 ГБ)
- Итого: ~3.5 ГБ вместо ~9 ГБ

**Плюсы:**
- Не требует перезапусков
- Мгновенный эффект

**Минусы:**
- Лимит контекста 8K может не хватать для некоторых авторефератов
- Qwen3.5-2B хорошо работает с контекстом до 32K, но 8K — приемлемый компромисс

---

### Вариант C: Использовать GPU кэширование (if supported)

**Принцип:** Перенести KV cache на GPU, разгрузить RAM.

**Параметры:**
```bash
# Попытаться удержать KV cache на GPU
--gpu-layers 99 \
--tensor-split 0.9
```

**Проблема:** GTX 1060 3GB слишком мала для KV cache даже при Q4.

**Альтернатива:** Использовать `--mlock` и `--low-vram` для оптимизации использования GPU.

---

### Вариант D: Чанкирование запросов (в приложении)

**Принцип:** Разбивать большие запросы на части, чтобы не перегружать KV cache.

**Реализация в `extractors/publications.py`:**
```python
# Вместо отправки всех 12000 символов за раз:
CHUNK_SIZE = 4000
chunks = [text[i:i+CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
results = []
for chunk in chunks:
    result = call_llm(chunk)
    results.append(result)
# Merge results
```

**Плюсы:**
- Меньше пиковое потребление памяти
- Не влияет на llama.cpp

**Минусы:**
- Больше LLM-запросов = больше времени
- Потенциально хуже качество (потеря контекста между чанками)

---

### Вариант E: Комбинированный подход (наиболее надёжный)

**Рекомендую следующий план:**

1. **Снизить контекст до 16384** (компромисс между качеством и памятью):
   ```bash
   -c 16384 --ctx-checkpoints 4
   ```

2. **Добавить мониторинг RAM в daily_sync.py**:
   ```python
   # В check_memory():
   import psutil
   total_mem = psutil.virtual_memory().total / (1024*1024)  # 16384 MB
   available_mem = psutil.virtual_memory().available / (1024*1024)
   
   # Если свободно < 3 ГБ — принудительный перезапуск llama.cpp
   if available_mem < 3072:
       logger.warning(f"Low RAM: {available_mem:.0f} MB, restarting llama-server")
       subprocess.run(["pkill", "-f", "llama-server"])
       time.sleep(3)
       subprocess.Popen(["llama-server", ...])
       time.sleep(15)  # wait for cold start
   ```

3. **Настроить systemd для автоперезпуска llama.cpp**:
   ```ini
   # /etc/systemd/system/llama-server.service
   [Service]
   Restart=on-failure
   RestartSec=5
   # Ограничить память процесса (мягкий лимит)
   MemoryMax=8G
   ```

4. **Оптимизировать GC в daily_sync.py**:
   ```python
   # После каждого объявления:
   gc.collect()
   gc.collect()
   gc.collect()
   # + del для больших переменных
   del pdf_full_text
   del struct_pubs
   del email_results
   ```

---

## Рекомендуемая конфигурация запуска

```bash
# Оптимально для 16GB RAM + 7GB other services
/home/user/llama.cpp/build/bin/llama-server \
  -m /home/user/llama.cpp/Qwen3.5-2B-Q4_K_M.gguf \
  -ngl 99 \
  -c 16384 \
  --ctx-checkpoints 4 \
  --chat-template-kwargs '{"enable_thinking": false}' \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --batch-size 512 \
  --threads 2 \
  --host 0.0.0.0 \
  --port 8080
```

**Ожидаемое потребление:**
- Model weights: ~1.1 ГБ
- KV cache (max): ~3.8 ГБ
- Activations: ~0.5 ГБ
- **Итого пик: ~5.5 ГБ** (вместо ~9 ГБ)

С 9 ГБ доступной памяти — запас ~3.5 ГБ для Python-процесса и системы.

---

## Дополнительные рекомендации

1. **Использовать `--cache-reuse 8`** (если поддерживается версией llama.cpp) — кэширует чанки KV, уменьшает дублирование.

2. **Переключиться на Qwen3.5-1.5B** если 2B всё ещё слишком тяжёлый — модель почти сопоставима по качеству для задач извлечения данных, но потребляет на ~30% меньше памяти.

3. **Использовать `--no-mmap`** если система начинает свопить — предотвращает маппинг модели в swap.

4. **Запускать daily_sync в ночное время** когда система меньше нагружена.

5. **Мониторинг через `htop`/`vmstat`** для выявления паттернов потребления памяти.
