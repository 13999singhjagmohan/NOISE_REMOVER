"""Desktop GUI (PySide6). All processing runs in a background QThread."""

from __future__ import annotations

import os
import sys
import threading
import time

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .media import Cancelled, JobOptions, default_output_path, denoise_file, find_tool

FORMAT_CHOICES = [("Same as input", None), ("WAV", ".wav"), ("FLAC", ".flac"),
                  ("MP3", ".mp3"), ("M4A (AAC)", ".m4a"), ("OGG", ".ogg"), ("Opus", ".opus")]
INPUT_FILTER = ("Audio / Video (*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.opus *.wma *.aiff *.aif "
                "*.amr *.ac3 *.mka *.caf *.wv *.mp4 *.m4v *.mov *.mkv *.webm *.avi *.wmv *.flv "
                "*.ts *.mts *.m2ts *.3gp *.mpg *.mpeg);;All files (*.*)")

COL_FILE, COL_STATUS, COL_PROGRESS = range(3)


def _fmt_time(sec: float) -> str:
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Worker(QObject):
    progress = Signal(int, float, str)     # row, fraction, stage
    finished_one = Signal(int, str)        # row, output path
    failed_one = Signal(int, str)          # row, error
    cancelled_one = Signal(int)
    all_done = Signal()

    def __init__(self, jobs, opts: JobOptions):
        super().__init__()
        self.jobs = jobs                   # list of (row, src, dst)
        self.opts = opts
        self.cancel = threading.Event()

    @Slot()
    def run(self):
        for row, src, dst in self.jobs:
            if self.cancel.is_set():
                self.cancelled_one.emit(row)
                continue
            try:
                denoise_file(src, dst, self.opts,
                             lambda f, stage, r=row: self.progress.emit(r, f, stage),
                             self.cancel)
                self.finished_one.emit(row, dst)
            except Cancelled:
                self.cancelled_one.emit(row)
            except Exception as e:  # keep going with the next file
                self.failed_one.emit(row, str(e) or e.__class__.__name__)
        self.all_done.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Noise Remover {__version__} (offline)")
        self.resize(900, 560)
        self.setAcceptDrops(True)
        self.settings = QSettings("NoiseRemover", "NoiseRemover")
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.files: list[str] = []
        self.started_at = {}

        central = QWidget()
        root = QVBoxLayout(central)

        hint = QLabel("Drag and drop audio or video files here, or click Add files. "
                      "Everything runs on this computer; nothing is uploaded.")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Progress"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_STATUS, 280)
        hdr.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 220)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.add_btn = QPushButton("Add files…")
        self.add_btn.clicked.connect(self.add_files_dialog)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn = QPushButton("Clear list")
        self.clear_btn.clicked.connect(self.clear_list)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        root.addLayout(row)

        # options
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Strength:"))
        self.strength = QSlider(Qt.Horizontal)
        self.strength.setRange(0, 100)
        self.strength.setValue(int(self.settings.value("strength", 70)))
        self.strength.setFixedWidth(160)
        self.strength_lbl = QLabel()
        self.strength.valueChanged.connect(self._update_strength_label)
        self._update_strength_label()
        opt.addWidget(self.strength)
        opt.addWidget(self.strength_lbl)
        opt.addSpacing(16)
        self.lowcut = QCheckBox("Remove hum / rumble below 70 Hz")
        self.lowcut.setChecked(self.settings.value("lowcut", "true") == "true")
        opt.addWidget(self.lowcut)
        opt.addSpacing(16)
        opt.addWidget(QLabel("Output format:"))
        self.fmt = QComboBox()
        for label, _ in FORMAT_CHOICES:
            self.fmt.addItem(label)
        self.fmt.setCurrentIndex(int(self.settings.value("fmt", 0)))
        self.fmt.setToolTip("'Same as input' keeps videos as videos (video stream copied, not re-encoded).")
        opt.addWidget(self.fmt)
        opt.addStretch(1)
        root.addLayout(opt)

        out = QHBoxLayout()
        out.addWidget(QLabel("Save to:"))
        self.out_dir = QLineEdit(self.settings.value("out_dir", ""))
        self.out_dir.setPlaceholderText("Same folder as each input file")
        out.addWidget(self.out_dir, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.pick_out_dir)
        out.addWidget(browse)
        root.addLayout(out)

        bottom = QHBoxLayout()
        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.overall.setFormat("Ready")
        self.overall.setTextVisible(True)
        bottom.addWidget(self.overall, 1)
        self.start_btn = QPushButton("Remove noise")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton("Open output folder")
        self.open_btn.clicked.connect(self.open_output_folder)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.open_btn)
        root.addLayout(bottom)

        self.setCentralWidget(central)
        self.last_output = ""

        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except Exception as e:
            QMessageBox.warning(self, "ffmpeg missing", str(e))

    # ------------------------------------------------------------- file list
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isdir(p):
                for dirpath, _, names in os.walk(p):
                    paths += [os.path.join(dirpath, n) for n in sorted(names)]
            elif p:
                paths.append(p)
        self.add_files(paths)

    def add_files_dialog(self):
        start = self.settings.value("last_in_dir", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose audio or video files", start, INPUT_FILTER)
        if paths:
            self.settings.setValue("last_in_dir", os.path.dirname(paths[0]))
            self.add_files(paths)

    def add_files(self, paths):
        if self.busy():
            return
        for p in paths:
            if p in self.files or not os.path.isfile(p):
                continue
            self.files.append(p)
            r = self.table.rowCount()
            self.table.insertRow(r)
            item = QTableWidgetItem(os.path.basename(p))
            item.setToolTip(p)
            self.table.setItem(r, COL_FILE, item)
            self.table.setItem(r, COL_STATUS, QTableWidgetItem("Queued"))
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setFormat("0%")
            self.table.setCellWidget(r, COL_PROGRESS, bar)

    def remove_selected(self):
        if self.busy():
            return
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)
            del self.files[r]

    def clear_list(self):
        if self.busy():
            return
        self.table.setRowCount(0)
        self.files.clear()

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _update_strength_label(self):
        v = self.strength.value()
        word = "gentle" if v < 35 else "balanced" if v < 75 else "strong"
        self.strength_lbl.setText(f"{v}% ({word})")

    # ------------------------------------------------------------ processing
    def busy(self) -> bool:
        return self.thread is not None

    def start(self):
        if self.busy():
            return
        if not self.files:
            QMessageBox.information(self, "No files", "Add at least one audio or video file.")
            return
        out_dir = self.out_dir.text().strip() or None
        out_ext = FORMAT_CHOICES[self.fmt.currentIndex()][1]
        jobs = []
        for r, src in enumerate(self.files):
            if self.table.item(r, COL_STATUS).text().startswith("Done"):
                continue
            jobs.append((r, src, default_output_path(src, out_dir, out_ext)))
            self._set_row(r, 0.0, "Waiting")
        if not jobs:
            QMessageBox.information(self, "Nothing to do", "All files are already done.")
            return

        self.settings.setValue("strength", self.strength.value())
        self.settings.setValue("lowcut", "true" if self.lowcut.isChecked() else "false")
        self.settings.setValue("fmt", self.fmt.currentIndex())
        self.settings.setValue("out_dir", self.out_dir.text())

        opts = JobOptions(strength=self.strength.value() / 100.0,
                          low_cut_hz=70.0 if self.lowcut.isChecked() else 0.0,
                          keep_video=out_ext is None)
        self.job_rows = [j[0] for j in jobs]
        self.row_frac = {r: 0.0 for r in self.job_rows}
        self.batch_start = time.monotonic()
        self.ok = self.failed = 0

        self.thread = QThread(self)
        self.worker = Worker(jobs, opts)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_one.connect(self.on_done)
        self.worker.failed_one.connect(self.on_failed)
        self.worker.cancelled_one.connect(self.on_cancelled)
        self.worker.all_done.connect(self.on_all_done)
        self._set_controls(True)
        self.thread.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.cancel_btn.setEnabled(False)
            self.overall.setFormat("Cancelling…")

    def _set_controls(self, running: bool):
        for w in (self.start_btn, self.add_btn, self.remove_btn, self.clear_btn,
                  self.strength, self.lowcut, self.fmt, self.out_dir):
            w.setEnabled(not running)
        self.cancel_btn.setEnabled(running)

    def _set_row(self, r: int, frac: float, status: str):
        self.table.item(r, COL_STATUS).setText(status)
        bar = self.table.cellWidget(r, COL_PROGRESS)
        bar.setValue(int(frac * 1000))
        bar.setFormat(f"{frac * 100:.0f}%")

    def _update_overall(self):
        n = len(self.job_rows)
        total = sum(self.row_frac.values()) / max(n, 1)
        self.overall.setValue(int(total * 1000))
        elapsed = time.monotonic() - self.batch_start
        eta = ""
        if 0.01 < total < 1:
            eta = f" · about {_fmt_time(elapsed * (1 - total) / total)} left"
        self.overall.setFormat(f"{total * 100:.1f}% · {_fmt_time(elapsed)} elapsed{eta}")

    @Slot(int, float, str)
    def on_progress(self, r, frac, stage):
        self.row_frac[r] = frac
        self._set_row(r, frac, stage)
        self._update_overall()

    @Slot(int, str)
    def on_done(self, r, path):
        self.ok += 1
        self.row_frac[r] = 1.0
        self.last_output = path
        self._set_row(r, 1.0, f"Done → {os.path.basename(path)}")
        self.table.item(r, COL_STATUS).setToolTip(path)
        self._update_overall()

    @Slot(int, str)
    def on_failed(self, r, msg):
        self.failed += 1
        self.row_frac[r] = 1.0
        self._set_row(r, 0.0, "Failed")
        self.table.item(r, COL_STATUS).setToolTip(msg)
        self.table.item(r, COL_STATUS).setText(f"Failed: {msg.splitlines()[0][:80] if msg else ''}")
        self._update_overall()

    @Slot(int)
    def on_cancelled(self, r):
        self.row_frac[r] = 0.0
        self._set_row(r, 0.0, "Cancelled")

    @Slot()
    def on_all_done(self):
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self._set_controls(False)
        elapsed = _fmt_time(time.monotonic() - self.batch_start)
        self.overall.setValue(1000 if self.ok else 0)
        self.overall.setFormat(f"Finished: {self.ok} done, {self.failed} failed · {elapsed}")

    def open_output_folder(self):
        folder = self.out_dir.text().strip() or (os.path.dirname(self.last_output) if self.last_output else "")
        if folder and os.path.isdir(folder):
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", folder])

    def closeEvent(self, e):
        if self.busy():
            if QMessageBox.question(self, "Still working",
                                    "Processing is still running. Cancel it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.worker.cancel.set()
            self.thread.quit()
            self.thread.wait(10000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Noise Remover")
    w = MainWindow()
    w.add_files(sys.argv[1:])
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
