from __future__ import annotations

import time

import numpy
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class OscilloscopeTrace(QWidget):
    def __init__(self, trace_color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._trace_color = QColor(trace_color)
        self._samples = numpy.zeros(256, dtype=numpy.float32)
        self._last_update_monotonic = 0.0
        self.setMinimumHeight(54)

    def set_audio_chunk(self, chunk: bytes, sample_rate: int, timestamp: float) -> None:
        del sample_rate
        if not chunk or (time.monotonic() - timestamp) > 0.18:
            self._samples *= 0.82
            self.update()
            return

        samples = numpy.frombuffer(chunk, dtype=numpy.int16).astype(numpy.float32)
        if samples.size < 8:
            self._samples *= 0.82
            self.update()
            return

        target_count = self._samples.size
        if samples.size >= target_count:
            indices = numpy.linspace(0, samples.size - 1, target_count).astype(numpy.int32)
            sampled = samples[indices]
        else:
            sampled = numpy.interp(
                numpy.linspace(0, samples.size - 1, target_count),
                numpy.arange(samples.size),
                samples,
            )

        normalized = numpy.clip(sampled / 32768.0, -1.0, 1.0)
        self._samples = (self._samples * 0.35) + (normalized * 0.65)
        self._last_update_monotonic = time.monotonic()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(rect, QColor("#10151b"))
        painter.setPen(QPen(QColor("#2a3440"), 1))
        painter.drawRoundedRect(rect, 7, 7)

        center_y = rect.center().y()
        painter.setPen(QPen(QColor("#22303d"), 1))
        painter.drawLine(rect.left() + 6, center_y, rect.right() - 6, center_y)

        inner = rect.adjusted(8, 8, -8, -8)
        if inner.width() <= 0 or inner.height() <= 0:
            return

        x_positions = numpy.linspace(inner.left(), inner.right(), self._samples.size)
        amplitude = inner.height() * 0.48

        path = QPainterPath()
        path.moveTo(float(x_positions[0]), center_y - float(self._samples[0] * amplitude))
        for x, sample in zip(x_positions[1:], self._samples[1:]):
            path.lineTo(float(x), center_y - float(sample * amplitude))

        painter.setPen(QPen(self._trace_color, 2.0))
        painter.drawPath(path)
