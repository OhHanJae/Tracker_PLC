"""PySide6 TCP configuration client for the XGT gateway."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

try:
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
except ImportError as exc:  # Friendly error when launched without client dependencies.
    raise SystemExit("PySide6 is not installed. Run run_config_client.bat or run_config_client.sh first.") from exc


class GatewayConnection(QtCore.QObject):
    connected = QtCore.Signal()
    disconnected = QtCore.Signal()
    response = QtCore.Signal(dict)
    failed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.socket = QtNetwork.QTcpSocket(self)
        self.socket.connected.connect(self.connected.emit)
        self.socket.disconnected.connect(self.disconnected.emit)
        self.socket.readyRead.connect(self._read_ready)
        self.socket.errorOccurred.connect(self._socket_error)
        self._buffer = bytearray()

    def open(self, host: str, port: int) -> None:
        self._buffer.clear()
        self.socket.abort()
        self.socket.connectToHost(host, port)

    def close(self) -> None:
        self.socket.disconnectFromHost()

    def send(self, command: str, params: dict[str, Any] | None = None) -> str:
        if self.socket.state() != QtNetwork.QAbstractSocket.SocketState.ConnectedState:
            raise RuntimeError("TCP 설정 서버에 연결되지 않았습니다.")
        request_id = uuid.uuid4().hex
        value = {"id": request_id, "command": command, "params": params or {}}
        self.socket.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        return request_id

    @QtCore.Slot()
    def _read_ready(self) -> None:
        self._buffer.extend(bytes(self.socket.readAll()))
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                return
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            try:
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict):
                    self.response.emit(value)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.failed.emit(f"응답 JSON 해석 실패: {exc}")

    @QtCore.Slot(QtNetwork.QAbstractSocket.SocketError)
    def _socket_error(self, _error: QtNetwork.QAbstractSocket.SocketError) -> None:
        self.failed.emit(self.socket.errorString())


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("XGT Gateway 설정")
        self.resize(1500, 900)
        self.connection = GatewayConnection()
        self.pending: dict[str, str] = {}
        self.current_web_enabled = False
        self.settings = QtCore.QSettings("XGTGateway", "ConfigClient")

        # 진단 테이블 갱신/편집 상태입니다.
        # 500 ms 연속 읽기 중 불필요한 전체 테이블 재생성을 막기 위해 마지막 데이터를 캐시합니다.
        self._table_cache: dict[int, tuple[bytes, str]] = {}
        self._table_refreshing = False
        self._setting_tx_hex = False
        self._tx_dirty = False

        self._build_ui()

        # Gateway 상태 조회 타이머
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.setInterval(1000)
        self.status_timer.timeout.connect(self._request_status)

        # 공유 메모리 진단 연속 읽기 타이머.
        # RX/TX 중 하나라도 연속 읽기가 켜져 있으면 500 ms마다 갱신합니다.
        self.diagnostic_timer = QtCore.QTimer(self)
        self.diagnostic_timer.setInterval(500)
        self.diagnostic_timer.timeout.connect(self._refresh_diagnostics)

        self._wire_events()

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(root)

        connection_box = QtWidgets.QGroupBox("Gateway TCP 연결")
        connection_layout = QtWidgets.QHBoxLayout(connection_box)
        self.host = QtWidgets.QLineEdit(str(self.settings.value("connection/host", "")))
        self.host.setPlaceholderText("Gateway IP")
        self.control_port = self._spin(1, 65535, 8765)
        self.control_port.setValue(self._saved_int("connection/port", 8765))
        self.connect_button = QtWidgets.QPushButton("연결")
        self.connection_label = QtWidgets.QLabel("● 연결 안 됨")
        self.connection_label.setStyleSheet("color:#d9534f")
        connection_layout.addWidget(QtWidgets.QLabel("Host")); connection_layout.addWidget(self.host, 2)
        connection_layout.addWidget(QtWidgets.QLabel("Port")); connection_layout.addWidget(self.control_port)
        connection_layout.addWidget(self.connect_button); connection_layout.addWidget(self.connection_label)
        outer.addWidget(connection_box)

        status_box = QtWidgets.QGroupBox("실시간 상태")
        status_layout = QtWidgets.QGridLayout(status_box)
        self.plc_state = QtWidgets.QLabel("-")
        self.plc_endpoint = QtWidgets.QLabel("-")
        self.reads = QtWidgets.QLabel("0")
        self.writes = QtWidgets.QLabel("0")
        self.last_error = QtWidgets.QLabel("없음")
        self.last_error.setWordWrap(True)
        for row, (title, widget) in enumerate([
            ("PLC 상태", self.plc_state), ("PLC 주소", self.plc_endpoint),
            ("읽기 횟수", self.reads), ("쓰기 횟수", self.writes), ("마지막 오류", self.last_error),
        ]):
            status_layout.addWidget(QtWidgets.QLabel(title), row // 3 * 2, (row % 3) * 2)
            status_layout.addWidget(widget, row // 3 * 2, (row % 3) * 2 + 1)
        self.reconnect_button = QtWidgets.QPushButton("PLC 즉시 재연결")
        self.plc_start_button = QtWidgets.QPushButton("PLC 통신 켜기")
        self.plc_stop_button = QtWidgets.QPushButton("PLC 통신 끄기")
        self.web_start_button = QtWidgets.QPushButton("웹 서버 켜기")
        self.web_stop_button = QtWidgets.QPushButton("웹 서버 끄기")
        button_row = QtWidgets.QHBoxLayout()
        for button in (self.reconnect_button, self.plc_start_button, self.plc_stop_button, self.web_start_button, self.web_stop_button):
            button_row.addWidget(button)
        button_row.addStretch()
        status_layout.addLayout(button_row, 4, 0, 1, 6)
        outer.addWidget(status_box)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_plc_tab(), "PLC / 메모리 설정")
        tabs.addTab(self._build_diagnostics_tab(), "공유 메모리 진단")
        outer.addWidget(tabs, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.message = QtWidgets.QLabel("Gateway에 연결하세요.")
        self.message.setWordWrap(True)
        self.reload_button = QtWidgets.QPushButton("설정 다시 불러오기")
        self.save_button = QtWidgets.QPushButton("검증 후 저장 · 즉시 적용")
        self.save_button.setDefault(True)
        bottom.addWidget(self.message, 1); bottom.addWidget(self.reload_button); bottom.addWidget(self.save_button)
        outer.addLayout(bottom)
        self.setCentralWidget(root)

    def _build_plc_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        plc_group = QtWidgets.QGroupBox("PLC 연결 / 재시도")
        form = QtWidgets.QFormLayout(plc_group)
        self.plc_enabled = QtWidgets.QCheckBox("PLC 통신 사용")
        self.plc_host = QtWidgets.QLineEdit()
        self.plc_port = self._spin(1, 65535, 2004)
        self.connect_timeout = self._double_spin(0.1, 120.0, 3.0)
        self.io_timeout = self._double_spin(0.1, 120.0, 2.0)
        self.retry_initial = self._spin(50, 600000, 500)
        self.retry_max = self._spin(50, 3600000, 10000)
        self.retry_multiplier = self._double_spin(1.0, 10.0, 2.0)
        self.cpu_info = QtWidgets.QLineEdit("0xA0")
        self.slot = self._spin(0, 15, 0); self.base = self._spin(0, 15, 0)
        self.use_bcc = QtWidgets.QCheckBox("BCC 계산 사용")
        form.addRow(self.plc_enabled); form.addRow("PLC IP / Host", self.plc_host); form.addRow("PLC TCP Port", self.plc_port)
        form.addRow("연결 제한시간 (s)", self.connect_timeout); form.addRow("응답 제한시간 (s)", self.io_timeout)
        form.addRow("최초 재시도 (ms)", self.retry_initial); form.addRow("최대 재시도 (ms)", self.retry_max); form.addRow("재시도 배율", self.retry_multiplier)
        advanced = QtWidgets.QHBoxLayout(); advanced.addWidget(QtWidgets.QLabel("CPU Info")); advanced.addWidget(self.cpu_info); advanced.addWidget(QtWidgets.QLabel("Slot")); advanced.addWidget(self.slot); advanced.addWidget(QtWidgets.QLabel("Base")); advanced.addWidget(self.base); advanced.addWidget(self.use_bcc)
        form.addRow("XGT 헤더", advanced)

        rw_group = QtWidgets.QGroupBox("연속 BYTE 읽기 / 쓰기")
        rw = QtWidgets.QGridLayout(rw_group)
        self.read_enabled = QtWidgets.QCheckBox("읽기 사용"); self.read_address = QtWidgets.QLineEdit(); self.read_address.setPlaceholderText("D0 또는 %DB0")
        self.read_count = self._spin(1, 1400, 100); self.read_interval = self._spin(10, 3600000, 100)
        self.write_enabled = QtWidgets.QCheckBox("쓰기 사용"); self.write_address = QtWidgets.QLineEdit(); self.write_address.setPlaceholderText("D100 또는 %DB200")
        self.write_count = self._spin(1, 1400, 100); self.write_interval = self._spin(10, 3600000, 100)
        self.write_mode = QtWidgets.QComboBox(); self.write_mode.addItem("값 커밋 시", "on_change"); self.write_mode.addItem("주기 반복", "cyclic")
        self.write_startup = QtWidgets.QCheckBox("시작 즉시 현재 공유 메모리 값을 PLC에 쓰기")
        rows = [
            ("", self.read_enabled, "", self.write_enabled),
            ("읽기 시작 주소", self.read_address, "쓰기 시작 주소", self.write_address),
            ("읽기 크기 (byte)", self.read_count, "쓰기 크기 (byte)", self.write_count),
            ("읽기 주기 (ms)", self.read_interval, "쓰기 확인/주기 (ms)", self.write_interval),
            ("", QtWidgets.QLabel("100 byte = 50 word"), "쓰기 모드", self.write_mode),
        ]
        for row, values in enumerate(rows):
            rw.addWidget(QtWidgets.QLabel(values[0]), row, 0); rw.addWidget(values[1], row, 1)
            rw.addWidget(QtWidgets.QLabel(values[2]), row, 2); rw.addWidget(values[3], row, 3)
        rw.addWidget(self.write_startup, len(rows), 2, 1, 2)

        lower = QtWidgets.QHBoxLayout()
        shm_group = QtWidgets.QGroupBox("공유 메모리")
        shm = QtWidgets.QFormLayout(shm_group)
        self.shm_name = QtWidgets.QLineEdit(); self.shm_size = self._spin(128, 1048576, 512)
        self.shm_read_offset = self._spin(64, 1048575, 64); self.shm_write_offset = self._spin(64, 1048575, 256)
        self.shm_unlink = QtWidgets.QCheckBox("Gateway 종료 시 제거")
        shm.addRow("이름", self.shm_name); shm.addRow("전체 크기", self.shm_size); shm.addRow("읽기 Offset", self.shm_read_offset); shm.addRow("쓰기 Offset", self.shm_write_offset); shm.addRow(self.shm_unlink)
        web_group = QtWidgets.QGroupBox("웹 설정 서버")
        web = QtWidgets.QFormLayout(web_group)
        self.web_enabled = QtWidgets.QCheckBox("웹 서버 사용")
        self.web_host = QtWidgets.QLineEdit(); self.web_port = self._spin(1, 65535, 8080)
        web.addRow(self.web_enabled); web.addRow("Bind 주소", self.web_host); web.addRow("HTTP Port", self.web_port)
        lower.addWidget(shm_group); lower.addWidget(web_group)
        layout.addWidget(plc_group); layout.addWidget(rw_group); layout.addLayout(lower); layout.addStretch()
        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        wrapper = QtWidgets.QWidget(); wrapper_layout = QtWidgets.QVBoxLayout(wrapper); wrapper_layout.setContentsMargins(0,0,0,0); wrapper_layout.addWidget(scroll)
        return wrapper

    def _build_diagnostics_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        # Word 해석 Endian 선택. 기본값은 Big-Endian입니다.
        option_row = QtWidgets.QHBoxLayout()
        option_row.addWidget(QtWidgets.QLabel("Word Byte Order"))
        self.big_endian_radio = QtWidgets.QRadioButton("Big Endian")
        self.little_endian_radio = QtWidgets.QRadioButton("Little Endian")
        self.big_endian_radio.setChecked(True)
        self.endian_group = QtWidgets.QButtonGroup(self)
        self.endian_group.addButton(self.big_endian_radio)
        self.endian_group.addButton(self.little_endian_radio)
        option_row.addWidget(self.big_endian_radio)
        option_row.addWidget(self.little_endian_radio)
        option_row.addSpacing(20)
        bit_hint = QtWidgets.QLabel("Bit: 0 = LSB, F = MSB")
        bit_hint.setToolTip("각 Word의 16개 비트를 PLC 표기처럼 0~F로 표시합니다.")
        option_row.addWidget(bit_hint)
        option_row.addStretch()
        layout.addLayout(option_row)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # -----------------------------
        # 읽기 공유 메모리(RX) 진단 영역
        # -----------------------------
        rx_group = QtWidgets.QGroupBox("읽기 영역 (RX)")
        rx_layout = QtWidgets.QVBoxLayout(rx_group)

        rx_controls = QtWidgets.QHBoxLayout()
        self.read_rx_button = QtWidgets.QPushButton("읽기 영역 확인")
        self.continuous_rx_checkbox = QtWidgets.QCheckBox("연속 읽기 (500 ms)")
        rx_controls.addWidget(self.read_rx_button)
        rx_controls.addWidget(self.continuous_rx_checkbox)
        rx_controls.addStretch()
        rx_layout.addLayout(rx_controls)

        self.rx_hex = QtWidgets.QPlainTextEdit()
        self.rx_hex.setReadOnly(True)
        self.rx_hex.setMaximumHeight(110)
        self.rx_hex.setPlaceholderText("PLC에서 읽은 공유 메모리 HEX 데이터")
        rx_layout.addWidget(self.rx_hex)

        self.rx_table = self._create_word_table(editable=False)
        rx_layout.addWidget(self.rx_table, 1)

        # -----------------------------
        # 쓰기 공유 메모리(TX) 진단 영역
        # -----------------------------
        tx_group = QtWidgets.QGroupBox("쓰기 영역 (TX)")
        tx_layout = QtWidgets.QVBoxLayout(tx_group)

        tx_controls = QtWidgets.QHBoxLayout()
        self.read_tx_button = QtWidgets.QPushButton("쓰기 영역 확인")
        self.continuous_tx_checkbox = QtWidgets.QCheckBox("연속 읽기 (500 ms)")
        self.commit_tx_button = QtWidgets.QPushButton("HEX를 쓰기 영역에 커밋")
        tx_controls.addWidget(self.read_tx_button)
        tx_controls.addWidget(self.continuous_tx_checkbox)
        tx_controls.addWidget(self.commit_tx_button)
        tx_controls.addStretch()
        tx_layout.addLayout(tx_controls)

        self.tx_hex = QtWidgets.QPlainTextEdit()
        self.tx_hex.setMaximumHeight(110)
        self.tx_hex.setPlaceholderText("설정된 쓰기 길이만큼 HEX 입력")
        tx_layout.addWidget(self.tx_hex)

        # TX 표는 HEX/DEC Word 값을 직접 수정할 수 있습니다.
        self.tx_table = self._create_word_table(editable=True)
        tx_layout.addWidget(self.tx_table, 1)

        split.addWidget(rx_group)
        split.addWidget(tx_group)
        split.setSizes([750, 750])
        layout.addWidget(split, 1)

        note = QtWidgets.QLabel(
            "표는 2 byte = 1 Word 기준입니다. Big/Little Endian 선택에 따라 ASCII / HEX / DEC / Bit 0~F를 해석합니다. "
            "Bit 0은 LSB, Bit F는 MSB입니다. "
            "TX는 먼저 '쓰기 영역 확인'으로 현재 값을 읽은 뒤 HEX 또는 DEC 셀을 더블클릭하여 값을 수정할 수 있습니다. "
            "수정하면 TX 연속 읽기는 자동으로 꺼져 원격 값이 편집 중인 값을 덮어쓰지 않습니다. "
            "수정 후 'HEX를 쓰기 영역에 커밋'을 누르면 변경한 Word가 공유 메모리에 반영됩니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _wire_events(self) -> None:
        self.connect_button.clicked.connect(self._toggle_connection)
        self.connection.connected.connect(self._connected)
        self.connection.disconnected.connect(self._disconnected)
        self.connection.response.connect(self._response)
        self.connection.failed.connect(self._show_error)
        self.reload_button.clicked.connect(self._request_config)
        self.save_button.clicked.connect(self._save)
        self.reconnect_button.clicked.connect(lambda: self._send("reconnect_plc", {}, "reconnect"))
        self.plc_start_button.clicked.connect(lambda: self._send("set_plc_enabled", {"enabled": True}, "plc_toggle"))
        self.plc_stop_button.clicked.connect(lambda: self._send("set_plc_enabled", {"enabled": False}, "plc_toggle"))
        self.web_start_button.clicked.connect(lambda: self._send("set_web_enabled", {"enabled": True}, "web_toggle"))
        self.web_stop_button.clicked.connect(lambda: self._send("set_web_enabled", {"enabled": False}, "web_toggle"))
        self.read_rx_button.clicked.connect(lambda: self._request_shared_memory("read", "read_rx"))
        self.read_tx_button.clicked.connect(self._manual_read_tx)
        self.commit_tx_button.clicked.connect(self._commit_tx)

        # 진단 표시/주기 갱신 이벤트
        self.continuous_rx_checkbox.toggled.connect(self._update_diagnostic_timer)
        self.continuous_tx_checkbox.toggled.connect(self._update_diagnostic_timer)
        self.tx_hex.textChanged.connect(self._on_tx_hex_changed)
        self.tx_table.itemChanged.connect(self._on_tx_table_item_changed)
        self.big_endian_radio.toggled.connect(self._endianness_changed)
        self.little_endian_radio.toggled.connect(self._endianness_changed)

    def _toggle_connection(self) -> None:
        if self.connection.socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState:
            self.connection.close()
            return
        if not self.host.text().strip():
            self._show_error("Gateway IP를 입력하세요.")
            return
        self.message.setText("TCP 설정 서버에 연결하는 중...")
        self.connection.open(self.host.text().strip(), self.control_port.value())

    @QtCore.Slot()
    def _connected(self) -> None:
        self.connection_label.setText("● 연결됨"); self.connection_label.setStyleSheet("color:#2fbf8f")
        self.connect_button.setText("연결 끊기"); self.message.setText("연결되었습니다.")
        self._save_connection_settings()
        self._request_config(); self._request_status(); self.status_timer.start()

        # 연결 전에 연속 읽기를 체크해 둔 경우에도 바로 동작하게 합니다.
        self._update_diagnostic_timer()

    @QtCore.Slot()
    def _disconnected(self) -> None:
        self.connection_label.setText("● 연결 안 됨"); self.connection_label.setStyleSheet("color:#d9534f")
        self.connect_button.setText("연결"); self.status_timer.stop(); self.diagnostic_timer.stop(); self.pending.clear()

    def _send(self, command: str, params: dict[str, Any], operation: str) -> None:
        try:
            request_id = self.connection.send(command, params)
            self.pending[request_id] = operation
        except Exception as exc:
            self._show_error(str(exc))

    def _request_config(self) -> None:
        self._send("get_config", {}, "get_config")

    def _request_status(self) -> None:
        if "get_status" not in self.pending.values():
            self._send("get_status", {}, "get_status")

    def _request_shared_memory(self, area: str, operation: str) -> None:
        """공유 메모리 진단 요청을 영역별로 중복 전송하지 않습니다."""
        pending_operations = self.pending.values()
        if area == "read" and any(op == "read_rx" for op in pending_operations):
            return
        if area == "write" and any(op.startswith("read_tx") for op in pending_operations):
            return
        self._send("read_shared_memory", {"area": area}, operation)

    @QtCore.Slot()
    def _manual_read_tx(self) -> None:
        """사용자가 명시적으로 TX를 다시 읽으면 로컬 편집 내용을 버리고 현재 공유메모리 값을 다시 가져옵니다."""
        self._tx_dirty = False
        self._request_shared_memory("write", "read_tx_manual")

    @QtCore.Slot()
    def _commit_tx(self) -> None:
        """현재 TX HEX 버퍼를 공유 메모리에 커밋합니다."""
        data = self._hex_text_to_bytes(self.tx_hex.toPlainText())
        if data is None:
            self._show_error("TX HEX 형식이 올바르지 않습니다.")
            return
        self._send("write_shared_memory", {"hex": self.tx_hex.toPlainText()}, "write_tx")

    def _update_diagnostic_timer(self, _checked: bool | None = None) -> None:
        """RX/TX 연속 읽기 체크 상태에 따라 500 ms 타이머를 제어합니다."""
        connected = self.connection.socket.state() == QtNetwork.QAbstractSocket.SocketState.ConnectedState
        enabled = self.continuous_rx_checkbox.isChecked() or self.continuous_tx_checkbox.isChecked()

        if connected and enabled:
            if not self.diagnostic_timer.isActive():
                self.diagnostic_timer.start()
            # 체크 직후 500 ms를 기다리지 않고 한 번 즉시 갱신합니다.
            self._refresh_diagnostics()
        else:
            self.diagnostic_timer.stop()

    def _refresh_diagnostics(self) -> None:
        """체크된 공유 메모리 영역을 500 ms마다 갱신합니다."""
        if self.connection.socket.state() != QtNetwork.QAbstractSocket.SocketState.ConnectedState:
            self.diagnostic_timer.stop()
            return

        if self.continuous_rx_checkbox.isChecked():
            self._request_shared_memory("read", "read_rx")
        if self.continuous_tx_checkbox.isChecked() and not self._tx_dirty:
            self._request_shared_memory("write", "read_tx_auto")

    @staticmethod
    def _create_word_table(editable: bool = False) -> QtWidgets.QTableWidget:
        """1 Word 진단용 테이블을 생성합니다.

        RX는 읽기 전용이고, TX는 HEX/DEC 셀만 직접 수정할 수 있습니다.
        연속 갱신 중 레이아웃 재계산을 피하기 위해 컬럼 폭을 고정합니다.
        """
        bit_headers = [format(bit, "X") for bit in range(16)]
        headers = ["Word", "ASCII", "HEX (BE)", "DEC", *bit_headers]
        table = QtWidgets.QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setProperty("wordEditable", editable)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        table.verticalHeader().setDefaultSectionSize(24)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
        table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)

        if editable:
            table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
                | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
                | QtWidgets.QAbstractItemView.EditTrigger.SelectedClicked
            )
        else:
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        # ResizeToContents는 500 ms 갱신마다 전체 셀을 다시 측정해 스크롤 렉의 원인이 됩니다.
        # 모든 폭을 고정하여 데이터 갱신 중 geometry 재계산을 최소화합니다.
        header = table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 68)   # Word
        table.setColumnWidth(1, 60)   # ASCII
        table.setColumnWidth(2, 92)   # HEX
        table.setColumnWidth(3, 78)   # DEC
        for column in range(4, 20):
            table.setColumnWidth(column, 28)

        return table

    @staticmethod
    def _hex_text_to_bytes(text: str) -> bytes | None:
        """공백/줄바꿈/일반 구분자가 포함된 HEX 문자열을 bytes로 변환합니다."""
        cleaned = text.replace("0x", "").replace("0X", "")
        for separator in (" ", "\t", "\r", "\n", ",", ";", ":", "_", "-"):
            cleaned = cleaned.replace(separator, "")

        if not cleaned:
            return b""
        if len(cleaned) % 2:
            return None
        if any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
            return None

        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _bytes_to_hex_text(data: bytes) -> str:
        """공유 메모리 byte 배열을 보기 쉬운 공백 구분 HEX 문자열로 변환합니다."""
        return " ".join(f"{value:02X}" for value in data)

    @staticmethod
    def _ascii_word(raw: bytes) -> str:
        """Word의 두 바이트를 표시 가능한 ASCII로 변환합니다."""
        return "".join(chr(value) if 0x20 <= value <= 0x7E else "." for value in raw)

    def _byteorder(self) -> str:
        """현재 선택된 Word byte order를 반환합니다."""
        return "little" if self.little_endian_radio.isChecked() else "big"

    @staticmethod
    def _set_plain_text_preserve_view(
        edit: QtWidgets.QPlainTextEdit,
        text: str,
        *,
        block_signals: bool = False,
    ) -> bool:
        """내용이 실제로 달라질 때만 텍스트를 바꾸고 현재 스크롤 위치를 보존합니다."""
        if edit.toPlainText() == text:
            return False

        vbar = edit.verticalScrollBar()
        hbar = edit.horizontalScrollBar()
        vpos = vbar.value()
        hpos = hbar.value()
        cursor = edit.textCursor()
        cursor_position = cursor.position()

        previous = edit.blockSignals(block_signals)
        try:
            edit.setPlainText(text)
        finally:
            edit.blockSignals(previous)

        vbar.setValue(min(vpos, vbar.maximum()))
        hbar.setValue(min(hpos, hbar.maximum()))
        cursor = edit.textCursor()
        cursor.setPosition(min(cursor_position, len(text)))
        edit.setTextCursor(cursor)
        return True

    def _configure_table_item(self, table: QtWidgets.QTableWidget, item: QtWidgets.QTableWidgetItem, column: int) -> None:
        """RX/TX 테이블 셀의 편집 가능 여부와 정렬을 지정합니다."""
        editable_table = bool(table.property("wordEditable"))
        editable_column = editable_table and column in (2, 3)  # HEX / DEC

        flags = QtCore.Qt.ItemFlag.ItemIsSelectable | QtCore.Qt.ItemFlag.ItemIsEnabled
        if editable_column:
            flags |= QtCore.Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)

        if column != 1:
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.AlignmentFlag.AlignVCenter)

    def _set_table_text(
        self,
        table: QtWidgets.QTableWidget,
        row: int,
        column: int,
        text: str,
        *,
        tooltip: str = "",
    ) -> None:
        """필요한 셀만 생성/변경합니다. 동일한 값이면 Qt UI를 건드리지 않습니다."""
        item = table.item(row, column)
        if item is None:
            item = QtWidgets.QTableWidgetItem(text)
            self._configure_table_item(table, item, column)
            if tooltip:
                item.setToolTip(tooltip)
            table.setItem(row, column, item)
            return

        if item.text() != text:
            item.setText(text)
        if tooltip and item.toolTip() != tooltip:
            item.setToolTip(tooltip)

    def _fill_word_table(self, table: QtWidgets.QTableWidget, hex_text: str, *, force: bool = False) -> None:
        """HEX 데이터를 Word 단위로 갱신합니다.

        기존 버전처럼 500 ms마다 모든 QTableWidgetItem을 삭제/재생성하지 않고,
        이전 byte 데이터와 비교하여 바뀐 Word 행만 수정합니다.
        """
        data = self._hex_text_to_bytes(hex_text)
        if data is None:
            if table.rowCount() != 0:
                table.setRowCount(0)
            self._table_cache.pop(id(table), None)
            return

        byteorder = self._byteorder()
        cache_key = id(table)
        previous_data, previous_byteorder = self._table_cache.get(cache_key, (b"", ""))
        word_count = (len(data) + 1) // 2

        # 같은 데이터 + 같은 Endian이면 500 ms 응답이 와도 UI 갱신을 전혀 하지 않습니다.
        if not force and data == previous_data and byteorder == previous_byteorder:
            return

        endian_label = "LE" if byteorder == "little" else "BE"
        header_item = table.horizontalHeaderItem(2)
        header_text = f"HEX ({endian_label})"
        if header_item is not None and header_item.text() != header_text:
            header_item.setText(header_text)

        row_count_changed = table.rowCount() != word_count
        old_vpos = table.verticalScrollBar().value()
        old_hpos = table.horizontalScrollBar().value()

        self._table_refreshing = True
        table.setUpdatesEnabled(False)
        try:
            if row_count_changed:
                table.setRowCount(word_count)

            for word_index in range(word_count):
                byte_offset = word_index * 2
                raw_word = data[byte_offset: byte_offset + 2]
                old_word = previous_data[byte_offset: byte_offset + 2]

                # 길이/Endian이 같고 해당 Word byte가 그대로면 이 행은 건너뜁니다.
                if (
                    not force
                    and not row_count_changed
                    and byteorder == previous_byteorder
                    and raw_word == old_word
                ):
                    continue

                display_word = raw_word if byteorder == "big" else raw_word[::-1]
                value = int.from_bytes(raw_word, byteorder=byteorder, signed=False)
                hex_value = "0x" + display_word.hex().upper()
                values = [
                    f"W{word_index:04d}",
                    self._ascii_word(display_word),
                    hex_value,
                    str(value),
                    *[str((value >> bit) & 0x1) for bit in range(16)],
                ]

                for column, cell_text in enumerate(values):
                    tooltip = ""
                    if column == 0:
                        tooltip = f"Byte offset +{byte_offset}"
                    elif column >= 4:
                        bit_no = column - 4
                        tooltip = f"Bit {format(bit_no, 'X')} ({bit_no})"
                    elif table.property("wordEditable") and column in (2, 3):
                        tooltip = "더블클릭하여 Word 값을 수정할 수 있습니다."
                    self._set_table_text(table, word_index, column, cell_text, tooltip=tooltip)
        finally:
            table.setUpdatesEnabled(True)
            self._table_refreshing = False

        # Row 수가 바뀐 경우에도 사용자가 보고 있던 위치를 최대한 유지합니다.
        if row_count_changed:
            table.verticalScrollBar().setValue(min(old_vpos, table.verticalScrollBar().maximum()))
            table.horizontalScrollBar().setValue(min(old_hpos, table.horizontalScrollBar().maximum()))

        self._table_cache[cache_key] = (bytes(data), byteorder)

    def _stop_tx_continuous_for_edit(self) -> None:
        """사용자가 TX를 편집하기 시작하면 자동 읽기가 로컬 값을 덮어쓰지 않도록 중지합니다."""
        if self.continuous_tx_checkbox.isChecked():
            self.continuous_tx_checkbox.setChecked(False)
        self._tx_dirty = True

    @QtCore.Slot()
    def _on_tx_hex_changed(self) -> None:
        """사용자가 직접 TX HEX 텍스트를 수정했을 때 표를 갱신합니다."""
        if self._setting_tx_hex:
            return
        self._stop_tx_continuous_for_edit()
        # 텍스트 직접 편집은 이전 캐시와 비교해 필요한 행만 갱신합니다.
        self._fill_word_table(self.tx_table, self.tx_hex.toPlainText())

    def _on_tx_table_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        """TX 표의 HEX/DEC 셀을 숫자로 수정하면 해당 Word의 2 byte를 갱신합니다."""
        if self._table_refreshing:
            return
        if item.column() not in (2, 3):
            return

        data = self._hex_text_to_bytes(self.tx_hex.toPlainText())
        if data is None:
            self._show_error("현재 TX HEX 데이터 형식이 올바르지 않아 표 값을 수정할 수 없습니다.")
            return

        row = item.row()
        byte_offset = row * 2
        if byte_offset + 2 > len(data):
            self._show_error("마지막 Word가 2 byte가 아니므로 이 행은 Word 값으로 수정할 수 없습니다.")
            self._fill_word_table(self.tx_table, self.tx_hex.toPlainText(), force=True)
            return

        text = item.text().strip()
        try:
            if item.column() == 2:
                # HEX: 0x1234 또는 1234 모두 허용합니다.
                normalized = text[2:] if text.lower().startswith("0x") else text
                if not normalized or any(ch not in "0123456789abcdefABCDEF" for ch in normalized):
                    raise ValueError
                value = int(normalized, 16)
            else:
                # DEC: 0 ~ 65535
                value = int(text, 10)

            if not 0 <= value <= 0xFFFF:
                raise ValueError
        except ValueError:
            self._show_error("Word 값은 HEX 0x0000~0xFFFF 또는 DEC 0~65535 범위로 입력하세요.")
            self._fill_word_table(self.tx_table, self.tx_hex.toPlainText(), force=True)
            return

        mutable = bytearray(data)
        mutable[byte_offset: byte_offset + 2] = value.to_bytes(2, byteorder=self._byteorder(), signed=False)
        new_data = bytes(mutable)
        new_hex = self._bytes_to_hex_text(new_data)

        self._stop_tx_continuous_for_edit()
        self._setting_tx_hex = True
        try:
            self._set_plain_text_preserve_view(self.tx_hex, new_hex, block_signals=True)
        finally:
            self._setting_tx_hex = False

        # 변경한 Word를 포함해 파생 ASCII/HEX/DEC/Bit 값을 즉시 맞춥니다.
        self._fill_word_table(self.tx_table, new_hex)
        self.message.setText(f"TX W{row:04d} 값을 수정했습니다. 커밋 버튼을 눌러 공유 메모리에 반영하세요.")

    @QtCore.Slot(bool)
    def _endianness_changed(self, checked: bool) -> None:
        """Endian 라디오 버튼이 바뀌면 RX/TX 표를 한 번 전체 재해석합니다."""
        if not checked:
            return
        self._fill_word_table(self.rx_table, self.rx_hex.toPlainText(), force=True)
        self._fill_word_table(self.tx_table, self.tx_hex.toPlainText(), force=True)

    def _save(self) -> None:
        try:
            cpu_info = int(self.cpu_info.text().strip(), 0)
        except ValueError:
            self._show_error("CPU Info는 0xA0 같은 16진수 또는 160 같은 10진수로 입력하세요.")
            return
        patch = {
            "plc": {"enabled": self.plc_enabled.isChecked(), "host": self.plc_host.text().strip(), "port": self.plc_port.value(), "cpu_info": cpu_info, "slot": self.slot.value(), "base": self.base.value(), "use_bcc": self.use_bcc.isChecked(), "connect_timeout_s": self.connect_timeout.value(), "io_timeout_s": self.io_timeout.value(), "retry_initial_ms": self.retry_initial.value(), "retry_max_ms": self.retry_max.value(), "retry_multiplier": self.retry_multiplier.value()},
            "read": {"enabled": self.read_enabled.isChecked(), "address": self.read_address.text().strip(), "byte_count": self.read_count.value(), "interval_ms": self.read_interval.value()},
            "write": {"enabled": self.write_enabled.isChecked(), "address": self.write_address.text().strip(), "byte_count": self.write_count.value(), "interval_ms": self.write_interval.value(), "mode": self.write_mode.currentData(), "write_on_startup": self.write_startup.isChecked()},
            "shared_memory": {"name": self.shm_name.text().strip(), "size": self.shm_size.value(), "read_offset": self.shm_read_offset.value(), "write_offset": self.shm_write_offset.value(), "unlink_on_exit": self.shm_unlink.isChecked()},
            "web": {"enabled": self.web_enabled.isChecked(), "host": self.web_host.text().strip(), "port": self.web_port.value()},
        }
        self._send("update_config", {"patch": patch}, "update_config")
        self.message.setText("설정을 검증하고 적용하는 중...")

    @QtCore.Slot(dict)
    def _response(self, response: dict[str, Any]) -> None:
        request_id = str(response.get("id", ""))
        operation = self.pending.pop(request_id, "")
        if not response.get("ok"):
            self._show_error(response.get("message") or response.get("error") or "요청 실패")
            return
        result = response.get("result")
        if operation == "get_config":
            self._fill_config(result); self.message.setText("설정을 불러왔습니다.")
        elif operation == "get_status":
            self._fill_status(result)
        elif operation == "update_config":
            self._fill_config(result["config"])
            note = ", ".join(result.get("restart_required", []))
            self.message.setText("저장하고 즉시 적용했습니다." + (f" 재시작 필요: {note}" if note else ""))
        elif operation in {"plc_toggle", "web_toggle"}:
            self._request_config(); self._request_status(); self.message.setText("상태를 변경했습니다.")
        elif operation == "reconnect":
            self.message.setText("PLC 재연결을 요청했습니다.")
        elif operation == "read_rx":
            # 내용이 달라진 경우에만 HEX/Text/Table을 갱신하여 스크롤 렉을 줄입니다.
            self._set_plain_text_preserve_view(self.rx_hex, result["hex"], block_signals=True)
            self._fill_word_table(self.rx_table, result["hex"])
            self.message.setText(f"RX {result['length']}바이트, sequence={result['sequence']}")
        elif operation in {"read_tx_manual", "read_tx_auto"}:
            # 편집 중에는 직전에 도착한 자동 읽기 응답이 로컬 수정 값을 덮어쓰지 않습니다.
            if operation == "read_tx_auto" and self._tx_dirty:
                return
            self._setting_tx_hex = True
            try:
                self._set_plain_text_preserve_view(self.tx_hex, result["hex"], block_signals=True)
            finally:
                self._setting_tx_hex = False
            self._fill_word_table(self.tx_table, result["hex"])
            self._tx_dirty = False
            self.message.setText(f"TX {result['length']}바이트, sequence={result['sequence']}")
        elif operation == "write_tx":
            self._tx_dirty = False
            self.message.setText(f"TX 데이터를 커밋했습니다. sequence={result['sequence']}")

    def _fill_config(self, c: dict[str, Any]) -> None:
        p = c["plc"]
        self.plc_enabled.setChecked(p["enabled"]); self.plc_host.setText(p["host"]); self.plc_port.setValue(p["port"])
        self.cpu_info.setText(f"0x{p['cpu_info']:02X}"); self.slot.setValue(p["slot"]); self.base.setValue(p["base"]); self.use_bcc.setChecked(p["use_bcc"])
        self.connect_timeout.setValue(p["connect_timeout_s"]); self.io_timeout.setValue(p["io_timeout_s"]); self.retry_initial.setValue(p["retry_initial_ms"]); self.retry_max.setValue(p["retry_max_ms"]); self.retry_multiplier.setValue(p["retry_multiplier"])
        r, w = c["read"], c["write"]
        self.read_enabled.setChecked(r["enabled"]); self.read_address.setText(r["address"]); self.read_count.setValue(r["byte_count"]); self.read_interval.setValue(r["interval_ms"])
        self.write_enabled.setChecked(w["enabled"]); self.write_address.setText(w["address"]); self.write_count.setValue(w["byte_count"]); self.write_interval.setValue(w["interval_ms"]); self.write_mode.setCurrentIndex(max(0, self.write_mode.findData(w["mode"]))); self.write_startup.setChecked(w["write_on_startup"])
        s = c["shared_memory"]
        self.shm_name.setText(s["name"]); self.shm_size.setValue(s["size"]); self.shm_read_offset.setValue(s["read_offset"]); self.shm_write_offset.setValue(s["write_offset"]); self.shm_unlink.setChecked(s["unlink_on_exit"])
        web = c["web"]
        self.web_enabled.setChecked(web["enabled"]); self.web_host.setText(web["host"]); self.web_port.setValue(web["port"]); self.current_web_enabled = web["enabled"]

    def _fill_status(self, s: dict[str, Any]) -> None:
        self.plc_state.setText(str(s.get("plc_state", "-"))); self.plc_endpoint.setText(str(s.get("plc_endpoint", "-")))
        self.reads.setText(str(s.get("read_count", 0))); self.writes.setText(str(s.get("write_count", 0))); self.last_error.setText(s.get("last_error") or "없음")

    @QtCore.Slot(str)
    def _show_error(self, text: str) -> None:
        self.message.setText(f"오류: {text}")

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int) -> QtWidgets.QSpinBox:
        widget = QtWidgets.QSpinBox(); widget.setRange(minimum, maximum); widget.setValue(value); return widget

    @staticmethod
    def _double_spin(minimum: float, maximum: float, value: float) -> QtWidgets.QDoubleSpinBox:
        widget = QtWidgets.QDoubleSpinBox(); widget.setRange(minimum, maximum); widget.setDecimals(2); widget.setValue(value); return widget

    def _saved_int(self, key: str, default: int) -> int:
        try:
            return int(self.settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def _save_connection_settings(self) -> None:
        self.settings.setValue("connection/host", self.host.text().strip())
        self.settings.setValue("connection/port", self.control_port.value())


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("XGT Gateway Config")
    app.setStyle("Fusion")
    window = MainWindow(); window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
