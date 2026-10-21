"""
Предупреждения обработки: всё, что не удалось распознать/перевести, копится
здесь и показывается пользователю (статус, предпросмотр, лист в xlsx),
вместо того чтобы молча пропадать.
"""
import logging
import threading

logger = logging.getLogger(__name__)


class Report:
    def __init__(self, job_id: str = ""):
        self._items = []
        self._job_id = job_id
        self._lock = threading.Lock()  # пишут потоки распознавания/перевода

    def add(self, message: str):
        with self._lock:
            if message not in self._items:
                self._items.append(message)
            logger.info("[%s] %s", self._job_id, message)

    @property
    def items(self) -> list:
        with self._lock:
            return list(self._items)


def ranges(numbers) -> str:
    """[1,2,3,7,9,10] -> '1–3, 7, 9–10'."""
    nums = sorted(set(numbers))
    out, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start}–{prev}" if prev != start else f"{start}")
            start = prev = n
    return ", ".join(out)
