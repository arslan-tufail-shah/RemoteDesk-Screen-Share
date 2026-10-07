"""Recordings gallery: browse, rename, delete, preview and play back the
.mp4 files saved by Viewer's recording feature, plus a trim-and-save-copy
tool in the playback window.
"""

import os
import time
import cv2

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QScrollArea, QMessageBox, QInputDialog, QSlider, QStackedLayout,
    QSizePolicy, QFrame
)
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

RECORDINGS_DIR = "recordings"  # matches viewer.py's start_recording()
THUMBNAIL_DIR = os.path.join(RECORDINGS_DIR, ".thumbnails")
CARD_THUMB_SIZE = (240, 150)


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def get_video_metadata(path):
    """Returns (duration_seconds, fps, width, height, size_bytes, mtime)."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    duration = (frame_count / fps) if fps > 0 else 0
    try:
        size_bytes = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        size_bytes, mtime = 0, 0
    return duration, fps, width, height, size_bytes, mtime


def get_thumbnail_pixmap(path):
    """Reads (and caches to disk) the first frame of a video as a QPixmap
    sized for the gallery card. Cache is keyed by filename + mtime so a
    replaced/re-recorded file with the same name gets a fresh thumbnail."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    cache_name = f"{os.path.basename(path)}.{int(mtime)}.jpg"
    cache_path = os.path.join(THUMBNAIL_DIR, cache_name)

    if os.path.exists(cache_path):
        pixmap = QPixmap(cache_path)
        if not pixmap.isNull():
            return pixmap

    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return None

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, ch = frame_rgb.shape
    image = QImage(frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
    pixmap = QPixmap.fromImage(image.copy())
    try:
        pixmap.save(cache_path, "JPG", 80)
    except Exception:
        pass
    return pixmap


class RecordingCard(QWidget):
    """One gallery tile: thumbnail (hover = live muted preview), filename,
    metadata, and Rename/Delete actions. Clicking the tile (not a button)
    opens the file in a VideoPlayerWindow."""

    play_requested = pyqtSignal(str)
    renamed = pyqtSignal(str, str)   # old_path, new_path
    delete_requested = pyqtSignal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self._preview_player = None
        self._preview_video_widget = None
        self.setObjectName("recordingCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(CARD_THUMB_SIZE[0] + 24)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Thumbnail area - a stacked layout so a hover preview video can be
        # swapped in over the static image without changing card size.
        self.thumb_frame = QFrame()
        self.thumb_frame.setFixedSize(*CARD_THUMB_SIZE)
        self.thumb_frame.setObjectName("thumbFrame")
        self._thumb_stack = QStackedLayout(self.thumb_frame)
        self._thumb_stack.setContentsMargins(0, 0, 0, 0)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(*CARD_THUMB_SIZE)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setObjectName("thumbLabel")
        self._thumb_stack.addWidget(self.thumb_label)
        layout.addWidget(self.thumb_frame)

        self.title_label = QLabel()
        self.title_label.setObjectName("rowTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("rowSubtitle")
        layout.addWidget(self.subtitle_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.rename_button = QPushButton("Rename")
        self.rename_button.setObjectName("allowButton")
        self.rename_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rename_button.clicked.connect(self._on_rename_clicked)
        button_row.addWidget(self.rename_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("blockButton")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        button_row.addWidget(self.delete_button)
        layout.addLayout(button_row)

        self.refresh()

    def refresh(self):
        pixmap = get_thumbnail_pixmap(self.path)
        if pixmap is not None:
            self.thumb_label.setPixmap(pixmap.scaled(
                *CARD_THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            ))
        else:
            self.thumb_label.setPixmap(QPixmap())
            self.thumb_label.setText("No preview")

        name = os.path.basename(self.path)
        self.title_label.setText(name)

        duration, fps, width, height, size_bytes, mtime = get_video_metadata(self.path)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "Unknown date"
        self.subtitle_label.setText(
            f"{_format_duration(duration)}  •  {width}x{height}  •  {_format_size(size_bytes)}\n{when}"
        )

    # --- hover-to-preview -------------------------------------------------

    def enterEvent(self, event):
        self._start_preview()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._stop_preview()
        super().leaveEvent(event)

    def _start_preview(self):
        if self._preview_player is not None:
            return
        if not os.path.exists(self.path):
            return
        video_widget = QVideoWidget()
        video_widget.setFixedSize(*CARD_THUMB_SIZE)
        player = QMediaPlayer(self)
        audio_output = QAudioOutput(self)
        audio_output.setMuted(True)
        player.setAudioOutput(audio_output)
        player.setVideoOutput(video_widget)
        player.setSource(QUrl.fromLocalFile(os.path.abspath(self.path)))

        def _loop(status):
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                player.setPosition(0)
                player.play()

        player.mediaStatusChanged.connect(_loop)

        self._thumb_stack.addWidget(video_widget)
        self._thumb_stack.setCurrentWidget(video_widget)
        self._preview_player = player
        self._preview_video_widget = video_widget
        player.play()

    def _stop_preview(self):
        if self._preview_player is None:
            return
        try:
            self._preview_player.stop()
        except Exception:
            pass
        self._thumb_stack.setCurrentWidget(self.thumb_label)
        self._thumb_stack.removeWidget(self._preview_video_widget)
        self._preview_video_widget.deleteLater()
        self._preview_player.deleteLater()
        self._preview_player = None
        self._preview_video_widget = None

    # --- click / actions ----------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self.path)
        super().mousePressEvent(event)

    def _on_rename_clicked(self):
        directory = os.path.dirname(self.path)
        old_name = os.path.basename(self.path)
        base, ext = os.path.splitext(old_name)
        new_base, ok = QInputDialog.getText(self, "Rename recording", "New name:", text=base)
        if not ok or not new_base.strip():
            return
        new_name = new_base.strip() + ext
        new_path = os.path.join(directory, new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Rename failed", "A file with that name already exists.")
            return
        try:
            os.rename(self.path, new_path)
        except OSError as e:
            QMessageBox.warning(self, "Rename failed", str(e))
            return
        old_path = self.path
        self.path = new_path
        self.renamed.emit(old_path, new_path)

    def _on_delete_clicked(self):
        name = os.path.basename(self.path)
        confirm = QMessageBox.question(
            self, "Delete recording",
            f"Delete \"{name}\"? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(self.path)
        except OSError as e:
            QMessageBox.warning(self, "Delete failed", str(e))
            return
        self.delete_requested.emit(self.path)


class RecordingsPage(QWidget):
    """The 'Recordings' tab: a refreshable grid of RecordingCard tiles."""

    GRID_COLUMNS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards = {}
        self.players = []  # keep references to open VideoPlayerWindow instances
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        panel = QWidget()
        panel.setObjectName("panelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        panel_layout.setSpacing(12)

        panel_header = QHBoxLayout()
        panel_icon = QLabel("🎬")
        panel_icon.setObjectName("panelIcon")
        panel_icon.setFixedSize(44, 44)
        panel_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_header.addWidget(panel_icon)
        panel_text = QVBoxLayout()
        panel_text.setSpacing(0)
        panel_title = QLabel("Recordings")
        panel_title.setObjectName("panelTitle")
        panel_sub = QLabel("Browse, rename, delete, preview and play back saved session recordings.")
        panel_sub.setObjectName("panelSubtitle")
        panel_text.addWidget(panel_title)
        panel_text.addWidget(panel_sub)
        panel_header.addLayout(panel_text)
        panel_header.addStretch(1)

        self.count_label = QLabel("0 recordings")
        self.count_label.setObjectName("countBadge")
        panel_header.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignTop)

        self.refresh_button = QPushButton("⟳")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.setFixedSize(38, 38)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh)
        panel_header.addWidget(self.refresh_button)
        panel_layout.addLayout(panel_header)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setObjectName("recordingsScroll")
        self.grid_host = QWidget()
        self.grid_layout = QGridLayout(self.grid_host)
        self.grid_layout.setSpacing(14)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll_area.setWidget(self.grid_host)
        panel_layout.addWidget(self.scroll_area, 1)

        self.empty_state = self._build_empty_state()
        panel_layout.addWidget(self.empty_state)

        layout.addWidget(panel, 1)

        self.refresh()

    def _build_empty_state(self):
        box = QWidget()
        box.setObjectName("emptyState")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(20, 40, 20, 40)
        box_layout.setSpacing(6)
        box_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🎬")
        icon.setObjectName("emptyIcon")
        icon.setFixedSize(90, 90)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("No recordings yet")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(title)

        sub = QLabel("Recordings you save from a live viewer session will show up here.")
        sub.setObjectName("emptySubtitle")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box_layout.addWidget(sub)

        return box

    def refresh(self):
        for card in list(self.cards.values()):
            card._stop_preview()
            self.grid_layout.removeWidget(card)
            card.deleteLater()
        self.cards = {}

        if not os.path.isdir(RECORDINGS_DIR):
            paths = []
        else:
            paths = [
                os.path.join(RECORDINGS_DIR, f)
                for f in os.listdir(RECORDINGS_DIR)
                if f.lower().endswith((".mp4", ".avi", ".mov", ".mkv"))
            ]
        paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)

        for index, path in enumerate(paths):
            card = RecordingCard(path)
            card.play_requested.connect(self._open_player)
            card.renamed.connect(self._on_card_renamed)
            card.delete_requested.connect(self._on_card_deleted)
            row, col = divmod(index, self.GRID_COLUMNS)
            self.grid_layout.addWidget(card, row, col)
            self.cards[path] = card

        self.count_label.setText(f"{len(paths)} recording{'s' if len(paths) != 1 else ''}")
        self.empty_state.setVisible(len(paths) == 0)
        self.scroll_area.setVisible(len(paths) > 0)

    def _open_player(self, path):
        player_window = VideoPlayerWindow(path)
        player_window.trimmed_copy_saved.connect(lambda _p: self.refresh())
        self.players.append(player_window)
        player_window.destroyed.connect(
            lambda: self.players.remove(player_window) if player_window in self.players else None
        )
        player_window.show()

    def _on_card_renamed(self, old_path, new_path):
        self.refresh()

    def _on_card_deleted(self, path):
        self.refresh()


class VideoPlayerWindow(QWidget):
    """Standalone playback window with a trim tool: pick a start/end point
    from the current playback position, then save the trimmed range as a
    new file alongside the original (re-encoded via OpenCV, so it doesn't
    depend on an external ffmpeg install)."""

    trimmed_copy_saved = pyqtSignal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle(f"Playing - {os.path.basename(path)}")
        self.resize(880, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.duration_ms = 0
        self.trim_start_ms = 0
        self.trim_end_ms = None  # None until set or duration known

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(420)
        layout.addWidget(self.video_widget, 1)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.positionChanged.connect(self._on_position_changed)

        # Transport controls
        transport = QHBoxLayout()
        transport.setSpacing(10)
        self.play_button = QPushButton("▶ Play")
        self.play_button.setObjectName("viewButton")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(self._toggle_play)
        transport.addWidget(self.play_button)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.player.setPosition)
        transport.addWidget(self.position_slider, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("rowSubtitle")
        transport.addWidget(self.time_label)
        layout.addLayout(transport)

        # Trim controls
        trim_box = QWidget()
        trim_box.setObjectName("panelCard")
        trim_layout = QVBoxLayout(trim_box)
        trim_layout.setContentsMargins(14, 12, 14, 12)
        trim_layout.setSpacing(8)

        trim_title = QLabel("Trim")
        trim_title.setObjectName("panelTitle")
        trim_layout.addWidget(trim_title)

        trim_controls = QHBoxLayout()
        trim_controls.setSpacing(10)

        self.set_start_button = QPushButton("Set start ⏱")
        self.set_start_button.setObjectName("allowButton")
        self.set_start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_start_button.clicked.connect(self._set_trim_start)
        trim_controls.addWidget(self.set_start_button)

        self.set_end_button = QPushButton("Set end ⏱")
        self.set_end_button.setObjectName("allowButton")
        self.set_end_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_end_button.clicked.connect(self._set_trim_end)
        trim_controls.addWidget(self.set_end_button)

        self.trim_range_label = QLabel("Trim: full video")
        self.trim_range_label.setObjectName("rowSubtitle")
        trim_controls.addWidget(self.trim_range_label, 1)

        self.reset_trim_button = QPushButton("Reset")
        self.reset_trim_button.setObjectName("blockButton")
        self.reset_trim_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_trim_button.clicked.connect(self._reset_trim)
        trim_controls.addWidget(self.reset_trim_button)

        trim_layout.addLayout(trim_controls)

        self.save_button = QPushButton("💾 Save Trimmed Copy")
        self.save_button.setObjectName("viewButton")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self._save_trimmed_copy)
        trim_layout.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(trim_box)

        self.player.play()
        self.play_button.setText("⏸ Pause")

    # --- transport ------------------------------------------------------

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_button.setText("▶ Play")
        else:
            self.player.play()
            self.play_button.setText("⏸ Pause")

    def _on_duration_changed(self, duration_ms):
        self.duration_ms = duration_ms
        self.position_slider.setRange(0, duration_ms)
        self._update_time_label()

    def _on_position_changed(self, position_ms):
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position_ms)
        self._update_time_label()

    def _update_time_label(self):
        pos = _format_duration(self.player.position() / 1000)
        dur = _format_duration(self.duration_ms / 1000)
        self.time_label.setText(f"{pos} / {dur}")

    # --- trim -------------------------------------------------------------

    def _set_trim_start(self):
        self.trim_start_ms = self.player.position()
        if self.trim_end_ms is not None and self.trim_end_ms <= self.trim_start_ms:
            self.trim_end_ms = None
        self._update_trim_label()

    def _set_trim_end(self):
        end = self.player.position()
        if end <= self.trim_start_ms:
            QMessageBox.warning(self, "Invalid trim range", "End point must be after the start point.")
            return
        self.trim_end_ms = end
        self._update_trim_label()

    def _reset_trim(self):
        self.trim_start_ms = 0
        self.trim_end_ms = None
        self._update_trim_label()

    def _update_trim_label(self):
        start_s = self.trim_start_ms / 1000
        end_s = (self.trim_end_ms if self.trim_end_ms is not None else self.duration_ms) / 1000
        if self.trim_start_ms == 0 and self.trim_end_ms is None:
            self.trim_range_label.setText("Trim: full video")
        else:
            self.trim_range_label.setText(
                f"Trim: {_format_duration(start_s)} → {_format_duration(end_s)} "
                f"({_format_duration(max(0, end_s - start_s))})"
            )

    def _save_trimmed_copy(self):
        end_ms = self.trim_end_ms if self.trim_end_ms is not None else self.duration_ms
        if end_ms <= self.trim_start_ms:
            QMessageBox.warning(self, "Invalid trim range", "End point must be after the start point.")
            return

        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.player.pause()

        directory = os.path.dirname(self.path)
        base, ext = os.path.splitext(os.path.basename(self.path))
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(directory, f"{base}_trimmed_{timestamp}.mp4")

        ok = self._write_trimmed_file(self.path, out_path, self.trim_start_ms, end_ms)
        if ok:
            QMessageBox.information(self, "Saved", f"Trimmed copy saved as:\n{os.path.basename(out_path)}")
            self.trimmed_copy_saved.emit(out_path)
        else:
            QMessageBox.warning(self, "Save failed", "Could not save the trimmed copy.")

        if was_playing:
            self.player.play()

    def _write_trimmed_file(self, src_path, out_path, start_ms, end_ms):
        cap = cv2.VideoCapture(src_path)
        if not cap.isOpened():
            return False
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
        if not writer.isOpened():
            cap.release()
            return False

        try:
            while True:
                pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                if pos_ms >= end_ms:
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
        finally:
            cap.release()
            writer.release()

        return os.path.exists(out_path) and os.path.getsize(out_path) > 0

    def closeEvent(self, event):
        try:
            self.player.stop()
        except Exception:
            pass
        event.accept()
